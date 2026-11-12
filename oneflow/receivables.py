"""Receivables: find overdue items and missing pieces, sort customer mail into
piles, and draft reminders that go out only after a person approves each one.

The program never decides that money is owed. Any mail from a customer stops
reminders for the items it touches until a person has read it; the keywords only
choose which pile the mail goes on. Disputed amounts are never chased.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import os
import re
from datetime import date, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import base, rules
from .base import Refused
from .models import ArItem, Handoff, Mail, MailSignal, Reminder, User, now
from .tables import Unreadable, records

FLOW = "receivables"
REQUIRED = ["客戶", "發票號碼", "發票日期", "到期日", "金額", "已收金額"]
INVOICE_NO = re.compile(r"[A-Z]{2}-?\d{8}")        # Taiwan uniform invoice number
PILES = {                                           # first match wins, top to bottom
    "dispute": re.compile(r"不符|短少|缺少|瑕疵|不良|退貨|折讓|有誤|錯誤|重複開立|重複請款|(未|沒有?|尚未)收到(貨|商品|產品)|爭議|異議|破損|無法接受"),
    "claims_paid": re.compile(r"已(經)?(付款|匯款|轉帳|付清|匯出|電匯|支付)|款項已|已於.{0,12}(付款|匯款|轉帳|匯出)|匯款(單|水單|證明)|付款證明"),
    "promise": re.compile(r"(預計|將於|會在|下週|下周|月底|月初|下個月).{0,15}(付款|匯款|轉帳|支付)"),
}
HOLD_LABEL = {"dispute": "爭議：不催收", "claims_paid": "客戶稱已付款：待核對", "promise": "客戶承諾付款日",
              "other": "有來信待閱讀"}
RANK = ["dispute", "claims_paid", "promise", "other"]


def balance(item: ArItem) -> Decimal | None:
    return None if item.amount is None or item.received is None else item.amount - item.received


def days_overdue(item: ArItem, as_of: date) -> int:
    """Days past the due date with money still open; 0 when not overdue or when the row's own
    dates or amounts fail the rules, because then it is not knowable."""
    open_amount = balance(item)
    if any(i["kind"] == "rule" and i["field"] != "invoice_no" for i in item.issues) or item.due_date is None or open_amount is None or open_amount <= 0 or item.due_date >= as_of:
        return 0
    return (as_of - item.due_date).days


def _issue(fld: str, kind: str, msg: str) -> dict:
    return {"field": fld, "kind": kind, "msg": msg}      # kind: rule (data is wrong) | missing (a piece is absent)


def _read_row(row: dict) -> dict:
    issues, out = [], {"customer": rules.norm(row.get("客戶"))}
    invoice_no = rules.norm(row.get("發票號碼")).upper()
    if not invoice_no:
        issues.append(_issue("invoice_no", "missing", "缺件：發票號碼"))
    elif not INVOICE_NO.fullmatch(invoice_no):
        issues.append(_issue("invoice_no", "rule", f"發票號碼格式不符（應為兩個英文字母加八位數字）：{invoice_no}"))
    out["invoice_no"] = invoice_no.replace("-", "") or None
    for fld, label in (("invoice_date", "發票日期"), ("due_date", "到期日")):
        out[fld], notes = rules.parse_date(row.get(label))
        if out[fld] is None:
            issues.append(_issue(fld, "missing" if "缺少" in notes[0] else "rule", f"{label}：{notes[0]}"))
    if out["invoice_date"] and out["due_date"] and out["due_date"] < out["invoice_date"]:
        issues.append(_issue("due_date", "rule", "到期日早於發票日期"))
    for fld, label in (("amount", "金額"), ("received", "已收金額")):
        blank = row.get(label) in (None, "")
        out[fld], notes = (Decimal(0), []) if blank and fld == "received" else rules.parse_money(row.get(label))
        if out[fld] is None:
            issues.append(_issue(fld, "rule", f"{label}：{notes[0]}"))
    if out["amount"] is not None and out["amount"] <= 0:
        issues.append(_issue("amount", "rule", "金額必須大於零"))
    if out["received"] is not None and (out["received"] < 0 or (out["amount"] is not None and out["received"] > out["amount"])):
        issues.append(_issue("received", "rule", "已收金額為負或超過發票金額"))
    mail = rules.norm(row.get("聯絡信箱")).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", mail):
        issues.append(_issue("contact_email", "missing", "缺件：聯絡信箱" if not mail else f"聯絡信箱格式不符：{mail}"))
        mail = ""
    out["contact_email"] = mail or None
    receipt = rules.norm(row.get("簽收單"))
    out["has_receipt"] = True if receipt in ("有", "Y", "y", "是") else False if receipt in ("無", "N", "n", "否") else None
    if "簽收單" in row and out["has_receipt"] is not True:
        issues.append(_issue("has_receipt", "missing", "缺件：簽收單"))
    out["issues"] = issues
    return out


def import_export(session: Session, user: User, filename: str, content: bytes,
                  data_class: str = "synthetic", consent_id: int | None = None) -> dict:
    """Load a receivables export (CSV or Excel). The same file twice changes nothing; a later
    export updates the items it names and cancels pending reminders for anything now paid."""
    source, is_new = base.keep_source(session, user, FLOW, "export", filename, content, data_class, consent_id)
    if not is_new:
        return {"new": 0, "updated": 0, "repeat": True}
    try:
        rows = records(content, REQUIRED)
    except Unreadable as exc:
        base.hand_off(session, FLOW, "source", source.id, "unreadable", str(exc))
        session.commit()
        raise Refused(str(exc)) from exc
    counts = {"new": 0, "updated": 0, "repeat": False}
    for number, row in rows:
        values = _read_row(row)
        if not values["customer"]:
            continue
        row_text = "｜".join(f"{k}：{'' if v is None else v}" for k, v in row.items())
        key = (f"{values['customer']}|{values['invoice_no']}" if values["invoice_no"]
               else "row|" + hashlib.sha256(row_text.encode()).hexdigest()[:24])
        item = session.scalar(select(ArItem).where(ArItem.key == key))
        if item is None:
            session.add(ArItem(key=key, source_id=source.id, row_no=number, row_text=row_text, **values))
            counts["new"] += 1
            continue
        changed = [fld for fld, new in values.items() if fld != "issues" and getattr(item, fld) != new]
        for fld in changed:
            base.log_change(session, FLOW, "ar_item", item.id, fld, getattr(item, fld), values[fld], None, note=f"匯入 {filename}")
        for fld, new in values.items():
            setattr(item, fld, new)
        item.source_id, item.row_no, item.row_text = source.id, number, row_text
        counts["updated"] += bool(changed)
    session.flush()
    refresh(session)
    session.commit()
    return counts


# ----------------------------------------------------------------------- mail

def _pile(text: str) -> tuple[str, str]:
    """Which pile a mail goes on, and the sentences that put it there."""
    sentences = [s.strip() for s in re.split(r"[。！？!?\n]+", text) if s.strip()]
    for kind, pattern in PILES.items():
        hits = [s for s in sentences if pattern.search(s)]
        if hits:
            return kind, " ／ ".join(hits)
    return "other", ""


def import_mail(session: Session, user: User, filename: str, content: bytes,
                data_class: str = "synthetic", consent_id: int | None = None) -> Mail | None:
    """Load one .eml file. Returns None when this message was loaded before."""
    source, is_new = base.keep_source(session, user, FLOW, "mail", filename, content, data_class, consent_id)
    if not is_new:
        return None
    msg = email.message_from_bytes(content, policy=email.policy.default)
    sender = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
    part = msg.get_body(preferencelist=("plain",)) if sender else None
    if part is None:
        base.hand_off(session, FLOW, "source", source.id, "unreadable", "不是可讀的郵件檔（需要 .eml，且有寄件人與純文字內容）")
        session.commit()
        return None
    message_id = str(msg.get("Message-ID") or "").strip() or f"<{source.sha256[:32]}@no-message-id>"
    if session.scalar(select(Mail).where(Mail.message_id == message_id)):
        session.commit()
        return None
    sent_at = None
    try:
        sent_at = email.utils.parsedate_to_datetime(str(msg["Date"]))
        sent_at = sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    body = part.get_content().strip()
    mail = Mail(source_id=source.id, message_id=message_id, sender=sender, subject=str(msg.get("Subject", "")),
                sent_at=sent_at, body=body)
    session.add(mail)
    session.flush()
    source.text = f"寄件人：{sender}\n主旨：{mail.subject}\n\n{body}"
    kind, evidence = _pile(f"{mail.subject}\n{body}")
    evidence = evidence or mail.subject
    numbers = {n.replace("-", "") for n in INVOICE_NO.findall(f"{mail.subject}\n{body}")}
    items = list(session.scalars(select(ArItem).where(ArItem.invoice_no.in_(numbers)))) if numbers else []
    if items:
        for item in items:
            session.add(MailSignal(mail_id=mail.id, ar_item_id=item.id, customer=item.customer, kind=kind,
                                   linked_by="invoice_no", evidence=evidence))
    else:
        customers = set(session.scalars(select(ArItem.customer).where(ArItem.contact_email == sender)))
        if len(customers) != 1:          # same company domain, when exactly one customer uses it
            domain = sender.rsplit("@", 1)[-1]
            customers = set(session.scalars(select(ArItem.customer).where(ArItem.contact_email.like(f"%@{domain}"))))
        customer = customers.pop() if len(customers) == 1 else None
        signal = MailSignal(mail_id=mail.id, customer=customer, kind=kind, linked_by="sender" if customer else "nobody",
                            evidence=evidence)
        session.add(signal)
        session.flush()
        if customer is None:
            base.hand_off(session, FLOW, "mail_signal", signal.id, "unlinked_mail",
                          f"來信無法對應到客戶或發票：{sender}《{mail.subject}》")
    session.flush()
    refresh(session)
    session.commit()
    return mail


def assign_mail(session: Session, user: User, signal_id: int, customer: str) -> None:
    """A person says which customer an unlinked mail belongs to. It then holds that customer's items."""
    signal = session.get(MailSignal, signal_id)
    if not session.scalar(select(ArItem.id).where(ArItem.customer == customer).limit(1)):
        raise Refused(f"帳款中沒有客戶「{customer}」")
    base.log_change(session, FLOW, "mail_signal", signal.id, "customer", signal.customer, customer, user)
    signal.customer, signal.linked_by = customer, "manual"
    for h in session.scalars(select(Handoff).where(Handoff.flow == FLOW, Handoff.entity == "mail_signal",
                                                   Handoff.entity_id == signal.id, Handoff.status != "closed")):
        h.status, h.taken_by, h.closed_at, h.resolution = "closed", user.id, now(), f"歸戶：{customer}"
    session.flush()
    refresh(session)
    session.commit()


