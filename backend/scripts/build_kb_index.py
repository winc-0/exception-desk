"""给理赔条款语料算向量，结果写入 kb/embeddings.json 并提交到仓库。用法：

uv run python -m scripts.build_kb_index

语料更新后要重新运行，否则内容变了的文档只能走关键词，测试也会报错。
"""

import asyncio
import json

from app.ai.kb import INDEX_PATH, searchable_docs
from app.config import get_settings
from app.main import build_embeddings


async def main() -> None:
    settings = get_settings()
    embeddings = build_embeddings(settings)
    if embeddings is None:
        raise SystemExit("请先配置 DESK_LLM_API_KEY 和 DESK_EMBEDDING_MODEL")

    docs = searchable_docs()
    vectors = await embeddings.aembed_documents([doc.search_text for doc in docs])
    payload = {
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
        "vectors": {
            doc.id: {
                "hash": doc.content_hash,
                "vector": [round(value, 5) for value in vector],
            }
            for doc, vector in zip(docs, vectors, strict=True)
        },
    }
    INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"已写入 {len(docs)} 条向量，维度 {len(vectors[0])}")


if __name__ == "__main__":
    asyncio.run(main())
