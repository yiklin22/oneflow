"""Write the order-entry test set: a product table, a customer part table, 50 order
files (PDF and Excel, five layouts) and a manifest saying what each line should become.

Everything is invented. The difficulties are planted on purpose and listed per line
in the manifest, so the acceptance run can say which ones the program caught.
Needs a Traditional Chinese TrueType font (Noto Sans TC, SIL Open Font License).
"""
import csv
import io
import json
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).parent.parent / "fixtures" / "orders"
FONT = os.environ.get("ONEFLOW_FONT", "/mnt/c/Windows/Fonts/NotoSansTC-VF.ttf")
rng = random.Random(20261112)

# ------------------------------------------------------------------ products
PRODUCTS = []


def add(part_no, name, unit, pack_unit, pack_qty, price, active=True):
    PRODUCTS.append(dict(part_no=part_no, name=name, unit=unit, pack_unit=pack_unit, pack_qty=pack_qty,
                         price=price, active=active))


for d, lengths in ((3, (8, 12)), (4, (12, 16, 20)), (5, (16, 25)), (6, (20, 30)), (8, (25, 40))):
    for length in lengths:
        add(f"SC-M{d}{length:03d}-SS", f"不鏽鋼六角螺絲 M{d}×{length}", "支", "盒", 100, round(0.8 + d * 0.35 + length * 0.03, 2))
for d in (3, 4, 5, 6, 8, 10):
    add(f"NT-M{d}-ZN", f"鍍鋅六角螺帽 M{d}", "個", "盒", 200, round(0.3 + d * 0.12, 2))
    add(f"WS-M{d}-SS", f"不鏽鋼平墊圈 M{d}", "個", "包", 500, round(0.15 + d * 0.05, 2))
for n in (6200, 6201, 6202, 6203, 6204, 6205):
    add(f"BR-{n}-2RS", f"深溝滾珠軸承 {n}-2RS", "個", "箱", 20, 38 + (n - 6200) * 6)
add("BR-6200-ZZ", "深溝滾珠軸承 6200-ZZ", "個", "箱", 20, 35, active=False)
for n in (10, 16, 20, 25, 30):
    add(f"OR-P{n}-NBR", f"O型環 P{n} NBR", "個", "包", 100, round(1.2 + n * 0.08, 2))
for length in (100, 150, 200, 300):
    add(f"CT-{length}-BK", f"尼龍束帶 {length}mm 黑", "條", "包", 100, round(0.4 + length * 0.004, 2))
BY_PART = {p["part_no"]: p for p in PRODUCTS}
ACTIVE = [p for p in PRODUCTS if p["active"]]

CUSTOMERS = ["禾臻精密股份有限公司", "鉅暘機械有限公司", "昇揚自動化股份有限公司", "旭海五金行",
             "柏昕科技股份有限公司", "嘉東工業有限公司"]
ALIASES = {}          # (customer, their number) -> ours
for i, p in enumerate(ACTIVE[::2]):
    ALIASES[(CUSTOMERS[1], f"H-{10000 + i * 7}")] = p["part_no"]
for i, p in enumerate(ACTIVE[1::2]):
    ALIASES[(CUSTOMERS[2], f"KY{i + 1:04d}A")] = p["part_no"]
THEIRS = {(c, ours): theirs for (c, theirs), ours in ALIASES.items()}

# ----------------------------------------------------------- planted trouble
PLANTS = (["normalized"] * 8 + ["typo"] * 6 + ["wrong_valid"] * 3 + ["inactive"] * 2 + ["pack"] * 10
          + ["fullwidth"] * 4 + ["bad_qty"] * 2 + ["dozen"] * 2 + ["no_due"] * 4 + ["due_before"] * 2
          + ["bad_date"] * 2 + ["price"] * 4)
rng.shuffle(PLANTS)
LAYOUTS = "ABCDE"


def squash(s):
    return "".join(ch for ch in s if ch not in " -_./").upper()


def allowed(plant, layout, customer, product):
    aliased = (customer, product["part_no"]) in THEIRS
    if plant in ("normalized", "wrong_valid", "inactive") and aliased:
        return False
    if plant in ("no_due", "due_before", "bad_date") and layout == "E":   # E has one due date in the header
        return False
    if plant == "due_before" and layout == "D":                          # D writes no year
        return False
    if plant == "price":
        return layout == "B"
    if plant == "wrong_valid":
        return product["part_no"].startswith("SC-")
    return True


