"""Order entry: read a PDF or Excel order, match part numbers to the product
table, and export only what a person has confirmed.

Nothing here ships goods, changes a price or posts to the ledger. Reading is
by rule, not by a model: a layout the rules cannot read goes to a person.
"""
from __future__ import annotations

import csv
import difflib
import io
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import base, rules
from .base import Refused
from .models import CustomerPart, ExportBatch, Order, OrderLine, Product, User, now
from .tables import Unreadable, grid, is_xlsx, records

FLOW = "orders"
COLUMNS = {
    "part": ["料號", "品號", "產品編號", "貨號", "客戶料號", "part no", "part no.", "part number", "p/n"],
    "name": ["品名", "品名規格", "規格", "品項", "description"],
    "qty": ["數量", "訂購量", "訂購數量", "qty", "quantity"],
    "unit": ["單位", "unit"],
    "due": ["交期", "交貨日", "交貨日期", "需求日", "預交日", "delivery", "due date"],
    "price": ["單價", "unit price"],
}
META = {
    "customer": ["客戶名稱", "客戶", "訂購單位", "買方", "customer", "buyer"],
    "po_no": ["訂單編號", "採購單號", "訂單號碼", "po no", "po number"],
    "order_date": ["訂單日期", "採購日期", "日期", "date"],
    "due": COLUMNS["due"],
}
TABLE_END = ("合計", "總計", "小計", "備註", "注意", "以上")
COUNT_UNITS = {"", "PCS", "PC", "EA"}       # "pieces": the product's own unit


def _key(text) -> str:
    return re.sub(r"\s+", " ", rules.norm(text).lower()).rstrip(":：").strip()


_COLUMN_OF = {name: f for f, names in COLUMNS.items() for name in names}
_META_OF = {name: f for f, names in META.items() for name in names}
_META_RE = re.compile("(" + "|".join(sorted(map(re.escape, _META_OF), key=len, reverse=True)) + r")\s*[:：]\s*", re.I)


@dataclass
class Extracted:
    meta: dict = field(default_factory=dict)       # customer, po_no, order_date, due (as written)
    lines: list[dict] = field(default_factory=list)  # part, name, qty, unit, due, price + evidence
    text: str = ""                                 # the source as plain lines, for side-by-side


def _meta_from(text: str, meta: dict) -> None:
    hits = list(_META_RE.finditer(text))
    for i, m in enumerate(hits):
        value = text[m.end(): hits[i + 1].start() if i + 1 < len(hits) else len(text)].strip()
        if value:
            meta.setdefault(_META_OF[m[1].lower()], value)


def _read_pdf(content: bytes) -> Extracted:
    import pdfplumber
    out, text_lines, columns = Extracted(), [], None
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as exc:
        raise Unreadable(f"PDF 無法開啟：{exc}") from exc
    with pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)
            rows: list[list[dict]] = []
            for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
                if rows and abs(rows[-1][0]["top"] - w["top"]) <= 3:
                    rows[-1].append(w)
                else:
                    rows.append([w])
            in_table = columns is not None
            for row in rows:
                row.sort(key=lambda w: w["x0"])
                line_text = "  ".join(w["text"].strip() for w in row)
                text_lines.append(line_text)
                fields = [_COLUMN_OF.get(_key(w["text"])) for w in row]
                if "part" in fields and "qty" in fields:           # a table header
                    columns, in_table = list(zip(fields, row)), True
                    continue
                if not in_table:
                    for w in row:
                        _meta_from(w["text"], out.meta)
                    continue
                if line_text.startswith(TABLE_END):
                    in_table = False
                    continue
                cells: dict[str, str] = {}
                for w in row:
                    name, head = min(columns, key=lambda c: _gap(w, c[1]))
                    if name and _gap(w, head) <= 40:
                        cells[name] = (cells.get(name, "") + " " + w["text"]).strip()
                if cells.get("part") or cells.get("qty"):
                    cells["evidence"] = {"page": page_no, "line": len(text_lines) - 1, "text": line_text}
                    out.lines.append(cells)
    if not text_lines:
        raise Unreadable("PDF 沒有可讀文字（可能是掃描影像），需人工輸入")
    out.text = "\n".join(text_lines)
    return out