def signals_for(session: Session, item: ArItem, uncleared_only: bool = False) -> list[MailSignal]:
    q = select(MailSignal).where((MailSignal.ar_item_id == item.id) |
                                 (MailSignal.ar_item_id.is_(None) & (MailSignal.customer == item.customer)))
    if uncleared_only:
        q = q.where(MailSignal.id > item.hold_cleared_upto)
    return list(session.scalars(q.order_by(MailSignal.id)))


def refresh(session: Session) -> None:
    """Recompute every hold, and cancel reminders that are no longer allowed to go out."""
    for item in session.scalars(select(ArItem)):
        kinds = {s.kind for s in signals_for(session, item, uncleared_only=True)}
        item.hold = next((k for k in RANK if k in kinds), None)
        open_amount = balance(item)
        reason = ("帳款已收" if open_amount is not None and open_amount <= 0
                  else HOLD_LABEL[item.hold] if item.hold else "")
        if reason:
            for r in item.reminders:
                if r.status in ("draft", "approved"):
                    base.log_change(session, FLOW, "reminder", r.id, "status", r.status, "cancelled", None, note=reason)
                    r.status, r.cancel_reason = "cancelled", reason


def clear_hold(session: Session, user: User, item_id: int, reason: str) -> None:
    """A person has read the mail and decided reminders may resume. A later mail holds the item again."""
    item = session.get(ArItem, item_id)
    if not item.hold:
        return
    if not reason.strip():
        raise Refused("解除暫停必須寫明原因")
    base.log_change(session, FLOW, "ar_item", item.id, "hold", item.hold, None, user, note=reason.strip())
    item.hold, item.hold_cleared_upto = None, session.scalar(select(func.max(MailSignal.id))) or 0
    session.commit()


