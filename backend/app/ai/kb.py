"""理赔条款知识库和检索。

语料只有几十条短文档，所以放在内存里检索，不引入向量数据库：部署简单，2 核 4G 的服务器也放得下。
数据量到几千条，或者要按公司上传私有条款时，再换成 pgvector。

关键词和向量两路各有所长：“第 496 条”“7 倍运费”这类词要精确匹配，
“盒子没坏但东西碎了”和“外包装完好”这样的说法要靠语义。两路结果用 RRF 融合，不用调权重。
"""

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KB_DIR = Path(__file__).resolve().parents[2] / "kb"
INDEX_PATH = KB_DIR / "embeddings.json"
CARRIER_TERMS_TYPE = "承运商条款"
# 这些来源可能过时或只是单方说法，界面上要提醒
WEAK_TIERS = frozenset(
    {"媒体转述", "媒体报道", "投诉平台（单方陈述）", "二手摘述", "第三方转载官方文本"}
)

_RRF_K = 60
_BM25_K1 = 1.5
_BM25_B = 0.75
_CHINESE = re.compile(r"[一-鿿]+")
_LATIN = re.compile(r"[A-Za-z0-9.]+")


@dataclass(frozen=True)
class Doc:
    id: str
    doc_type: str
    title: str
    carrier: str
    source_name: str
    source_url: str
    source_tier: str
    version_date: str
    text: str
    terms: tuple[tuple[str, str], ...]
    reasons: tuple[str, ...]
    counters: tuple[str, ...]
    tags: tuple[str, ...]
    is_eval: bool
    case: dict | None

    @property
    def search_text(self) -> str:
        return " ".join((self.title, self.text, " ".join(self.tags)))

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.search_text.encode("utf-8")).hexdigest()[:16]

    @property
    def is_weak_source(self) -> bool:
        return self.source_tier in WEAK_TIERS


@dataclass(frozen=True)
class Hit:
    doc: Doc
    score: float
    via: tuple[str, ...]


def _as_doc(raw: dict) -> Doc:
    case = raw.get("case")
    reasons = raw.get("reasons") or (case or {}).get("carrier_defenses") or ()
    counters = raw.get("counters") or (case or {}).get("counter_arguments_accepted") or ()
    return Doc(
        id=raw["id"],
        doc_type=raw["doc_type"],
        title=raw["title"],
        carrier=raw.get("carrier") or "",
        source_name=raw["source_name"],
        source_url=raw["source_url"],
        source_tier=raw["source_tier"],
        version_date=raw["version_date"],
        text=raw["text"],
        terms=tuple((raw.get("terms") or {}).items()),
        reasons=tuple(reasons),
        counters=tuple(counters),
        tags=tuple(raw.get("tags") or ()),
        is_eval=raw.get("split_suggestion") == "eval",
        case=case,
    )


@lru_cache
def load_all_docs() -> tuple[Doc, ...]:
    docs = []
    for name in ("claims_corpus.jsonl", "extra_corpus.jsonl"):
        with (KB_DIR / name).open(encoding="utf-8") as file:
            docs += [_as_doc(json.loads(line)) for line in file if line.strip()]
    return tuple(docs)


def searchable_docs() -> tuple[Doc, ...]:
    """留作评测的判例不进检索库，否则评测就成了开卷考试。"""
    return tuple(doc for doc in load_all_docs() if not doc.is_eval)


@lru_cache
def load_taxonomy() -> dict[str, dict]:
    with (KB_DIR / "taxonomy.json").open(encoding="utf-8") as file:
        raw = json.load(file)
    return {
        "reasons": {item["id"]: item for item in raw["refusal_reasons"]},
        "counters": {item["id"]: item for item in raw["counter_arguments"]},
        "evidence": {item["id"]: item for item in raw["evidence_items"]},
    }


def tokenize(text: str) -> list[str]:
    """中文切成单字和相邻两字。不用分词词典，少一个依赖，对“保价”“称重”这类短词够用。"""
    tokens = [word.lower() for word in _LATIN.findall(text)]
    for run in _CHINESE.findall(text):
        tokens += list(run)
        tokens += [run[i : i + 2] for i in range(len(run) - 1)]
    return tokens


class Bm25:
    def __init__(self, docs: tuple[Doc, ...]) -> None:
        self.docs = docs
        self.counts = [Counter(tokenize(doc.search_text)) for doc in docs]
        self.lengths = [sum(count.values()) for count in self.counts]
        self.average = sum(self.lengths) / max(len(docs), 1)
        frequency: Counter = Counter()
        for count in self.counts:
            frequency.update(count.keys())
        total = len(docs)
        self.idf = {
            token: math.log(1 + (total - seen + 0.5) / (seen + 0.5))
            for token, seen in frequency.items()
        }

    def rank(self, query: str) -> list[Doc]:
        terms = Counter(tokenize(query))
        scored = []
        for doc, count, length in zip(self.docs, self.counts, self.lengths, strict=True):
            score = 0.0
            for term, weight in terms.items():
                seen = count.get(term, 0)
                if seen:
                    norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * length / self.average)
                    score += weight * self.idf[term] * seen * (_BM25_K1 + 1) / (seen + norm)
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [doc for _, doc in scored]


def load_index() -> dict[str, list[float]]:
    """读取预先算好的向量。内容变了的文档哈希对不上，会被跳过，只走关键词。"""
    if not INDEX_PATH.exists():
        return {}
    with INDEX_PATH.open(encoding="utf-8") as file:
        stored = json.load(file)["vectors"]
    return {
        doc.id: stored[doc.id]["vector"]
        for doc in searchable_docs()
        if doc.id in stored and stored[doc.id]["hash"] == doc.content_hash
    }


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class Retriever:
    def __init__(
        self,
        docs: tuple[Doc, ...] | None = None,
        vectors: dict[str, list[float]] | None = None,
    ) -> None:
        self.docs = searchable_docs() if docs is None else docs
        self.vectors = load_index() if vectors is None else vectors
        self.bm25 = Bm25(self.docs)

    def rank_vector(self, query_vector: list[float]) -> list[Doc]:
        scored = [
            (_cosine(query_vector, self.vectors[doc.id]), doc)
            for doc in self.docs
            if doc.id in self.vectors
        ]
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [doc for _, doc in scored]

    def search(
        self,
        query: str,
        *,
        k: int = 6,
        query_vector: list[float] | None = None,
        accept: Callable[[Doc], bool] = lambda _: True,
    ) -> list[Hit]:
        rankings = {"keyword": self.bm25.rank(query)}
        if query_vector is not None:
            rankings["vector"] = self.rank_vector(query_vector)

        scores: Counter = Counter()
        via: dict[str, list[str]] = {}
        by_id = {}
        for name, ranking in rankings.items():
            kept = [doc for doc in ranking if accept(doc)]
            for position, doc in enumerate(kept, start=1):
                scores[doc.id] += 1 / (_RRF_K + position)
                via.setdefault(doc.id, []).append(name)
                by_id[doc.id] = doc
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
        return [Hit(by_id[doc_id], score, tuple(via[doc_id])) for doc_id, score in ordered]


def match_carrier_terms(carrier: str | None, docs: tuple[Doc, ...] | None = None) -> Doc | None:
    """“顺丰速运”也要匹配到顺丰的条款。"""
    if not carrier:
        return None
    for doc in searchable_docs() if docs is None else docs:
        if doc.doc_type == CARRIER_TERMS_TYPE and doc.carrier and doc.carrier in carrier:
            return doc
    return None