def _gap(word: dict, head: dict) -> float:
    """Distance between a cell and a header: left edges, right edges or centres, whichever is closest."""
    return min(abs(word["x0"] - head["x0"]), abs(word["x1"] - head["x1"]),
               abs((word["x0"] + word["x1"]) - (head["x0"] + head["x1"])) / 2)


def _read_xlsx(content: bytes) -> Extracted:
    out, columns = Extracted(), None
    rows = grid(content)
    out.text = "\n".join("\t".join("" if c is None else str(c) for c in row).rstrip() for row in rows)
    for index, row in enumerate(rows):
        fields = [_COLUMN_OF.get(_key(c)) for c in row]
        if columns is None:
            if "part" in fields and "qty" in fields:
                columns = fields
                continue
            for i, cell in enumerate(row):
                if isinstance(cell, str) and _META_RE.search(cell):
                    _meta_from(cell, out.meta)
                elif _key(cell) in _META_OF:
                    value = next((c for c in row[i + 1:] if c not in (None, "")), None)
                    if value is not None:
                        out.meta.setdefault(_META_OF[_key(cell)], value)
            continue
        if isinstance(row[0] if row else None, str) and row[0].startswith(TABLE_END):
            break
        cells = {name: cell for name, cell in zip(columns, row) if name and cell not in (None, "")}
        if "part" in cells or "qty" in cells:
            cells["evidence"] = {"page": 1, "line": index, "text": out.text.split("\n")[index]}
            out.lines.append(cells)
    return out


def extract(content: bytes) -> Extracted:
    if content[:4] == b"%PDF":
        out = _read_pdf(content)
    elif is_xlsx(content):
        out = _read_xlsx(content)
    else:
        raise Unreadable("只接受 PDF 與 Excel（.xlsx）訂單")
    if not out.lines:
        raise Unreadable("找不到同時有料號與數量欄的表格")
    return out


# ------------------------------------------------------------------- matching

def _squash(part: str) -> str:
    return re.sub(r"[\s\-_./]", "", rules.norm(part)).upper()


@dataclass
class Catalog:
    products: dict[str, Product]
    aliases: dict[tuple[str, str], str]

    @classmethod
    def load(cls, session: Session) -> "Catalog":
        return cls({p.part_no: p for p in session.scalars(select(Product))},
                   {(a.customer, a.customer_part_no): a.part_no for a in session.scalars(select(CustomerPart))})

    def match(self, customer: str | None, written: str) -> tuple[str | None, str, list[str]]:
        """(part number, how it matched, candidates). Only an exact part number, a recorded
        customer part number or a spelling difference in punctuation and case is ever accepted."""
        written = rules.norm(written)
        if written in self.products:
            return written, "exact", []
        if customer and (customer, written) in self.aliases:
            return self.aliases[(customer, written)], "customer_part", []
        same = [p for p in self.products if _squash(p) == _squash(written)] if written else []
        if len(same) == 1:
            return same[0], "normalized", []
        close = difflib.get_close_matches(_squash(written), [_squash(p) for p in self.products], n=3, cutoff=0.75)
        return None, "none", [p for p in self.products if _squash(p) in close]


def _issue(fld: str, code: str, msg: str, blocking: bool) -> dict:
    return {"field": fld, "code": code, "msg": msg, "blocking": blocking}