# ------------------------------------------------------------------ reminders

def blockers(item: ArItem, as_of: date) -> list[str]:
    """Why no reminder may be drafted or sent for this item right now. Empty means it may."""
    out = []
    if days_overdue(item, as_of) <= 0:
        out.append("未逾期或已收清")
    if item.hold:
        out.append(HOLD_LABEL[item.hold])
    out += [i["msg"] for i in item.issues if i["kind"] == "rule" or i["field"] in ("invoice_no", "contact_email")]
    return out


def stage_for(days: int) -> int:
    return 1 if days <= 30 else 2 if days <= 60 else 3


def money(value: Decimal) -> str:
    return f"NT${value:,.0f}" if value == value.to_integral_value() else f"NT${value:,.2f}"


OPENING = {1: "提醒您，以下款項已超過付款期限 {days} 天：",
           2: "先前曾提醒過，以下款項已超過付款期限 {days} 天，仍未見入帳：",
           3: "以下款項已超過付款期限 {days} 天，請協助確認付款安排："}


def compose(item: ArItem, as_of: date) -> tuple[int, str, str]:
    """Every figure in the text comes from the item's fields; none is typed by hand or by a model."""
    days = days_overdue(item, as_of)
    stage = stage_for(days)
    subject = f"【付款提醒】發票 {item.invoice_no}（到期日 {item.due_date}）"
    body = "\n".join([
        f"{item.customer} 您好：", "", OPENING[stage].format(days=days), "",
        f"　發票號碼：{item.invoice_no}", f"　發票日期：{item.invoice_date}", f"　到期日：{item.due_date}",
        f"　發票金額：{money(item.amount)}", f"　已收金額：{money(item.received)}", f"　未收餘額：{money(balance(item))}", "",
        "若您已付款，請回信告知付款日期與金額，我們核對後會更新紀錄，並請忽略此信。",
        "若對金額或貨品有任何疑問，也請直接回覆此信。", "", "謝謝。", os.environ.get("ONEFLOW_COMPANY", "（本公司名稱）")])
    return stage, subject, body


