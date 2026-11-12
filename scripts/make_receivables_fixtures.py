"""Write the receivables test set: one month-end export (30 Nov 2026), a later export in
which two items have been paid, 16 customer mails, and a manifest of what is true.
Everything is invented; the mail domains use the reserved .example ending.
"""
import csv
import json
import random
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent.parent / "fixtures" / "receivables"
AS_OF = date(2026, 11, 30)
rng = random.Random(1130)
CUSTOMERS = [("禾臻精密股份有限公司", "ap@hezhen.example"), ("鉅暘機械有限公司", "account@juyang.example"),
             ("昇揚自動化股份有限公司", "finance@shengyang.example"), ("旭海五金行", "xuhai.hardware@mail.example"),
             ("柏昕科技股份有限公司", "ap@boxin.example"), ("嘉東工業有限公司", "acct@jiadong.example"),
             ("宏碩模具有限公司", "pay@hongshuo.example"), ("立岡電機股份有限公司", "ap@ligang.example"),
             ("永翊包裝材料行", "yongyi.pack@mail.example"), ("晟田金屬有限公司", "finance@shengtian.example"),
             ("群嶧機電工程有限公司", "ap@qunyi.example"), ("泓洋貿易有限公司", "account@hongyang.example")]
HEADER = ["客戶", "發票號碼", "發票日期", "到期日", "金額", "已收金額", "聯絡信箱", "簽收單"]

items = []
for ci, (customer, mail) in enumerate(CUSTOMERS):
    for _ in range(5):
        invoice_date = date(2026, 8, 10) + timedelta(days=rng.randrange(105))
        amount = rng.randrange(8, 400) * 525
        roll = rng.random()
        received = amount if roll < 0.3 else rng.randrange(1, 9) * amount // 10 if roll < 0.4 else 0
        items.append(dict(customer=customer, invoice_no=f"{rng.choice('ABCDEFGH')}{rng.choice('KLMNPQ')}{rng.randrange(10**7, 10**8)}",
                          invoice_date=invoice_date, due_date=invoice_date + timedelta(days=30), amount=amount,
                          received=received, email=mail, receipt="有", planted=[]))
# make sure the customers the mails are about have something overdue and unpaid to talk about
for ci in range(12):
    mine = [i for i in items if i["customer"] == CUSTOMERS[ci][0]]
    mine[0].update(invoice_date=date(2026, 9, 1) + timedelta(days=ci * 3), received=0)
    mine[0]["due_date"] = mine[0]["invoice_date"] + timedelta(days=30)
    mine[1].update(invoice_date=date(2026, 10, 5) + timedelta(days=ci), received=0)
    mine[1]["due_date"] = mine[1]["invoice_date"] + timedelta(days=30)


def first(ci, n=0):
    return [i for i in items if i["customer"] == CUSTOMERS[ci][0]][n]


# planted data problems, on rows the mails do not touch (index 2 to 4 of a customer)
first(0, 2).update(invoice_no="", planted=["no_invoice_no"]); first(0, 2).update(received=0)
first(1, 2).update(invoice_no="", planted=["no_invoice_no"])
first(2, 2).update(email="", planted=["no_email"], received=0, invoice_date=date(2026, 9, 20), due_date=date(2026, 10, 20))
first(3, 2).update(email="", planted=["no_email"])
for ci in (4, 5, 6, 7, 8):
    first(ci, 2).update(receipt="無", planted=["no_receipt"])
first(9, 2).update(due_date=first(9, 2)["invoice_date"] - timedelta(days=3), received=0, planted=["due_before_invoice"])
first(10, 2).update(received=first(10, 2)["amount"] + 1000, planted=["received_over_amount"])
first(11, 2).update(amount="12,34", received=0, planted=["bad_amount"])

MAILS = []


def mail(ci, subject, body, truth, about=(), whole_customer=False, sender=None, day=20, message_id=None):
    MAILS.append(dict(sender=sender or CUSTOMERS[ci][1], subject=subject, body=body, truth=truth,
                      invoices=[i["invoice_no"] for i in about], customer=CUSTOMERS[ci][0] if whole_customer or about else None,
                      whole_customer=whole_customer, day=day, message_id=message_id or f"<m{len(MAILS) + 1}@{(sender or CUSTOMERS[ci][1]).split('@')[1]}>"))


