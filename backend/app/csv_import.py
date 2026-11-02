"""解析批量导入的 CSV。纯函数，不依赖数据库。"""

import csv
import io
from dataclasses import dataclass

MAX_ROWS = 1000
MAX_BYTES = 1024 * 1024

_COLUMNS = {
    "类型": "type",
    "异常类型": "type",
    "type": "type",
    "标题": "title",
    "title": "title",
    "运单号": "tracking_number",
    "tracking_number": "tracking_number",
    "描述": "description",
    "description": "description",
}
_TYPES = {
    "破损": "damaged",
    "丢件": "lost",
    "超时": "delayed",
    "改址": "address_change",
    "其他": "other",
    "damaged": "damaged",
    "lost": "lost",
    "delayed": "delayed",
    "address_change": "address_change",
    "other": "other",
}


@dataclass(frozen=True)
class ImportRow:
    line: int
    type: str
    title: str
    tracking_number: str | None
    description: str


@dataclass(frozen=True)
class RowError:
    line: int
    message: str


def _decode(content: bytes) -> str:
    # Excel 在中文 Windows 上默认另存为 GBK，用 UTF-8 打开会乱码，这里两种都支持
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return content.decode("gb18030")
        except UnicodeDecodeError:
            raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GBK 编码的 CSV") from None


def _parse_row(line: int, values: dict[str, str]) -> ImportRow | RowError:
    type_value = _TYPES.get(values.get("type", ""))
    if type_value is None:
        return RowError(line, "类型必须是：破损、丢件、超时、改址、其他")
    title = values.get("title", "")
    if not title:
        return RowError(line, "标题不能为空")
    if len(title) > 200:
        return RowError(line, "标题不能超过 200 个字符")
    tracking_number = values.get("tracking_number", "")
    if len(tracking_number) > 64:
        return RowError(line, "运单号不能超过 64 个字符")
    description = values.get("description", "")
    if len(description) > 5000:
        return RowError(line, "描述不能超过 5000 个字符")
    return ImportRow(line, type_value, title, tracking_number or None, description)


def parse_csv(content: bytes) -> tuple[list[ImportRow], list[RowError]]:
    """返回解析成功的行和每行的错误。文件结构有问题时抛 ValueError。"""
    reader = csv.reader(io.StringIO(_decode(content), newline=""))
    try:
        header = next(reader, None)
        if header is None:
            raise ValueError("文件是空的")
        columns = [_COLUMNS.get(name.strip().lower()) for name in header]
        missing = {"type", "title"} - set(columns)
        if missing:
            raise ValueError("缺少必需的列：类型、标题")

        rows: list[ImportRow] = []
        errors: list[RowError] = []
        count = 0
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue
            count += 1
            if count > MAX_ROWS:
                raise ValueError(f"一次最多导入 {MAX_ROWS} 行")
            values = {
                column: cell.strip()
                for column, cell in zip(columns, record, strict=False)
                if column is not None
            }
            parsed = _parse_row(reader.line_num, values)
            (errors if isinstance(parsed, RowError) else rows).append(parsed)
    except csv.Error:
        raise ValueError("文件不是有效的 CSV") from None

    if count == 0:
        raise ValueError("文件里没有数据行")
    return rows, errors