def draft_reminders(session: Session, as_of: date) -> list[Reminder]:
    """One draft per overdue item per stage. Running it again creates nothing new."""
    made = []
    for item in session.scalars(select(ArItem).order_by(ArItem.id)):
        if blockers(item, as_of):
            continue
        stage, subject, body = compose(item, as_of)
        if any(r.stage == stage for r in item.reminders):
            continue
        r = Reminder(ar_item_id=item.id, stage=stage, to=item.contact_email, subject=subject, body=body)
        item.reminders.append(r)
        made.append(r)
    session.commit()
    return made


def _still_allowed(session: Session, r: Reminder, as_of: date) -> None:
    if why := blockers(r.item, as_of):
        r.status, r.cancel_reason = "cancelled", why[0]
        session.commit()
        raise Refused(f"不能寄送，草稿已取消：{why[0]}")
    waiting = session.scalar(select(func.count()).select_from(Handoff).where(
        Handoff.flow == FLOW, Handoff.reason == "unlinked_mail", Handoff.status != "closed"))
    if waiting:
        raise Refused(f"有 {waiting} 封來信尚未歸戶，可能與這筆帳款有關。請先處理再核准或寄送")


def approve(session: Session, user: User, reminder_id: int, as_of: date) -> None:
    r = session.get(Reminder, reminder_id)
    if r.status != "draft":
        raise Refused("只有草稿可以核准")
    _still_allowed(session, r, as_of)
    r.status, r.approved_by = "approved", user.id
    base.log_change(session, FLOW, "reminder", r.id, "status", "draft", "approved", user)
    session.commit()