def make_order(index):
    customer = CUSTOMERS[index % len(CUSTOMERS)]
    layout = "C" if customer in (CUSTOMERS[1], CUSTOMERS[2]) and index % 2 else LAYOUTS[index % 5]
    order_date = date(2026, 11, 2) + timedelta(days=rng.randrange(9))
    po_no = f"{'PO' if index % 2 else 'P'}{order_date:%y%m}-{1000 + index * 3}"
    header_due = order_date + timedelta(days=rng.randrange(10, 40))
    lines = []
    for product in rng.sample(ACTIVE, rng.randrange(2, 7)):
        qty = rng.choice([50, 100, 200, 300, 500, 1000, 1200, 2000, 5000])
        due = header_due if layout == "E" else order_date + timedelta(days=rng.randrange(7, 45))
        line = dict(product=product, part=THEIRS.get((customer, product["part_no"]), product["part_no"]),
                    name=product["name"], qty=f"{qty:,}", unit=product["unit"], due=due, price=product["price"],
                    truth=dict(part_no=product["part_no"], qty=qty, due=due.isoformat()), planted=[])
        if PLANTS and rng.random() < 0.3 and allowed(PLANTS[-1], layout, customer, product):
            plant(line, PLANTS.pop(), order_date)
        lines.append(line)
    return dict(customer=customer, layout=layout, order_date=order_date, po_no=po_no, header_due=header_due, lines=lines)


def plant(line, kind, order_date):
    p, truth = line["product"], line["truth"]
    line["planted"].append(kind)
    if kind == "normalized":
        line["part"] = p["part_no"].lower() if rng.random() < 0.5 else p["part_no"].replace("-", " ")
    elif kind == "typo":
        for digit in "7391":
            written = line["part"][:-4] + digit + line["part"][-3:] if p["part_no"] == line["part"] else line["part"][:-1] + digit
            if squash(written) not in map(squash, BY_PART) and written not in {t for _, t in ALIASES}:
                line["part"] = written
                break
    elif kind == "wrong_valid":        # a real part number of another size; the name still says the intended one
        family = [q for q in ACTIVE if q["part_no"][:5] == p["part_no"][:5] and q is not p]
        line["part"] = rng.choice(family)["part_no"]
    elif kind == "inactive":
        line["part"], line["name"] = "BR-6200-ZZ", BY_PART["BR-6200-ZZ"]["name"]
        truth["part_no"] = None
    elif kind == "pack":
        packs = rng.choice([2, 5, 10, 20])
        line["qty"], line["unit"], truth["qty"] = str(packs), p["pack_unit"], packs * p["pack_qty"]
    elif kind == "fullwidth":
        line["qty"] = str(truth["qty"]).translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    elif kind == "bad_qty":
        line["qty"], truth["qty"] = "1,20", None
    elif kind == "dozen":
        line["qty"], line["unit"], truth["qty"] = "10", "打", None
    elif kind == "no_due":
        line["due"], truth["due"] = None, None
    elif kind == "due_before":
        line["due"], truth["due"] = order_date - timedelta(days=5), None
    elif kind == "bad_date":
        line["due"], truth["due"] = "0230", None
    elif kind == "price":
        line["price"] = round(p["price"] * 0.9, 2)


# ------------------------------------------------------------------- drawing
def roc(d, long=False):
    return f"民國{d.year - 1911}年{d.month}月{d.day}日" if long else f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def due_text(due, style):
    if due is None:
        return ""
    if due == "0230":
        return {"iso": "2026/02/30", "roc": "116/02/30", "md": "2/30"}[style]
    return {"iso": f"{due:%Y/%m/%d}", "roc": roc(due), "md": f"{due.month}/{due.day}"}[style]


def pdf(title, meta, columns, rows, footer=""):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)

    def put(x, y, text, align="l"):
        (c.drawRightString if align == "r" else c.drawString)(x, y, str(text))

    c.setFont("TC", 16)
    put(50, 795, title)
    c.setFont("TC", 10.5)
    for row, (x, text) in meta:
        put(x, 768 - 18 * row, text)
    y = 700
    for label, x, align in columns:
        put(x, y, label, align)
    c.line(50, y - 5, 550, y - 5)
    for cells in rows:
        y -= 20
        for (_, x, align), cell in zip(columns, cells):
            if cell not in (None, ""):
                put(x, y, cell, align)
    put(50, y - 32, "備註：虛構測試資料，非真實訂單。" + footer)
    c.save()
    return buf.getvalue()


