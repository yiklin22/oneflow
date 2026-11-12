"""Dates, quantities and money are checked by these rules and nothing else.

Every function returns (value, notes). A value of None means "a person must
type it"; notes say why in Traditional Chinese, because the reviewer reads them.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation


def norm(value) -> str:
    """Full-width digits and letters become half-width; outer spaces go."""
    return unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()


_YMD = re.compile(r"(民國)?\s*(\d{2,4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?")
_MD = re.compile(r"(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?")


def parse_date(raw, ref: date | None = None) -> tuple[date | None, list[str]]:
    """Accepts 2026/11/20, 2026-11-20, 115/11/20, 民國115年11月20日, and 11/20 when `ref` is given."""
    if isinstance(raw, datetime):
        return raw.date(), []
    if isinstance(raw, date):
        return raw, []
    s = norm(raw)
    if not s:
        return None, ["缺少日期"]
    notes: list[str] = []
    if m := _YMD.fullmatch(s):
        y, mo, d = int(m[2]), int(m[3]), int(m[4])
        if m[1] or 100 <= y <= 200:
            y += 1911
            notes.append(f"民國年換算為西元 {y} 年")
        elif y < 1900:
            return None, [f"無法辨識的年份：{s}"]
    elif (m := _MD.fullmatch(s)) and ref:
        mo, d = int(m[1]), int(m[2])
        y = ref.year
        try:
            if date(y, mo, d) < ref:
                y += 1
        except ValueError:
            pass
        notes.append(f"原文沒有年份，依訂單日期推定為 {y} 年")
    else:
        return None, [f"無法辨識的日期格式：{s}"]
    try:
        return date(y, mo, d), notes
    except ValueError:
        return None, [f"日期不存在：{s}"]


def check_due(due: date, order_date: date | None) -> list[str]:
    if order_date and due < order_date:
        return [f"交期 {due} 早於訂單日期 {order_date}"]
    if order_date and due > order_date + timedelta(days=730):
        return [f"交期 {due} 距訂單日期超過兩年"]
    return []


_QTY = re.compile(r"(-?[\d,]*\.?\d+)\s*([^\d\s.,]*)")


def parse_number(text: str) -> Decimal | None:
    """1,200 is fine; 1,20 or 12,00 is not a number we are willing to guess at."""
    if "," in text and not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def parse_qty(raw) -> tuple[Decimal | None, str, list[str]]:
    """Returns (quantity, unit as written, notes). Quantity must be a positive whole number."""
    if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
        s = format(Decimal(str(raw)).normalize(), "f")
    else:
        s = norm(raw)
    if not s:
        return None, "", ["缺少數量"]
    m = _QTY.fullmatch(s)
    value = parse_number(m[1]) if m else None
    if value is None:
        return None, "", [f"無法辨識的數量：{s}"]
    if value <= 0:
        return None, m[2], [f"數量必須大於零：{s}"]
    if value != value.to_integral_value():
        return None, m[2], [f"數量不是整數：{s}"]
    return value.to_integral_value(), m[2], []


def parse_money(raw) -> tuple[Decimal | None, list[str]]:
    """New Taiwan dollars. Accepts NT$1,200, 1200元, (1,200) for negative; at most two decimals."""
    if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
        s = format(Decimal(str(raw)), "f")
    else:
        s = norm(raw)
    if not s:
        return None, ["缺少金額"]
    t = re.sub(r"^(NT\$|NTD|TWD|\$)\s*|\s*元$", "", s, flags=re.I).strip()
    negative = t.startswith("(") and t.endswith(")")
    value = parse_number(t.strip("()"))
    if value is None:
        return None, [f"無法辨識的金額：{s}"]
    if value != value.quantize(Decimal("0.01")):
        return None, [f"金額小數超過兩位：{s}"]
    return (-value if negative else value), []
