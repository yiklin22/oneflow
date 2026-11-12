"""Repair intake: collect area, equipment, problem and a convenient time from a web
form or LINE messages, then hand a complete summary to a person who books the visit.

The program asks fixed questions and records the customer's own words. It does not
diagnose, quote a price, book a visit or deal with an emergency: anything that
looks dangerous goes straight to a person.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import base, rules
from .base import Refused
from .models import Enquiry, Handoff, Message, User, now

FLOW = "repairs"
TAIPEI = ZoneInfo("Asia/Taipei")
OPEN_DAYS, OPENS, CLOSES = range(0, 6), time(8, 30), time(18, 0)      # Monday to Saturday
FIELDS = ("area", "equipment", "problem", "slot_text")
LABELS = {"area": "地區", "equipment": "設備", "problem": "狀況", "slot_text": "方便時間", "contact": "聯絡方式"}
QUESTIONS = {
    "area": "請問設備所在的縣市與區域是哪裡？（例如：台中市西屯區）",
    "equipment": "請問需要維修的設備是什麼？（例如：冷氣、冰箱、洗衣機）",
    "problem": "請簡單描述一下設備目前的狀況。",
    "slot_text": "請問您方便的到府時間是什麼時候？（例如：週三下午、11/20 上午）",
}
DONE = ("已收到您的報修資料：\n地區：{area}\n設備：{equipment}\n狀況：{problem}\n方便時間：{slot_text}\n"
        "我們會由專人與您聯絡並確認到府時間。這則訊息不代表預約已成立。")
EMERGENCY = "您描述的情況可能有立即危險。本系統無法處理緊急事故，已通知專人儘快與您聯絡。若有立即危險，請先離開現場並撥打 119。"
NO_QUOTE = "費用與故障原因需要師傅現場檢查後才能說明，這裡無法報價或判斷原因。"
GIVE_UP = "謝謝您。目前資料還不完整，我們會由專人與您聯絡。"
NOTED = "已收到您的補充，專人會一併處理。"
TEXT_ONLY = "目前只能處理文字訊息，請用文字描述設備狀況，謝謝。"

DANGER = re.compile(r"瓦斯味|瓦斯外洩|漏瓦斯|瓦斯漏|漏氣|冒煙|(?<!不)著火|起火|失火|火花|漏電|觸電|電到|燒焦|焦味|爆炸|一氧化碳|淹水")
ASKS_PRICE = re.compile(r"多少錢|費用|價格|價錢|報價|收費|怎麼算|什麼原因|為什麼會|是不是.{0,8}壞")
CITY = "台北|新北|桃園|台中|台南|高雄|基隆|新竹|嘉義|苗栗|彰化|南投|雲林|屏東|宜蘭|花蓮|台東|澎湖|金門|連江"
AREA = re.compile(rf"(?:{CITY})(?:市|縣)?(?:[一-鿿]{{1,3}}?(?:區|鄉|鎮|市))?")
DISTRICT = re.compile(r"[一-鿿]{2}(?:區|鄉|鎮)")
NOT_DISTRICT = ("地區", "社區", "市區", "郊區", "園區", "這區", "那區", "山區")
EQUIPMENT = {"抽油煙機": "抽油煙機", "排油煙機": "抽油煙機", "洗衣機": "洗衣機", "烘衣機": "烘衣機", "乾衣機": "烘衣機",
             "洗碗機": "洗碗機", "熱水器": "熱水器", "飲水機": "飲水機", "開飲機": "飲水機", "除濕機": "除濕機",
             "暖氣機": "暖氣機", "電暖器": "暖氣機", "微波爐": "微波爐", "瓦斯爐": "瓦斯爐", "電風扇": "電風扇",
             "冷氣": "冷氣", "空調": "冷氣", "冰箱": "冰箱", "冷凍櫃": "冷凍櫃", "電視": "電視", "烤箱": "烤箱"}
EQUIPMENT_RE = re.compile("|".join(EQUIPMENT))
SYMPTOM = re.compile(r"不冷|不涼|不熱|不轉|不動|不運轉|不通電|不能(開|用|啟動)|無法(開機|啟動|使用|運轉|排水|脫水|加熱)|沒(反應|電|熱水)"
                     r"|漏水|滴水|異音|怪聲|很吵|噪音|異味|臭味|跳電|故障|壞了|壞掉|顯示.{0,6}(錯誤|E\d+)|結冰|結霜|點不著|打不開|關不掉"
                     r"|不出(水|熱水)|不排水|忽冷忽熱")
WEEKDAY = "一二三四五六日天"
DAY = re.compile(rf"今天|明天|後天|(這|下)?(?:週|周|星期|禮拜)([{WEEKDAY}])|(\d{{1,2}})\s*/\s*(\d{{1,2}})|(\d{{1,2}})月(\d{{1,2}})[日號]?"
                 r"|平日|假日|週末|周末|隨時")
PART = re.compile(r"早上|上午|中午|下午|傍晚|晚上|白天|\d{1,2}\s*[點:：]")
PART_OF = {"早上": "上午", "上午": "上午", "中午": "中午", "下午": "下午", "傍晚": "晚上", "晚上": "晚上", "白天": "白天"}
AVAILABLE = re.compile(r"可以|方便|都在|有空|在家|過來|以後|之後")


def clean(text: str) -> str:
    return rules.norm(text).replace("臺", "台")


def find_area(text: str) -> str | None:
    if m := AREA.search(text):
        return m[0]
    for m in DISTRICT.finditer(text):
        if not m[0].endswith(NOT_DISTRICT):
            return m[0]
    return None


def find_equipment(text: str) -> str | None:
    found = list(dict.fromkeys(EQUIPMENT[m[0]] for m in EQUIPMENT_RE.finditer(text)))
    return "、".join(found) or None


def find_slot(text: str, today: date, asked: bool = False) -> tuple[str | None, date | None, str | None]:
    """(the customer's words, a date, a part of day). The date and part are filled only when the
    words name exactly one; "this afternoon or tomorrow morning" stays as words for a person."""
    pieces = re.split(r"[，,。；;！!？?\s]+", text)
    names_a_day = any(DAY.search(c) for c in pieces)
    clauses = [c for c in pieces if DAY.search(c) or (PART.search(c) and (asked or names_a_day or AVAILABLE.search(c)))]
    if not clauses:
        return None, None, None
    words = " ".join(clauses)
    days: set[date | None] = set()
    for m in DAY.finditer(words):
        if m[0] in ("今天", "明天", "後天"):
            days.add(today + timedelta(days=("今天", "明天", "後天").index(m[0])))
        elif m[2]:
            weekday = min(WEEKDAY.index(m[2]), 6)
            ahead = (7 - today.weekday()) + weekday if m[1] == "下" else (weekday - today.weekday()) % 7 or (7 if not m[1] else 0)
            days.add(today + timedelta(days=ahead))
        elif m[3] or m[5]:
            days.add(rules.parse_date(f"{m[3] or m[5]}/{m[4] or m[6]}", ref=today)[0])
        else:
            days.add(None)
    parts = {PART_OF[m[0]] for m in PART.finditer(words) if m[0] in PART_OF}
    return words, (days.pop() if len(days) == 1 else None), (parts.pop() if len(parts) == 1 else None)


def after_hours(at: datetime) -> bool:
    local = at.astimezone(TAIPEI)
    return local.weekday() not in OPEN_DAYS or not (OPENS <= local.time() < CLOSES)


def missing(e: Enquiry) -> list[str]:
    return [f for f in FIELDS if not getattr(e, f)]


def _say(session: Session, e: Enquiry, text: str, at: datetime) -> str:
    session.add(Message(enquiry_id=e.id, direction="out", text=text, at=at, delivered=False))
    return text


def _emergency(session: Session, e: Enquiry, text: str, at: datetime) -> list[str]:
    first = not session.scalar(select(Handoff.id).where(
        Handoff.flow == FLOW, Handoff.entity == "enquiry", Handoff.entity_id == e.id, Handoff.reason == "emergency"))
    e.status = "handoff"
    base.hand_off(session, FLOW, "enquiry", e.id, "emergency", f"訊息中有危險字眼：{text[:200]}")
    return [_say(session, e, EMERGENCY, at)] if first else []


def receive(session: Session, channel: str, external_user: str | None, text: str,
            at: datetime | None = None, event_id: str | None = None, is_text: bool = True) -> tuple[Enquiry, list[str]]:
    """Take one customer message and return (enquiry, replies to send).

    A LINE event delivered twice (same event id) is stored once and answered once.
    The message is stored as the customer wrote it; matching runs on a normalised copy.
    """
    at = at or now()
    if event_id and (seen := session.scalar(select(Message).where(Message.event_id == event_id))):
        return session.get(Enquiry, seen.enquiry_id), []
    written = (text or "")[:2000]
    text = clean(written)
    e = None
    if external_user:
        e = session.scalar(select(Enquiry).where(
            Enquiry.channel == channel, Enquiry.external_user == external_user,
            Enquiry.status.in_(("collecting", "ready", "handoff")), Enquiry.updated_at > at - timedelta(days=7))
            .order_by(Enquiry.id.desc()))
    if e is None:
        e = Enquiry(channel=channel, external_user=external_user, after_hours=after_hours(at), created_at=at, asked={})
        session.add(e)
        session.flush()
    session.add(Message(enquiry_id=e.id, direction="in", text=written, event_id=event_id, at=at))
    e.updated_at = at
    replies: list[str] = []
    if not is_text:
        replies = [_say(session, e, TEXT_ONLY, at)]
    elif DANGER.search(text):
        replies = _emergency(session, e, text, at)
    elif e.status != "collecting":
        replies = [_say(session, e, NOTED, at)]
    else:
        last_asked = e.asked.get("_last")
        today = at.astimezone(TAIPEI).date()
        e.area = e.area or find_area(text)
        e.equipment = e.equipment or find_equipment(text) or (text if last_asked == "equipment" and 0 < len(text) <= 15 else None)
        if not e.problem and (SYMPTOM.search(text) or (last_asked == "problem" and len(text) >= 2)):
            e.problem = written.strip()[:300]
        words, day, part = find_slot(text, today, asked=last_asked == "slot_text")
        if words or (last_asked == "slot_text" and text):
            e.slot_text, e.slot_date, e.slot_part = words or text[:200], day, part
        price = ASKS_PRICE.search(text) and not e.asked.get("_price")
        asked = dict(e.asked, **({"_price": 1} if ASKS_PRICE.search(text) else {}))
        lack = missing(e)
        if not lack:
            e.status = "ready"
            reply = DONE.format(area=e.area, equipment=e.equipment, problem=e.problem, slot_text=e.slot_text)
        elif asked.get(lack[0], 0) >= 2:
            e.status = "handoff"
            base.hand_off(session, FLOW, "enquiry", e.id, "incomplete", f"問了兩次仍缺：{LABELS[lack[0]]}")
            reply = GIVE_UP
        else:
            asked[lack[0]] = asked.get(lack[0], 0) + 1
            asked["_last"] = lack[0]
            reply = QUESTIONS[lack[0]]
        e.asked = asked
        replies = [_say(session, e, f"{NO_QUOTE}\n{reply}" if price else reply, at)]
    session.commit()
    return e, replies


def submit_form(session: Session, area: str, equipment: str, problem: str, slot: str, contact: str,
                at: datetime | None = None) -> Enquiry:
    """The web form: the customer fills the four fields and a way to reach them."""
    at = at or now()
    values = {k: clean(v)[:300] for k, v in dict(area=area, equipment=equipment, problem=problem, slot_text=slot, contact=contact).items()}
    lacking = [LABELS[k] for k, v in values.items() if not v]
    if lacking:
        raise Refused("請填寫：" + "、".join(lacking))
    _, day, part = find_slot(values["slot_text"], at.astimezone(TAIPEI).date(), asked=True)
    e = Enquiry(channel="form", status="ready", after_hours=after_hours(at), created_at=at, updated_at=at,
                slot_date=day, slot_part=part, asked={}, **values)
    session.add(e)
    session.flush()
    session.add(Message(enquiry_id=e.id, direction="in", at=at,
                        text="\n".join(f"{LABELS[k]}：{v}" for k, v in values.items())))
    if DANGER.search(values["problem"] + values["equipment"]):
        _emergency(session, e, values["problem"], at)
    session.commit()
    return e


# ------------------------------------------------------- the dispatcher's side

def edit(session: Session, user: User, enquiry_id: int, fld: str, value: str) -> None:
    e = session.get(Enquiry, enquiry_id)
    if e.status in ("confirmed", "closed"):
        raise Refused("已確認或已結案，不能再修改")
    if fld not in LABELS:
        raise Refused(f"不能修改欄位 {fld}")
    value = clean(value)
    if not value:
        raise Refused("不能留白")
    base.log_change(session, FLOW, "enquiry", e.id, fld, getattr(e, fld), value, user)
    setattr(e, fld, value)
    if fld == "slot_text":
        _, e.slot_date, e.slot_part = find_slot(value, now().astimezone(TAIPEI).date(), asked=True)
    if e.status == "collecting" and not missing(e):
        e.status = "ready"
    session.commit()


def confirm(session: Session, user: User, enquiry_id: int, when: str) -> Enquiry:
    """A person books the visit. Only then does the enquiry count as a work order."""
    e = session.get(Enquiry, enquiry_id)
    if e.status not in ("ready", "handoff"):
        raise Refused("只有資料齊全或已由人工接手的報修可以確認預約")
    if lack := missing(e):
        raise Refused("資料不完整，還缺：" + "、".join(LABELS[f] for f in lack))
    try:
        appointment = datetime.fromisoformat(rules.norm(when).replace(" ", "T")).replace(tzinfo=TAIPEI)
    except ValueError as exc:
        raise Refused("預約時間格式應為 2026-11-20 14:00") from exc
    if appointment <= e.created_at:
        raise Refused("預約時間不能早於報修時間")
    if not base.claim(session, f"appointment:{e.id}"):
        session.rollback()
        raise Refused("這筆報修已經確認過預約")
    base.log_change(session, FLOW, "enquiry", e.id, "appointment", None, appointment.isoformat(), user)
    e.status, e.appointment, e.confirmed_by = "confirmed", appointment, user.id
    session.commit()
    return e


def close(session: Session, user: User, enquiry_id: int, reason: str) -> None:
    e = session.get(Enquiry, enquiry_id)
    if e.status in ("confirmed", "closed"):
        return
    if not reason.strip():
        raise Refused("結案必須寫明原因")
    base.log_change(session, FLOW, "enquiry", e.id, "status", e.status, "closed", user, note=reason.strip())
    e.status = "closed"
    session.commit()


# ----------------------------------------------------------------------- LINE

def line_signature_ok(body: bytes, signature: str | None, secret: str) -> bool:
    """LINE signs the raw request body with HMAC-SHA256 under the channel secret, base64 encoded."""
    want = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return bool(signature) and hmac.compare_digest(want, signature)


def line_reply(reply_token: str, texts: list[str]) -> bool:
    """Send replies through LINE's reply endpoint. Returns False when no channel token is set.
    This call has only ever run against a stand-in, never against LINE itself."""
    token = os.environ.get("ONEFLOW_LINE_TOKEN")
    if not token or not texts:
        return False
    request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply", method="POST",
        data=json.dumps({"replyToken": reply_token, "messages": [{"type": "text", "text": t} for t in texts[:5]]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status == 200


def line_events(session: Session, payload: dict) -> int:
    """Handle a verified webhook body. Returns how many events were new."""
    handled = 0
    for event in payload.get("events", []):
        user = (event.get("source") or {}).get("userId")
        if event.get("type") != "message" or not user:
            continue
        message = event.get("message") or {}
        at = datetime.fromtimestamp(event.get("timestamp", 0) / 1000, tz=TAIPEI) if event.get("timestamp") else now()
        text = message.get("text") if message.get("type") == "text" else f"［{message.get('type', '非文字')}訊息］"
        enquiry, replies = receive(session, "line", user, text, at=at, event_id=event.get("webhookEventId"),
                                   is_text=message.get("type") == "text")
        if not replies:
            continue
        handled += 1
        try:
            delivered = line_reply(event.get("replyToken", ""), replies)
        except OSError as exc:
            delivered = False
            base.hand_off(session, FLOW, "enquiry", enquiry.id, "reply_failed", str(exc))
        if delivered:
            for m in enquiry.messages[-len(replies):]:
                m.delivered = True
        session.commit()
    return handled