def edit_draft(session: Session, user: User, reminder_id: int, body: str) -> None:
    r = session.get(Reminder, reminder_id)
    if r.status != "draft":
        raise Refused("只有草稿可以修改")
    if not body.strip():
        raise Refused("內容不能留白")
    base.log_change(session, FLOW, "reminder", r.id, "body", r.body, body.strip(), user)
    r.body = body.strip()
    session.commit()


def outbox() -> Path:
    return Path(os.environ.get("ONEFLOW_OUTBOX", "outbox"))


def send(session: Session, user: User, reminder_id: int, as_of: date) -> Path:
    """"Sending" in this version writes an .eml file to the outbox folder for the user's own
    mail program. There is no SMTP here. One file per item and stage, ever."""
    r = session.get(Reminder, reminder_id)
    if r.status != "approved":
        raise Refused("必須先由人核准才能寄送")
    _still_allowed(session, r, as_of)
    if not base.claim(session, f"reminder:{r.ar_item_id}:{r.stage}"):
        session.rollback()
        raise Refused("這筆帳款在這個階段已寄過提醒，不再重複寄送")
    msg = EmailMessage()
    msg["To"], msg["Subject"] = r.to, r.subject
    msg["Message-ID"] = f"<reminder-{r.id}@oneflow.local>"
    msg.set_content(r.body)
    path = outbox() / f"reminder-{r.id:05d}-{r.item.invoice_no}-stage{r.stage}.eml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(msg))
    except OSError as exc:
        session.rollback()
        base.hand_off(session, FLOW, "reminder", reminder_id, "send_failed", str(exc))
        session.commit()
        raise Refused(f"寫入寄件匣失敗，已轉人工：{exc}") from exc
    r.status, r.sent_at = "sent", now()
    base.log_change(session, FLOW, "reminder", r.id, "status", "approved", "sent", user, note=str(path))
    session.commit()
    return path


def todo(session: Session, as_of: date) -> dict[str, list]:
    """The accountant's list, most constrained first."""
    out: dict[str, list] = {k: [] for k in ("unlinked", "dispute", "claims_paid", "promise", "other", "wrong", "missing",
                                             "to_approve", "to_send", "to_draft")}
    out["unlinked"] = list(session.scalars(select(Handoff).where(Handoff.flow == FLOW, Handoff.status != "closed")))
    for item in session.scalars(select(ArItem).order_by(ArItem.due_date, ArItem.id)):
        open_amount = balance(item)
        if open_amount is not None and open_amount <= 0:
            continue
        if item.hold:
            out[item.hold].append(item)
        if any(i["kind"] == "rule" for i in item.issues):
            out["wrong"].append(item)
        if any(i["kind"] == "missing" for i in item.issues):
            out["missing"].append(item)
        for r in item.reminders:
            if r.status in ("draft", "approved"):
                out["to_approve" if r.status == "draft" else "to_send"].append(r)
        if not blockers(item, as_of) and not any(r.stage == stage_for(days_overdue(item, as_of)) for r in item.reminders):
            out["to_draft"].append(item)
    return out