def _names_agree(written: str, product_name: str) -> bool:
    """Sizes must agree: every number the customer wrote has to be in our product name."""
    if not set(re.findall(r"\d+", rules.norm(written))) <= set(re.findall(r"\d+", product_name)):
        return False
    a, b = _squash(written), _squash(product_name)
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def build_line(catalog: Catalog, customer: str | None, order_date: date | None, header_due, raw: dict) -> dict:
    """Turn one row as written into part number, quantity, due date and a list of issues."""
    issues: list[dict] = []
    written = rules.norm(raw.get("part"))
    part_no, kind, candidates = catalog.match(customer, written)
    product = catalog.products.get(part_no) if part_no else None
    if not product:
        issues.append(_issue("part_no", "no_match", f"商品表查無料號「{written}」" if written else "缺少料號", True))
    else:
        if kind == "normalized":
            issues.append(_issue("part_no", "normalized", f"料號寫法與商品表不同：「{written}」→ {part_no}，請確認", False))
        if not product.active:
            issues.append(_issue("part_no", "inactive", f"料號 {part_no} 已停用", True))
        if raw.get("name") and not _names_agree(str(raw["name"]), product.name):
            issues.append(_issue("part_no", "name_differs",
                                 f"訂單品名「{rules.norm(raw['name'])}」與商品表「{product.name}」不符，請確認料號", False))

    qty, unit, notes = rules.parse_qty(raw.get("qty"))
    unit = (rules.norm(raw.get("unit")) or unit).strip()
    for note in notes:
        issues.append(_issue("qty", "bad_qty", note, True))
    if qty is not None and product and unit.upper() not in COUNT_UNITS and unit != product.unit:
        if unit == product.pack_unit and product.pack_qty:
            issues.append(_issue("qty", "unit_converted",
                                 f"單位換算：{qty} {unit} × {product.pack_qty} = {qty * product.pack_qty} {product.unit}，請確認", False))
            qty = qty * product.pack_qty
        else:
            issues.append(_issue("qty", "unknown_unit", f"單位「{unit}」不是 {part_no} 的單位，數量需人工確認", True))

    due_written = raw.get("due") if raw.get("due") not in (None, "") else header_due
    due, notes = rules.parse_date(due_written, ref=order_date)
    if due is None:
        issues.append(_issue("due_date", "bad_date", notes[0].replace("日期", "交期", 1), True))
    else:
        for note in notes:
            if "推定" in note:
                issues.append(_issue("due_date", "year_inferred", note, False))
        for note in rules.check_due(due, order_date):
            issues.append(_issue("due_date", "bad_date", note, True))

    if product and raw.get("price") not in (None, "") and product.list_price is not None:
        price, _ = rules.parse_money(raw["price"])
        if price is not None and price != product.list_price:
            issues.append(_issue("price", "price_differs",
                                 f"訂單單價 {price} 與商品表牌價 {product.list_price} 不同。本系統不改價，請業務處理", False))
    return {"part_no": part_no, "qty": qty, "due_date": due, "match_kind": kind, "candidates": candidates, "issues": issues}


# ------------------------------------------------------------------ the flow

def import_products(session: Session, user: User, content: bytes) -> int:
    """Product table: 料號, 品名, 單位 required; 包裝單位, 包裝數量, 牌價, 停用 optional. Existing rows are updated."""
    count = 0
    for number, row in records(content, ["料號", "品名", "單位"]):
        part_no = rules.norm(row["料號"])
        pack_qty, price = row.get("包裝數量"), row.get("牌價")
        try:
            values = dict(name=rules.norm(row["品名"]), unit=rules.norm(row["單位"]),
                          pack_unit=rules.norm(row.get("包裝單位")) or None,
                          pack_qty=int(str(pack_qty)) if pack_qty not in (None, "") else None,
                          list_price=rules.parse_money(price)[0] if price not in (None, "") else None,
                          active=rules.norm(row.get("停用")) not in ("是", "Y", "y", "1", "停用"))
        except ValueError as exc:
            raise Refused(f"商品表第 {number} 列無法讀取：{exc}") from exc
        if not part_no or not values["name"] or not values["unit"]:
            raise Refused(f"商品表第 {number} 列缺少料號、品名或單位")
        product = session.get(Product, part_no)
        if product:
            for k, v in values.items():
                if getattr(product, k) != v:
                    base.log_change(session, FLOW, "product", 0, f"{part_no}.{k}", getattr(product, k), v, user)
                    setattr(product, k, v)
        else:
            session.add(Product(part_no=part_no, **values))
        count += 1
    session.commit()
    return count


def import_customer_parts(session: Session, user: User, content: bytes) -> int:
    count = 0
    for number, row in records(content, ["客戶", "客戶料號", "料號"]):
        customer, theirs, ours = (rules.norm(row[k]) for k in ("客戶", "客戶料號", "料號"))
        if not session.get(Product, ours):
            raise Refused(f"客戶料號表第 {number} 列：商品表沒有料號 {ours}")
        found = session.scalar(select(CustomerPart).where(CustomerPart.customer == customer,
                                                          CustomerPart.customer_part_no == theirs))
        if found:
            found.part_no = ours
        else:
            session.add(CustomerPart(customer=customer, customer_part_no=theirs, part_no=ours))
        count += 1
    session.commit()
    return count


