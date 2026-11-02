"""证据文件存储。

文件放在本地目录，路径只由租户和附件 ID 决定，用户提交的文件名不参与拼路径。
文件类型按文件头判断，不相信客户端声明的类型和扩展名。
"""

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
_HEAD_BYTES = 16

# 这些类型可以在浏览器里直接打开，其余只能下载
INLINE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime"})


class FileTooLarge(Exception):
    pass


def sniff_content_type(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        # iPhone 默认拍的是 HEIC
        if brand in {b"heic", b"heix", b"hevc", b"mif1", b"msf1"}:
            return "image/heic"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    return None


def clean_filename(name: str) -> str:
    """只用于展示和下载时的文件名。"""
    base = re.split(r"[\\/]", name)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[-200:] or "未命名文件"


def attachment_path(root: str, tenant_id: uuid.UUID, attachment_id: uuid.UUID) -> Path:
    return Path(root) / str(tenant_id) / str(attachment_id)


@dataclass(frozen=True)
class Received:
    temp_path: Path
    size: int
    sha256: str
    head: bytes


def _prepare_temp(root: str) -> Path:
    directory = Path(root) / ".tmp"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / uuid.uuid4().hex


async def receive(stream: AsyncIterator[bytes], root: str) -> Received:
    """把请求体写到临时文件。边收边计数，超过上限立即停止，不会先占满磁盘。"""
    temp_path = await asyncio.to_thread(_prepare_temp, root)
    digest = hashlib.sha256()
    head = b""
    size = 0
    output = await asyncio.to_thread(temp_path.open, "wb")
    try:
        async for chunk in stream:
            size += len(chunk)
            if size > MAX_BYTES:
                raise FileTooLarge
            if len(head) < _HEAD_BYTES:
                head = (head + chunk)[:_HEAD_BYTES]
            digest.update(chunk)
            await asyncio.to_thread(output.write, chunk)
    except BaseException:
        await asyncio.to_thread(output.close)
        await discard(temp_path)
        raise
    await asyncio.to_thread(output.close)
    return Received(temp_path, size, digest.hexdigest(), head)


def _publish(temp_path: Path, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, final_path)


async def publish(temp_path: Path, final_path: Path) -> None:
    await asyncio.to_thread(_publish, temp_path, final_path)


async def discard(path: Path) -> None:
    await asyncio.to_thread(path.unlink, missing_ok=True)
