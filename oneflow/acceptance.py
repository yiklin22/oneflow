"""Acceptance runs: feed a flow its test set and count what the brief asks to be counted.

Each function returns plain numbers. Time saved is not among them: that needs a
person doing the work both ways, and no person has used this yet.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import orders
from .models import Handoff, Order, User

FIXTURES = Path(__file__).parent.parent / "fixtures"


def run_orders(session: Session, user: User) -> dict:
    root = FIXTURES / "orders"
    orders.import_products(session, user, (root / "products.csv").read_bytes())
    orders.import_customer_parts(session, user, (root / "customer_parts.csv").read_bytes())
    manifest = json.loads((root / "manifest.json").read_text())["orders"]
    out = dict(files=len(manifest), lines=0, lines_missing=0, orders_clean=0, orders_with_flags=0,
               header_wrong=0, duplicates_expected=0, duplicates_caught=0, unreadable_expected=0, unreadable_handed_off=0)
    fields = {"part_no": "part", "qty": "qty", "due_date": "due"}
    for name in fields.values():
        out[name] = dict(right=0, right_flagged=0, left_to_person=0, wrong_flagged=0, wrong_silent=0, silent=[])
    for entry in manifest:
        order, _ = orders.ingest(session, user, entry["file"], (root / "files" / entry["file"]).read_bytes())
        if entry["expect"] == "handoff":
            out["unreadable_expected"] += 1
            out["unreadable_handed_off"] += order.status == "handoff" and bool(session.scalar(
                select(Handoff).where(Handoff.entity == "order", Handoff.entity_id == order.id)))
            continue
        if entry["expect"] == "duplicate":
            out["duplicates_expected"] += 1
            out["duplicates_caught"] += order.status == "duplicate"
        if (order.customer, order.po_no, order.order_date) != (entry["customer"], entry["po_no"], date.fromisoformat(entry["order_date"])):
            out["header_wrong"] += 1
        got = {l.line_no: l for l in order.lines}
        flagged = False
        for truth in entry["lines"]:
            out["lines"] += 1
            line = got.get(truth["line_no"])
            if line is None:
                out["lines_missing"] += 1
                continue
            flagged = flagged or bool(line.issues)
            for attr, name in fields.items():
                value = getattr(line, attr)
                value = int(value) if attr == "qty" and value is not None else value.isoformat() if isinstance(value, date) else value
                want = truth[{"due_date": "due"}.get(attr, attr)]
                issues = [i for i in line.issues if i["field"] == attr]
                stopped = any(i["blocking"] for i in issues)
                if want is None or value is None:
                    kind = "left_to_person" if stopped else "wrong_silent"
                elif value == want:
                    kind = "right_flagged" if issues else "right"
                else:
                    kind = "wrong_flagged" if issues else "wrong_silent"
                out[name][kind] += 1
                if kind == "wrong_silent":
                    out[name]["silent"].append(f"{entry['file']} line {truth['line_no']}: got {value}, want {want}")
        if entry["expect"] == "review":
            out["orders_with_flags" if flagged or order.issues else "orders_clean"] += 1
    return out


def run_receivables(session: Session, user: User) -> dict:
    import os
    import tempfile

    from . import receivables as ar
    from .base import Refused
    from .models import ArItem, Mail, MailSignal, Reminder

    root = FIXTURES / "receivables"
    manifest = json.loads((root / "manifest.json").read_text())
    as_of = date.fromisoformat(manifest["as_of"])
    truth = {t["invoice_no"]: t for t in manifest["items"] if t["invoice_no"]}
    first = (root / "ar-2026-11-30.csv").read_bytes()
    out = dict(items_in_export=len(manifest["items"]), items_loaded=ar.import_export(session, user, "ar-2026-11-30.csv", first)["new"])

    piles_right = 0
    for m in manifest["mails"]:
        mail = ar.import_mail(session, user, m["file"], (root / "mails" / m["file"]).read_bytes())
        if mail and not m["repeat_of_message"]:
            signal = session.scalar(select(MailSignal).where(MailSignal.mail_id == mail.id))
            piles_right += signal.kind == m["truth_pile"]
    out["mails"] = len(manifest["mails"])
    out["mails_loaded"] = session.query(Mail).count()
    out["mail_piles_right"] = piles_right
    out["mails_unlinked"] = len(ar.todo(session, as_of)["unlinked"])

    items = {i.invoice_no: i for i in session.scalars(select(ArItem)) if i.invoice_no}
    found_overdue = {n for n, i in items.items() if ar.days_overdue(i, as_of) > 0}
    want_overdue = {n for n, t in truth.items() if t["overdue"]}
    out["overdue_expected"], out["overdue_found"] = len(want_overdue), len(found_overdue & want_overdue)
    out["overdue_false"] = len(found_overdue - want_overdue)
    planted = [t for t in manifest["items"] if t["planted"]]
    by_key = {(i.customer, i.invoice_no): i for i in session.scalars(select(ArItem))}
    out["planted_problems"] = len(planted)
    out["planted_problems_flagged"] = sum(
        bool((by_key.get((t["customer"], t["invoice_no"])) or next(i for i in by_key.values() if i.customer == t["customer"] and not i.invoice_no)).issues)
        for t in planted)
    held_truth = {n for n, t in truth.items() if t["held"] and t["overdue"]}
    out["held_expected_among_overdue"] = len(held_truth)

    drafts = ar.draft_reminders(session, as_of)
    may = {n for n, t in truth.items() if t["may_remind"]}
    drafted = {r.item.invoice_no for r in drafts}
    out["may_be_reminded"], out["drafts"] = len(may), len(drafts)
    out["drafts_missed"] = len(may - drafted)
    out["drafts_for_held_or_paid_items"] = sorted(drafted - may)     # only the mail nobody could link may cause these
    out["drafts_second_run"] = len(ar.draft_reminders(session, as_of))

    refused = 0
    for r in drafts:
        try:
            ar.approve(session, user, r.id, as_of)
        except Refused:
            refused += 1
    out["approvals_refused_while_a_mail_is_unlinked"] = refused
    for h in ar.todo(session, as_of)["unlinked"]:                    # a person says whose mail it is
        m = next(m for m in manifest["mails"] if m["belongs_to"])
        ar.assign_mail(session, user, h.entity_id, m["belongs_to"])
    approved = 0
    for r in session.scalars(select(Reminder).where(Reminder.status == "draft")):
        ar.approve(session, user, r.id, as_of)
        approved += 1
    out["approved_after_assigning_it"] = approved
    out["cancelled_by_assigning_it"] = session.query(Reminder).filter_by(status="cancelled").count()

    out["same_export_again"] = ar.import_export(session, user, "again.csv", first)
    out["later_export"] = ar.import_export(session, user, "ar-2026-12-05.csv", (root / "ar-2026-12-05.csv").read_bytes())
    out["cancelled_because_paid"] = session.query(Reminder).filter_by(cancel_reason="帳款已收").count()

    with tempfile.TemporaryDirectory() as box:
        os.environ["ONEFLOW_OUTBOX"] = box
        sent, repeats_refused, unapproved_refused = [], 0, 0
        for r in session.scalars(select(Reminder).where(Reminder.status == "approved")).all():
            ar.send(session, user, r.id, as_of)
            sent.append(r.item.invoice_no)
            try:
                ar.send(session, user, r.id, as_of)
            except Refused:
                repeats_refused += 1
        out["sent"], out["files_in_outbox"] = len(sent), len(os.listdir(box))
        out["second_send_refused"] = repeats_refused
        del os.environ["ONEFLOW_OUTBOX"]
    forbidden = (set(truth) - may) | set(manifest["paid_in_later_export"])
    out["sent_to_paid_held_or_disputed"] = sorted(set(sent) & forbidden)
    out["sent_twice"] = len(sent) - len(set(sent))
    return out


def run_repairs(session: Session, user: User) -> dict:
    """Play the 30 scripted customers through the same function the LINE webhook calls."""
    from datetime import datetime, timedelta

    from . import repairs
    from .models import Enquiry

    entries = json.loads((FIXTURES / "repairs" / "enquiries.json").read_text())["enquiries"]
    out = dict(enquiries=len(entries), emergencies_expected=0, emergencies_handed_off=0, false_emergencies=0,
               repair_requests=0, complete=0, expected_complete=0, complete_but_field_wrong=[], slot_date_wrong=[],
               after_hours=0, after_hours_complete=0, work_orders_confirmed=0, after_hours_work_orders=0,
               price_questions=0, price_questions_answered_with_refusal=0, questions_asked=0)
    for entry in entries:
        at = datetime.fromisoformat(entry["at"])
        answers = {k: list(v) if isinstance(v, list) else [v] for k, v in entry["answers"].items()}
        enquiry, replies = repairs.receive(session, "line", entry["user"], entry["opening"], at=at, event_id=f"ev-{entry['id']}-0")
        said = list(replies)
        for turn in range(1, 12):
            asked = next((f for f, q in repairs.QUESTIONS.items() if replies and q in replies[-1]), None)
            if not asked or not answers.get(asked):
                break
            out["questions_asked"] += 1
            at += timedelta(minutes=2)
            enquiry, replies = repairs.receive(session, "line", entry["user"], answers[asked].pop(0), at=at, event_id=f"ev-{entry['id']}-{turn}")
            said += replies
        truth, emergency = entry["truth"], any(repairs.EMERGENCY in r for r in said)
        if truth.get("asks_price"):
            out["price_questions"] += 1
            out["price_questions_answered_with_refusal"] += any(repairs.NO_QUOTE in r for r in said)
        if entry["expect"] == "emergency":
            out["emergencies_expected"] += 1
            out["emergencies_handed_off"] += emergency and enquiry.status == "handoff"
            continue
        out["false_emergencies"] += emergency
        out["repair_requests"] += 1
        out["after_hours"] += enquiry.after_hours
        out["expected_complete"] += entry["expect"] == "ready"
        if enquiry.status != "ready":
            continue
        out["complete"] += 1
        out["after_hours_complete"] += enquiry.after_hours
        wrong = [f for f in ("area", "equipment") if truth.get(f) != getattr(enquiry, f)]
        if truth.get("problem_has") not in (enquiry.problem or ""):
            wrong.append("problem")
        if wrong:
            out["complete_but_field_wrong"].append(f"#{entry['id']}: {wrong}")
        got_date = enquiry.slot_date.isoformat() if enquiry.slot_date else None
        if (got_date, enquiry.slot_part) != (truth.get("slot_date"), truth.get("slot_part")):
            out["slot_date_wrong"].append(f"#{entry['id']}: got {got_date} {enquiry.slot_part}, want {truth.get('slot_date')} {truth.get('slot_part')}")
        # the dispatcher books a visit: here, 10:00 two days after the enquiry
        when = (datetime.fromisoformat(entry["at"]) + timedelta(days=2)).strftime("%Y-%m-%d 10:00")
        repairs.confirm(session, user, enquiry.id, when)
        out["work_orders_confirmed"] += 1
        out["after_hours_work_orders"] += enquiry.after_hours
    out["completeness_rate"] = round(out["complete"] / out["repair_requests"], 3)
    out["still_collecting"] = session.query(Enquiry).filter_by(status="collecting").count()
    out["handed_to_a_person"] = session.query(Enquiry).filter_by(status="handoff").count()
    return out
