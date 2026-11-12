"""Read a CSV or Excel sheet into rows of cells. Shared by the product table and the receivables export."""
from __future__ import annotations

import csv
import io


class Unreadable(Exception):
    """The file cannot be read by rule. A person takes over."""


def is_xlsx(content: bytes) -> bool:
    return content[:2] == b"PK"


def grid(content: bytes) -> list[list]:
    """Rows of raw cell values from the first sheet (Excel) or the whole file (CSV)."""
    if is_xlsx(content):
        import openpyxl
        try:
            book = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        except Exception as exc:
            raise Unreadable(f"Excel 檔無法開啟：{exc}") from exc
        return [list(row) for row in book.worksheets[0].iter_rows(values_only=True)]
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return list(csv.reader(io.StringIO(content.decode(encoding))))
        except UnicodeDecodeError:
            continue
    raise Unreadable("檔案不是 UTF-8 或 Big5 編碼的 CSV，也不是 Excel")


def records(content: bytes, required: list[str]) -> list[tuple[int, dict]]:
    """(row number, {header: cell}) for a sheet whose first row is the header."""
    rows = grid(content)
    if not rows:
        raise Unreadable("檔案是空的")
    header = ["" if c is None else str(c).strip() for c in rows[0]]
    missing = [name for name in required if name not in header]
    if missing:
        raise Unreadable(f"缺少欄位：{'、'.join(missing)}")
    out = []
    for number, row in enumerate(rows[1:], start=2):
        if any(c not in (None, "") for c in row):
            out.append((number, dict(zip(header, row))))
    return out