def _order_issues(order: Order) -> list[dict]:
    issues = []
    if not order.customer:
        issues.append(_issue("customer", "missing", "缺少客戶名稱", True))
    if not order.po_no:
        issues.append(_issue("po_no", "missing", "缺少客戶訂單編號", True))
    if not order.order_date:
        issues.append(_issue("order_date", "missing", "缺少或無法辨識訂單日期", True))
    return issues


def _mark_duplicate(session: Session, order: Order) -> None:
    first = None
    if order.customer and order.po_no:
        first = session.scalar(select(Order).where(
            Order.customer == order.customer, Order.po_no == order.po_no, Order.id != order.id,
            Order.status != "duplicate").order_by(Order.id))
    if first:
        order.status, order.duplicate_of = "duplicate", first.id
    elif order.status == "duplicate":
        order.status, order.duplicate_of = "needs_review", None


def ingest(session: Session, user: User | None, filename: str, content: bytes,
           data_class: str = "synthetic", consent_id: int | None = None) -> tuple[Order, bool]:
    """Read one order file. Returns (order, is_new); the same file twice gives the first order back."""
    source, is_new = base.keep_source(session, user, FLOW, "order", filename, content, data_class, consent_id)
    if not is_new:
        return session.scalar(select(Order).where(Order.source_id == source.id)), False
    order = Order(source_id=source.id, status="needs_review", issues=[])
    session.add(order)
    try:
        found = extract(content)
    except Unreadable as exc:
        order.status = "handoff"
        session.flush()
        base.hand_off(session, FLOW, "order", order.id, "unreadable", str(exc))
        session.commit()
        return order, True
    source.text = found.text
    order.customer = rules.norm(found.meta.get("customer")) or None
    order.po_no = rules.norm(found.meta.get("po_no")) or None
    order.order_date, _ = rules.parse_date(found.meta.get("order_date"))
    order.issues = _order_issues(order)
    catalog = Catalog.load(session)
    for number, raw in enumerate(found.lines, start=1):
        evidence = raw.pop("evidence")
        built = build_line(catalog, order.customer, order.order_date, found.meta.get("due"), raw)
        order.lines.append(OrderLine(line_no=number, raw={k: str(v) for k, v in raw.items()}, evidence=evidence, **built))
    session.flush()
    _mark_duplicate(session, order)
    session.commit()
    return order, True


def open_order(session: Session, order: Order) -> None:
    """Start the review clock the first time a person opens the order."""
    if order.opened_at is None and order.status == "needs_review":
        order.opened_at = now()
        session.commit()


def blocking(order: Order) -> list[dict]:
    return [i for i in order.issues if i["blocking"]] + [i for l in order.lines for i in l.issues if i["blocking"]]


def _editable(order: Order) -> None:
    if order.status in ("confirmed", "exported"):
        raise Refused("訂單已確認，不能再修改")


def edit_order(session: Session, user: User, order_id: int, fld: str, value: str) -> None:
    order = session.get(Order, order_id)
    _editable(order)
    value = rules.norm(value)
    if fld == "order_date":
        new, notes = rules.parse_date(value)
        if new is None:
            raise Refused(notes[0])
    elif fld in ("customer", "po_no"):
        if not value:
            raise Refused("不能留白")
        new = value
    else:
        raise Refused(f"不能修改欄位 {fld}")
    base.log_change(session, FLOW, "order", order.id, fld, getattr(order, fld), new, user)
    setattr(order, fld, new)
    order.issues = _order_issues(order)
    if order.status != "handoff":
        _mark_duplicate(session, order)
    session.commit()