a = first
mail(0, f"發票 {a(0)['invoice_no']} 數量問題", f"您好，發票 {a(0)['invoice_no']} 的出貨數量與我們驗收的數量不符，少了兩箱。\n請先確認後再請款。", "dispute", [a(0)])
mail(1, "關於貨款", f"{a(1)['invoice_no']} 這批軸承有三顆有瑕疵，我們要求退貨或折讓，處理完再付款。", "dispute", [a(1)])
mail(2, f"RE: 對帳單", f"發票{a(2)['invoice_no']}金額有誤，單價和報價單不一樣，請重新開立。", "dispute", [a(2)])
mail(3, "貨還沒到", f"您好，{a(3)['invoice_no']} 這張的貨我們到現在都沒有收到貨，麻煩查一下物流。", "dispute", [a(3)])
mail(4, "上個月的帳", "上個月那幾張發票我們覺得有爭議，主管要求先暫停付款，等業務來談。", "dispute", whole_customer=True)
mail(5, f"已付款通知 {a(5)['invoice_no']}", f"發票 {a(5)['invoice_no']} 已於 11/18 匯款，匯款單如附件，請查收。", "claims_paid", [a(5)], day=19)
mail(6, "付款通知", f"您好，{a(6)['invoice_no']} 款項已匯出，請確認入帳。", "claims_paid", [a(6)])
mail(7, f"{a(7)['invoice_no']} 已付清", f"這張發票我們已經付款了，用的是支票，11/10 寄出。", "claims_paid", [a(7)])
mail(8, "貨款", "會計說上週已經匯款了，是幾張一起匯的，麻煩對一下帳。", "claims_paid", whole_customer=True)
mail(9, f"RE: 催款 {a(9)['invoice_no']}", f"不好意思，{a(9)['invoice_no']} 尚未付款，預計下週五匯款。", "promise", [a(9)])
mail(10, "付款安排", f"發票 {a(10)['invoice_no']} 將於 12/10 付款，請見諒。", "promise", [a(10)])
mail(11, "索取型錄", "請寄一份最新的產品型錄給我們，謝謝。", "other", whole_customer=True)
mail(0, f"請確認 {a(0, 1)['invoice_no']}", f"想確認貴司是否收到我們上週針對 {a(0, 1)['invoice_no']} 的匯款？", "claims_paid", [a(0, 1)])
mail(6, "帳款", "我是宏碩模具的採購，老闆說那筆貨款已匯款，請不要再催了。", "claims_paid", sender="chen.personal@webmail.example")
MAILS[-1].update(customer=None, belongs_to=CUSTOMERS[6][0])
mail(1, "兩張發票的問題", f"{a(1)['invoice_no']} 和 {a(1, 1)['invoice_no']} 都有短少，請一併處理。", "dispute", [a(1), a(1, 1)])
mail(5, f"已付款通知 {a(5)['invoice_no']}", f"發票 {a(5)['invoice_no']} 已於 11/18 匯款，匯款單如附件，請查收。", "claims_paid", [a(5)], day=19,
     message_id=MAILS[5]["message_id"])        # the same message exported twice


def write_export(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for i in rows:
            w.writerow([i["customer"], i["invoice_no"], f"{i['invoice_date']:%Y/%m/%d}", f"{i['due_date']:%Y/%m/%d}",
                        i["amount"], i["received"], i["email"], i["receipt"]])


def main():
    (ROOT / "mails").mkdir(parents=True, exist_ok=True)
    for old in (ROOT / "mails").iterdir():
        old.unlink()
    write_export(ROOT / "ar-2026-11-30.csv", items)
    held_invoices = {n for m in MAILS for n in m["invoices"]}
    held_customers = {m["customer"] for m in MAILS if m["whole_customer"]} | {m["belongs_to"] for m in MAILS if m.get("belongs_to")}
    truth = []
    for i in items:
        sound = not ({"due_before_invoice", "received_over_amount", "bad_amount"} & set(i["planted"]))
        overdue = sound and i["due_date"] < AS_OF and i["received"] < i["amount"]
        held = i["invoice_no"] in held_invoices or i["customer"] in held_customers
        truth.append(dict(customer=i["customer"], invoice_no=i["invoice_no"] or None, overdue=overdue,
                          days=(AS_OF - i["due_date"]).days if overdue else 0, held=held,
                          may_remind=overdue and not held and bool(i["invoice_no"]) and bool(i["email"]), planted=i["planted"]))
    # a later export: two items that could be reminded on 30 Nov have been paid by 5 Dec
    paid_later = [t["invoice_no"] for t in truth if t["may_remind"]][:2]
    later = [dict(i, received=i["amount"]) if i["invoice_no"] in paid_later else i for i in items]
    write_export(ROOT / "ar-2026-12-05.csv", later)
    out = []
    for n, m in enumerate(MAILS, 1):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = m["sender"], "ar@our-company.example", m["subject"], m["message_id"]
        msg["Date"] = datetime(2026, 11, m["day"], 9, 30, tzinfo=timezone(timedelta(hours=8)))
        msg.set_content(m["body"] + "\n\n（虛構測試資料）")
        name = f"mail-{n:02d}.eml"
        (ROOT / "mails" / name).write_bytes(bytes(msg))
        out.append(dict(file=name, truth_pile=m["truth"], invoices=m["invoices"], customer=m["customer"],
                        belongs_to=m.get("belongs_to"), repeat_of_message=m["message_id"] if n == len(MAILS) else None))
    (ROOT / "manifest.json").write_text(json.dumps(dict(
        note="Invented data. `held` means a customer mail touches the item, so no reminder may go out until a person has read it.",
        as_of=AS_OF.isoformat(), paid_in_later_export=paid_later, items=truth, mails=out), ensure_ascii=False, indent=1))
    print(f"{len(items)} items, {sum(t['overdue'] for t in truth)} overdue, {sum(t['may_remind'] for t in truth)} may be reminded, "
          f"{sum(t['held'] for t in truth)} held, {len(MAILS)} mails")


if __name__ == "__main__":
    main()
