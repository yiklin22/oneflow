import json
from datetime import date
from decimal import Decimal
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from oneflow import acceptance, receivables as ar
from oneflow.base import Refused
from oneflow.models import ArItem, Change, Handoff, Reminder

ROOT = acceptance.FIXTURES / "receivables"
AS_OF = date(2026, 11, 30)
HEAD = "客戶,發票號碼,發票日期,到期日,金額,已收金額,聯絡信箱,簽收單\n"


def export(*rows):
    return (HEAD + "\n".join(rows) + "\n").encode()


def eml(sender, subject, body, message_id=None):
    msg = EmailMessage()
    msg["From"], msg["Subject"] = sender, subject
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    return bytes(msg)


GOOD = "甲公司,AB12345678,2026/09/01,2026/10/01,10500,0,ap@jia.example,有"


def test_acceptance_set_sends_nothing_it_should_not(session, admin):
    r = acceptance.run_receivables(session, admin)
    assert r["items_loaded"] == 60 and r["overdue_found"] == r["overdue_expected"] and r["overdue_false"] == 0
    assert r["planted_problems_flagged"] == r["planted_problems"] == 12
    assert r["drafts_missed"] == 0 and r["drafts_second_run"] == 0
    assert r["approvals_refused_while_a_mail_is_unlinked"] == r["drafts"]      # nothing moves while a mail has no owner
    assert r["same_export_again"]["repeat"] and r["later_export"] == {"new": 0, "updated": 2, "repeat": False}
    assert r["cancelled_because_paid"] == 2
    assert r["sent"] == r["files_in_outbox"] == r["second_send_refused"] > 0
    assert r["sent_to_paid_held_or_disputed"] == [] and r["sent_twice"] == 0
    assert r["mails_loaded"] == r["mails"] - 1                                 # one message was exported twice


def test_rows_are_checked_by_rule(session, admin):
    ar.import_export(session, admin, "a.csv", export(
        GOOD, "乙公司,,2026/09/01,2026/10/01,5000,0,,無", "丙公司,AB-1234,2026/09/31,2026/08/01,abc,9,x@y.example,有",
        "丁公司,CD12345678,115/09/01,115/10/01,\"NT$1,000\",\"2,000\",d@ding.example,有"))
    by = {i.customer: i for i in session.scalars(select(ArItem))}
    assert by["甲公司"].issues == [] and ar.days_overdue(by["甲公司"], AS_OF) == 60 and ar.balance(by["甲公司"]) == Decimal("10500")
    assert {i["field"] for i in by["乙公司"].issues} == {"invoice_no", "contact_email", "has_receipt"}
    assert {i["field"] for i in by["丙公司"].issues} >= {"invoice_no", "invoice_date", "amount"}
    assert by["丁公司"].due_date == date(2026, 10, 1) and any(i["field"] == "received" for i in by["丁公司"].issues)
    assert ar.days_overdue(by["丁公司"], AS_OF) == 0                # received more than billed: not knowable
    assert [r.item.customer for r in ar.draft_reminders(session, AS_OF)] == ["甲公司"]


@pytest.mark.parametrize("text, pile", [
    ("發票 AB12345678 已於 11/18 匯款", "claims_paid"), ("這張尚未付款，預計下週五匯款", "promise"),
    ("數量不符，少了兩箱", "dispute"), ("我們已付款，但其中一箱有瑕疵", "dispute"), ("請寄型錄", "other"),
    ("是否收到我們的匯款", "other"),          # a claim of payment the keywords miss; it still holds the item
])
def test_mail_piles(text, pile):
    assert ar._pile(text)[0] == pile


def test_any_customer_mail_holds_the_item_until_a_person_clears_it(session, admin):
    ar.import_export(session, admin, "a.csv", export(GOOD, "甲公司,AB22222222,2026/09/05,2026/10/05,2000,0,ap@jia.example,有"))
    draft = ar.draft_reminders(session, AS_OF)[0]
    ar.import_mail(session, admin, "1.eml", eml("ap@jia.example", "問題", "是否收到我們針對 AB12345678 的匯款"))
    item = session.scalar(select(ArItem).where(ArItem.invoice_no == "AB12345678"))
    assert item.hold == "other" and session.get(Reminder, draft.id).status == "cancelled"
    assert session.scalar(select(ArItem).where(ArItem.invoice_no == "AB22222222")).hold is None
    with pytest.raises(Refused):
        ar.clear_hold(session, admin, item.id, "")
    ar.clear_hold(session, admin, item.id, "已核對，未入帳")
    assert item.hold is None and session.scalar(select(Change).where(Change.field == "hold")).user_id == admin.id
    ar.import_mail(session, admin, "2.eml", eml("someone@jia.example", "帳", "那幾張有爭議"))       # no invoice number: whole customer
    assert {i.hold for i in session.scalars(select(ArItem))} == {"dispute"}