def edit_line(session: Session, user: User, line_id: int, fld: str, value: str) -> None:
    """A person sets a part number, quantity or due date. The value passes the same rules as the program's own."""
    line = session.get(OrderLine, line_id)
    order = session.get(Order, line.order_id)
    _editable(order)
    extra: list[dict] = []
    if fld == "part_no":
        product = session.get(Product, rules.norm(value))
        if not product:
            raise Refused(f"商品表沒有料號「{rules.norm(value)}」")
        if not product.active:
            raise Refused(f"料號 {product.part_no} 已停用")
        new = product.part_no
        unit = rules.norm(line.raw.get("unit")) or rules.parse_qty(line.raw.get("qty"))[1]
        if new != line.part_no and unit.upper() not in COUNT_UNITS and unit != product.unit:
            extra.append(_issue("qty", "recheck", f"料號已改，原單位「{unit}」的數量請重新確認", True))
        line.match_kind, line.candidates = "manual", []
    elif fld == "qty":
        new, _, notes = rules.parse_qty(value)
        if new is None:
            raise Refused(notes[0])
    elif fld == "due_date":
        new, notes = rules.parse_date(value, ref=order.order_date)
        if new is None:
            raise Refused(notes[0].replace("日期", "交期", 1))
        if problems := rules.check_due(new, order.order_date):
            raise Refused(problems[0])
    else:
        raise Refused(f"不能修改欄位 {fld}")
    base.log_change(session, FLOW, "order_line", line.id, fld, getattr(line, fld), new, user,
                    note=f"訂單 {order.id} 第 {line.line_no} 項")
    setattr(line, fld, new)
    line.issues = [i for i in line.issues if i["field"] != fld and not (fld == "qty" and i["code"] == "recheck")] + extra
    session.commit()


def release_duplicate(session: Session, user: User, order_id: int, reason: str) -> None:
    """A person states that a repeated order number is a separate order after all."""
    order = session.get(Order, order_id)
    if order.status != "duplicate":
        return
    if not reason.strip():
        raise Refused("解除重複標記必須寫明原因")
    base.log_change(session, FLOW, "order", order.id, "status", "duplicate", "needs_review", user, note=reason.strip())
    order.status = "needs_review"
    session.commit()


def confirm(session: Session, user: User, order_id: int) -> None:
    order = session.get(Order, order_id)
    if order.status != "needs_review":
        raise Refused({"duplicate": "這張訂單與另一張的客戶與單號相同，不能確認",
                       "handoff": "這張訂單需人工輸入，不能在此確認"}.get(order.status, "訂單已確認"))
    if not order.lines:
        raise Refused("訂單沒有明細")
    if left := blocking(order):
        raise Refused(f"還有 {len(left)} 個必須處理的問題：{left[0]['msg']}")
    notices = sum(len(l.issues) for l in order.lines)
    order.status, order.confirmed_at, order.confirmed_by = "confirmed", now(), user.id
    base.log_change(session, FLOW, "order", order.id, "status", "needs_review", "confirmed", user,
                    note=f"確認時有 {notices} 個提醒" if notices else "")
    session.commit()


EXPORT_HEADER = ["客戶", "客戶單號", "訂單日期", "項次", "料號", "品名", "數量", "單位", "交期"]


def export_confirmed(session: Session, user: User) -> ExportBatch:
    """Put every confirmed, not yet exported order into one CSV. An order is exported once;
    the row lock makes two people pressing the button at once get different orders or none."""
    orders = list(session.scalars(select(Order).where(Order.status == "confirmed", Order.export_batch_id.is_(None))
                                  .order_by(Order.id).with_for_update()))
    if not orders:
        session.rollback()
        raise Refused("沒有已確認而尚未匯出的訂單")
    products = Catalog.load(session).products
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(EXPORT_HEADER)
    for order in orders:
        for line in order.lines:
            p = products[line.part_no]
            writer.writerow([order.customer, order.po_no, order.order_date, line.line_no, line.part_no, p.name,
                             int(line.qty), p.unit, line.due_date])
    batch = ExportBatch(created_by=user.id, order_count=len(orders), csv=out.getvalue())
    session.add(batch)
    session.flush()
    for order in orders:
        order.status, order.export_batch_id = "exported", batch.id
        base.log_change(session, FLOW, "order", order.id, "status", "confirmed", "exported", user, note=f"匯出批次 {batch.id}")
    session.commit()
    return batch