def render(order, layout=None, footer=""):
    layout = layout or order["layout"]
    L, c, po, od = order["lines"], order["customer"], order["po_no"], order["order_date"]
    if layout == "A":
        rows = [(i, l["part"], l["name"], l["qty"], l["unit"], due_text(l["due"], "iso")) for i, l in enumerate(L, 1)]
        return "pdf", pdf("採購單", [(0, (50, f"客戶：{c}")), (0, (360, f"採購單號：{po}")), (1, (50, f"訂單日期：{od:%Y/%m/%d}"))],
                          [("項次", 50, "l"), ("料號", 85, "l"), ("品名規格", 200, "l"), ("數量", 370, "l"), ("單位", 420, "l"), ("交期", 470, "l")],
                          rows, footer)
    if layout == "B":
        rows = [(l["part"], l["name"], f"{l['qty']} {l['unit'] if 'pack' in l['planted'] or 'dozen' in l['planted'] else 'PCS'}",
                 f"{l['price']:.2f}", due_text(l["due"], "roc")) for l in L]
        return "pdf", pdf("訂 購 單", [(0, (50, f"訂購單位：{c}")), (1, (50, f"訂單編號：{po}")), (1, (360, f"日期：{roc(od)}"))],
                          [("品號", 50, "l"), ("品名", 170, "l"), ("訂購量", 400, "r"), ("單價", 455, "r"), ("交貨日", 545, "r")],
                          rows, footer)
    if layout == "E":
        rows = [(i, l["part"], l["name"], l["qty"], l["unit"]) for i, l in enumerate(L, 1)]
        return "pdf", pdf("採購訂單", [(0, (50, f"買方：{c}")), (0, (360, f"訂單號碼：{po}")),
                                    (1, (50, f"採購日期：{roc(od, long=True)}")), (1, (360, f"交期：{roc(order['header_due'], long=True)}"))],
                          [("項次", 50, "l"), ("料號", 90, "l"), ("品名", 210, "l"), ("數量", 420, "r"), ("單位", 440, "l")],
                          rows, footer)
    book = openpyxl.Workbook()
    book.properties.created = book.properties.modified = datetime(2026, 11, 12)
    ws = book.active
    if layout == "C":
        ws.append(["訂購單"])
        ws.append(["客戶名稱", c])
        ws.append(["訂單號碼", po])
        ws.append(["訂單日期", datetime(od.year, od.month, od.day)])
        ws.append([])
        ws.append(["客戶料號", "品名", "數量", "單位", "需求日"])
        for l in L:
            due = l["due"]
            due = datetime(due.year, due.month, due.day) if isinstance(due, date) else due_text(due, "iso")
            qty = int(l["qty"].replace(",", "")) if l["qty"].replace(",", "").isascii() and "," not in l["qty"][-3:] else l["qty"]
            ws.append([l["part"], l["name"], qty, l["unit"], due])
    else:
        ws.append(["PURCHASE ORDER"])
        ws.append([f"Customer: {c}"])
        ws.append([f"PO No: {po}"])
        ws.append([f"Date: {od:%Y/%m/%d}"])
        ws.append([])
        ws.append(["Item", "Part No", "Description", "Qty", "Delivery"])
        for i, l in enumerate(L, 1):
            unit = f" {l['unit']}" if "pack" in l["planted"] or "dozen" in l["planted"] else ""
            ws.append([i, l["part"], l["name"], l["qty"] + unit, due_text(l["due"], "md")])
    ws.append([])
    ws.append(["備註：虛構測試資料，非真實訂單。" + footer])
    buf = io.BytesIO()
    book.save(buf)
    return "xlsx", buf.getvalue()


def blank_scan():
    """A PDF with drawing but no text, standing in for a scanned or faxed order."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    for y in range(500, 780, 20):
        c.line(60, y, 60 + rng.randrange(200, 480), y)
    c.rect(50, 480, 500, 320)
    c.save()
    return buf.getvalue()


def main():
    pdfmetrics.registerFont(TTFont("TC", FONT))
    (ROOT / "files").mkdir(parents=True, exist_ok=True)
    for old in (ROOT / "files").iterdir():
        old.unlink()
    with open(ROOT / "products.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["料號", "品名", "單位", "包裝單位", "包裝數量", "牌價", "停用"])
        for p in PRODUCTS:
            w.writerow([p["part_no"], p["name"], p["unit"], p["pack_unit"], p["pack_qty"], p["price"], "" if p["active"] else "是"])
    with open(ROOT / "customer_parts.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["客戶", "客戶料號", "料號"])
        for (customer, theirs), ours in ALIASES.items():
            w.writerow([customer, theirs, ours])

    orders = [make_order(i) for i in range(47)]
    manifest, number = [], 0

    def emit(order, expect, layout=None, footer=""):
        nonlocal number
        number += 1
        kind, content = render(order, layout, footer)
        name = f"order-{number:02d}.{kind}"
        (ROOT / "files" / name).write_bytes(content)
        manifest.append(dict(file=name, layout=layout or order["layout"], customer=order["customer"], po_no=order["po_no"],
                             order_date=order["order_date"].isoformat(), expect=expect,
                             lines=[dict(line_no=i, **l["truth"], planted=l["planted"]) for i, l in enumerate(order["lines"], 1)]))

    for order in orders:
        emit(order, "review")
    emit(orders[5], "duplicate", layout="C" if orders[5]["layout"] != "C" else "A", footer="（改以 Excel 重送）")
    emit(orders[12], "duplicate", footer="（重新傳送）")
    number += 1
    (ROOT / "files" / f"order-{number:02d}.pdf").write_bytes(blank_scan())
    manifest.append(dict(file=f"order-{number:02d}.pdf", layout="scan", expect="handoff", lines=[]))
    (ROOT / "manifest.json").write_text(json.dumps(dict(
        note="Invented data. `planted` lists the difficulty put on a line on purpose; a null truth value means "
             "the order does not say and a person has to decide.", unused_plants=PLANTS, orders=manifest),
        ensure_ascii=False, indent=1))
    lines = sum(len(o["lines"]) for o in manifest)
    print(f"{len(manifest)} files, {lines} lines, {sum(bool(l['planted']) for o in manifest for l in o['lines'])} planted, "
          f"{len(PLANTS)} plants unused")


if __name__ == "__main__":
    main()