def test_a_reminder_needs_approval_and_goes_out_once(session, admin, tmp_path, monkeypatch):
    monkeypatch.setenv("ONEFLOW_OUTBOX", str(tmp_path))
    ar.import_export(session, admin, "a.csv", export(GOOD))
    draft = ar.draft_reminders(session, AS_OF)[0]
    assert draft.stage == 2 and "NT$10,500" in draft.body and "60 天" in draft.body
    with pytest.raises(Refused):
        ar.send(session, admin, draft.id, AS_OF)                    # not approved
    ar.edit_draft(session, admin, draft.id, draft.body + "\n承辦：王小姐")
    ar.approve(session, admin, draft.id, AS_OF)
    with pytest.raises(Refused):
        ar.edit_draft(session, admin, draft.id, "改")
    path = ar.send(session, admin, draft.id, AS_OF)
    assert "AB12345678" in path.name and len(list(tmp_path.iterdir())) == 1
    with pytest.raises(Refused):
        ar.send(session, admin, draft.id, AS_OF)
    assert ar.draft_reminders(session, AS_OF) == []                 # same stage: nothing new
    assert len(ar.draft_reminders(session, date(2026, 12, 15))) == 1    # 75 days: the next stage


def test_payment_between_approval_and_sending_stops_the_send(session, admin, tmp_path, monkeypatch):
    monkeypatch.setenv("ONEFLOW_OUTBOX", str(tmp_path))
    ar.import_export(session, admin, "a.csv", export(GOOD))
    draft = ar.draft_reminders(session, AS_OF)[0]
    ar.approve(session, admin, draft.id, AS_OF)
    ar.import_export(session, admin, "b.csv", export(GOOD.replace(",0,", ",10500,")))
    assert session.get(Reminder, draft.id).status == "cancelled"
    with pytest.raises(Refused):
        ar.send(session, admin, draft.id, AS_OF)
    assert not list(tmp_path.iterdir())


def test_unlinked_and_unreadable_mail_go_to_a_person(session, admin):
    ar.import_export(session, admin, "a.csv", export(GOOD))
    ar.import_mail(session, admin, "x.eml", eml("who@elsewhere.example", "款", "已匯款"))
    ar.import_mail(session, admin, "y.eml", b"not a mail at all")
    assert {h.reason for h in session.scalars(select(Handoff))} == {"unlinked_mail", "unreadable"}
    draft = ar.draft_reminders(session, AS_OF)[0]
    with pytest.raises(Refused):
        ar.approve(session, admin, draft.id, AS_OF)
    signal_id = session.scalar(select(Handoff.entity_id).where(Handoff.reason == "unlinked_mail"))
    with pytest.raises(Refused):
        ar.assign_mail(session, admin, signal_id, "沒有這家")
    ar.assign_mail(session, admin, signal_id, "甲公司")
    assert session.get(Reminder, draft.id).status == "cancelled" and session.scalar(select(ArItem)).hold == "claims_paid"


def test_same_message_id_and_real_data_gate(session, admin):
    ar.import_export(session, admin, "a.csv", export(GOOD))
    assert ar.import_mail(session, admin, "1.eml", eml("ap@jia.example", "a", "AB12345678 已匯款", "<1@x>"))
    assert ar.import_mail(session, admin, "2.eml", eml("ap@jia.example", "a", "AB12345678 已匯款 ", "<1@x>")) is None
    with pytest.raises(Refused):
        ar.import_export(session, admin, "real.csv", export(GOOD + " "), data_class="real")
    with pytest.raises(Refused):
        ar.import_export(session, admin, "bad.csv", b"no,such,columns\n1,2,3\n")
