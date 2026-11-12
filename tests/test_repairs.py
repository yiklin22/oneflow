import base64
import hashlib
import hmac
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from oneflow import acceptance, repairs
from oneflow.base import Refused
from oneflow.models import Change, Enquiry, Handoff, Message

MON = datetime(2026, 11, 16, 10, 0, tzinfo=repairs.TAIPEI)


def test_scripted_enquiries(session, admin):
    r = acceptance.run_repairs(session, admin)
    assert r["emergencies_handed_off"] == r["emergencies_expected"] == 3 and r["false_emergencies"] == 0
    assert r["complete"] == r["expected_complete"] == 24 and r["repair_requests"] == 27
    assert r["complete_but_field_wrong"] == [] and r["slot_date_wrong"] == []
    assert r["work_orders_confirmed"] == 24 and r["after_hours_work_orders"] == 10
    assert r["price_questions_answered_with_refusal"] == r["price_questions"] == 3


@pytest.mark.parametrize("text, asked, day, part", [
    ("週三下午可以來嗎", False, date(2026, 11, 18), "下午"), ("下週一", False, date(2026, 11, 23), None),
    ("今天下午或明天上午都可以", False, None, None), ("12/3 上午", False, date(2026, 12, 3), "上午"),
    ("11月20日晚上7點以後", False, date(2026, 11, 20), "晚上"), ("平日晚上", False, None, "晚上"),
    ("早上開機就跳電", False, "none", None), ("下午", True, None, "下午"), ("2/30 上午", False, None, "上午"),
])
def test_slots(text, asked, day, part):
    words, got_day, got_part = repairs.find_slot(text, MON.date(), asked)
    if day == "none":
        assert words is None
    else:
        assert words and (got_day, got_part) == (day, part)


@pytest.mark.parametrize("text, danger", [
    ("家裡有瓦斯味", True), ("插座冒煙", True), ("洗衣機漏電，摸到會麻", True), ("瓦斯爐點不著火", False),
    ("熱水器點不著", False), ("冷氣漏水", False), ("微波爐會跳電", False),
])
def test_danger_words(session, text, danger):
    e, replies = repairs.receive(session, "line", "U1", text, at=MON)
    assert (e.status == "handoff") == danger and (repairs.EMERGENCY in replies[0]) == danger
    assert bool(session.scalar(select(Handoff).where(Handoff.reason == "emergency"))) == danger


def test_no_diagnosis_no_price_no_booking_in_any_reply(session):
    e, replies = repairs.receive(session, "line", "U1", "冷氣不冷是不是壓縮機壞了？修一次多少錢？台中市西屯區 明天下午", at=MON)
    assert e.status == "ready" and repairs.NO_QUOTE in replies[0] and "不代表預約已成立" in replies[0]
    assert e.appointment is None and e.problem.startswith("冷氣不冷是不是壓縮機壞了？")   # the customer's words, untouched
    _, more = repairs.receive(session, "line", "U1", "那改後天好了", at=MON + timedelta(minutes=5))
    assert more == [repairs.NOTED] and session.query(Enquiry).count() == 1


def test_line_redelivery_and_non_text(session):
    e, first = repairs.receive(session, "line", "U1", "冰箱不冷", at=MON, event_id="ev1")
    _, again = repairs.receive(session, "line", "U1", "冰箱不冷", at=MON, event_id="ev1")
    assert first and again == [] and session.query(Message).filter_by(direction="in").count() == 1
    _, sticker = repairs.receive(session, "line", "U1", "［sticker訊息］", at=MON, event_id="ev2", is_text=False)
    assert sticker == [repairs.TEXT_ONLY] and e.asked == {"area": 1, "_last": "area"}


def test_danger_after_the_summary_still_goes_to_a_person(session):
    e, _ = repairs.receive(session, "line", "U1", "熱水器不熱 台中市西屯區 明天下午", at=MON)
    assert e.status == "ready"
    e, replies = repairs.receive(session, "line", "U1", "剛剛聞到瓦斯味", at=MON + timedelta(minutes=1))
    assert e.status == "handoff" and replies == [repairs.EMERGENCY]


def test_only_a_person_books_and_only_once(session, admin):
    e, _ = repairs.receive(session, "line", "U1", "冰箱不冷", at=MON)
    with pytest.raises(Refused):
        repairs.confirm(session, admin, e.id, "2026-11-18 10:00")             # still collecting
    repairs.edit(session, admin, e.id, "area", "高雄市鳳山區")
    repairs.edit(session, admin, e.id, "slot_text", "電話確認：週三上午")
    assert e.status == "ready" and session.scalar(select(Change).where(Change.field == "area")).user_id == admin.id
    for bad in ("週三上午", "2026-11-15 10:00", "2026-02-30 10:00"):
        with pytest.raises(Refused):
            repairs.confirm(session, admin, e.id, bad)
    done = repairs.confirm(session, admin, e.id, "2026-11-18T10:00")
    assert done.status == "confirmed" and done.appointment.hour == 10 and done.confirmed_by == admin.id
    with pytest.raises(Refused):
        repairs.confirm(session, admin, e.id, "2026-11-19 10:00")
    with pytest.raises(Refused):
        repairs.edit(session, admin, e.id, "area", "台北")


def test_web_form(session):
    with pytest.raises(Refused):
        repairs.submit_form(session, "台中市西屯區", "冷氣", "", "週三下午", "0912-000-000", at=MON)
    e = repairs.submit_form(session, "臺中市西屯區", "冷氣", "不冷", "週三下午", "0912-000-000", at=MON)
    assert e.status == "ready" and e.area == "台中市西屯區" and e.slot_date == date(2026, 11, 18) and not e.after_hours
    hot = repairs.submit_form(session, "台中", "熱水器", "有瓦斯味", "隨時", "0912", at=MON.replace(hour=22))
    assert hot.status == "handoff" and hot.after_hours


def test_line_signature_and_reply_delivery(session, monkeypatch):
    body = b'{"events": []}'
    good = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    assert repairs.line_signature_ok(body, good, "secret")
    assert not repairs.line_signature_ok(body, good, "other") and not repairs.line_signature_ok(body, None, "secret")
    sent = []
    monkeypatch.setattr(repairs, "line_reply", lambda token, texts: sent.append((token, texts)) or True)
    event = {"type": "message", "webhookEventId": "E1", "replyToken": "T1", "timestamp": int(MON.timestamp() * 1000),
             "source": {"userId": "U9"}, "message": {"type": "text", "text": "洗衣機不排水"}}
    assert repairs.line_events(session, {"events": [event, event, {"type": "follow", "source": {"userId": "U9"}}]}) == 1
    assert sent == [("T1", [repairs.QUESTIONS["area"]])]
    assert session.scalar(select(Message).where(Message.direction == "out")).delivered is True
