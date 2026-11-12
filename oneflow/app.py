"""The web interface. Every page is Traditional Chinese and server-rendered.

Only three paths work without a login: the login page, the public repair form
and the LINE webhook (which checks LINE's signature instead).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import base, orders, receivables as ar, repairs
from .base import Refused
from .db import get_session
from .models import ArItem, Change, Consent, Enquiry, ExportBatch, Handoff, MailSignal, Order, OrderLine, Reminder, User

COOKIE = "oneflow_session"
MAX_UPLOAD = 10 * 1024 * 1024
STATUS = {
    "needs_review": "待確認", "duplicate": "重複單號", "handoff": "人工接手", "confirmed": "已確認", "exported": "已匯出",
    "draft": "草稿", "approved": "已核准", "sent": "已寫入寄件匣", "cancelled": "已取消",
    "collecting": "蒐集中", "ready": "資料齊全，待確認預約", "closed": "已結案", "open": "待處理", "taken": "處理中",
}
REASONS = {"unreadable": "無法讀取", "unlinked_mail": "來信無法歸戶", "send_failed": "寄送失敗", "emergency": "可能的緊急狀況",
           "incomplete": "資料問不齊", "reply_failed": "LINE 回覆失敗"}

app = FastAPI(title="oneflow", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(FLOWS=base.FLOWS, ROLES=base.ROLES, STATUS=STATUS, REASONS=REASONS, HOLD=ar.HOLD_LABEL,
                             may=base.may, LABELS=repairs.LABELS)
templates.env.filters["taipei"] = lambda d: d.astimezone(repairs.TAIPEI).strftime("%Y-%m-%d %H:%M") if d else ""
templates.env.filters["money"] = lambda v: "" if v is None else ar.money(v)


class NeedLogin(Exception):
    pass


def back(request: Request, fallback: str | None = None, **message: str) -> RedirectResponse:
    """Go back to the page the form was on, carrying one message in the query string.
    Only the path of the referring page is used, so this cannot redirect to another site."""
    if fallback is None:
        fallback = next((p for p in ("/orders", "/receivables", "/repairs", "/repair", "/handoffs", "/consents")
                         if request.url.path.startswith(p)), "/")
    target = urlsplit(request.headers.get("referer") or fallback)
    path = "/" + target.path.lstrip("/") if target.path else fallback
    query = [(k, v) for k, v in parse_qsl(target.query) if k not in ("ok", "error")] + list(message.items())
    return RedirectResponse(f"{path}?{urlencode(query)}" if query else path, status_code=303)


@app.exception_handler(Refused)
async def refused(request: Request, exc: Refused):
    return back(request, error=str(exc))


@app.exception_handler(NeedLogin)
async def need_login(request: Request, exc: NeedLogin):
    return RedirectResponse("/login", status_code=303)


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    """A form post from another site is refused. The session cookie is SameSite=Lax as well."""
    origin = request.headers.get("origin")
    if request.method == "POST" and request.url.path != "/line/webhook" and origin and urlsplit(origin).netloc != request.headers.get("host"):
        return PlainTextResponse("來源不符", status_code=403)
    return await call_next(request)


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    user = base.user_for(session, request.cookies.get(COOKIE))
    if not user:
        raise NeedLogin()
    return user


def member(flow: str):
    def check(user: User = Depends(current_user)) -> User:
        if not base.may(user, flow):
            raise HTTPException(403, f"您的角色不能使用「{base.FLOWS[flow]}」")
        return user
    return check


def page(request: Request, name: str, user: User | None = None, **context):
    return templates.TemplateResponse(request, name, dict(context, user=user, ok=request.query_params.get("ok"),
                                                          error=request.query_params.get("error")))


async def read(upload: UploadFile) -> bytes:
    content = await upload.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise Refused(f"{upload.filename} 超過 10 MB")
    if not content:
        raise Refused("沒有選擇檔案，或檔案是空的")
    return content


def consent_of(data_class: str, consent_id: str) -> int | None:
    return int(consent_id) if data_class == "real" and consent_id.isdigit() else None


# ---------------------------------------------------------------- shared base

@app.get("/login")
def login_page(request: Request):
    return page(request, "login.html")


@app.post("/login")
def login(request: Request, email: str = Form(), password: str = Form(), session: Session = Depends(get_session)):
    token = base.log_in(session, email, password)
    if not token:
        return RedirectResponse("/login?" + urlencode({"error": "帳號或密碼不正確"}), status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=12 * 3600,
                        secure=request.url.scheme == "https")
    return response


@app.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    base.log_out(session, request.cookies.get(COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.get("/")
def home(request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)):
    def count(model, **where):
        return session.scalar(select(func.count()).select_from(model).filter_by(**where))
    cards = {
        "orders": [("待確認", count(Order, status="needs_review")), ("已確認待匯出", count(Order, status="confirmed")),
                   ("重複單號", count(Order, status="duplicate"))],
        "receivables": [("草稿待核准", count(Reminder, status="draft")), ("已核准待寄送", count(Reminder, status="approved")),
                        ("暫停催收中", session.scalar(select(func.count()).select_from(ArItem).where(ArItem.hold.is_not(None))))],
        "repairs": [("蒐集中", count(Enquiry, status="collecting")), ("待確認預約", count(Enquiry, status="ready")),
                    ("已確認預約", count(Enquiry, status="confirmed"))],
    }
    waiting = session.scalar(select(func.count()).select_from(Handoff).where(Handoff.status != "closed"))
    return page(request, "home.html", user, cards=cards, waiting=waiting)


@app.get("/handoffs")
def handoffs(request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)):
    rows = [h for h in session.scalars(select(Handoff).order_by(Handoff.status == "closed", Handoff.id.desc()).limit(200))
            if base.may(user, h.flow)]
    links = {"order": "/orders/{}", "enquiry": "/repairs/{}", "reminder": "/receivables", "mail_signal": "/receivables", "source": ""}
    return page(request, "handoffs.html", user, rows=rows, links=links)


@app.post("/handoffs/{handoff_id}/take")
def handoff_take(request: Request, handoff_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    base.take_handoff(session, user, handoff_id)
    return back(request, ok="已接手")


@app.post("/handoffs/{handoff_id}/close")
def handoff_close(request: Request, handoff_id: int, resolution: str = Form(""), user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    base.close_handoff(session, user, handoff_id, resolution)
    return back(request, ok="已結案")


@app.get("/consents")
def consents(request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return page(request, "consents.html", user, rows=session.scalars(select(Consent).order_by(Consent.id.desc())).all())


@app.post("/consents")
def consent_add(request: Request, party: str = Form(""), scope: str = Form(""), basis: str = Form(""),
                user: User = Depends(current_user), session: Session = Depends(get_session)):
    base.record_consent(session, user, party, scope, basis)
    return back(request, ok="已記錄同意")


@app.post("/consents/{consent_id}/revoke")
def consent_revoke(request: Request, consent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    base.revoke_consent(session, user, consent_id)
    return back(request, ok="已撤回；之後不能再以這筆同意匯入真實文件")


@app.get("/changes")
def changes(request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)):
    rows = [c for c in session.scalars(select(Change).order_by(Change.id.desc()).limit(300)) if c.flow == "base" or base.may(user, c.flow)]
    names = {u.id: u.name for u in session.scalars(select(User))}
    return page(request, "changes.html", user, rows=rows, names=names)


# --------------------------------------------------------------------- orders

@app.get("/orders")
def orders_list(request: Request, user: User = Depends(member("orders")), session: Session = Depends(get_session)):
    rows = session.scalars(select(Order).order_by(Order.id.desc()).limit(300)).all()
    return page(request, "orders.html", user, rows=rows, blocking=orders.blocking,
                consents=base.active_consents(session, "orders"),
                batches=session.scalars(select(ExportBatch).order_by(ExportBatch.id.desc()).limit(20)).all(),
                products=session.scalar(select(func.count()).select_from(orders.Product)))


@app.post("/orders/upload")
async def orders_upload(request: Request, files: list[UploadFile], data_class: str = Form("synthetic"), consent_id: str = Form(""),
                        user: User = Depends(member("orders")), session: Session = Depends(get_session)):
    new = repeats = 0
    for upload in files:
        _, is_new = orders.ingest(session, user, upload.filename or "order", await read(upload), data_class,
                                  consent_of(data_class, consent_id))
        new, repeats = new + is_new, repeats + (not is_new)
    return back(request, ok=f"讀入 {new} 張訂單" + (f"；{repeats} 個檔案先前已上傳，未重複處理" if repeats else ""))


@app.post("/orders/tables")
async def orders_tables(request: Request, file: UploadFile, kind: str = Form(), user: User = Depends(member("orders")),
                        session: Session = Depends(get_session)):
    load = orders.import_products if kind == "products" else orders.import_customer_parts
    try:
        count = load(session, user, await read(file))
    except orders.Unreadable as exc:
        raise Refused(str(exc)) from exc
    return back(request, ok=f"已匯入 {count} 列")


@app.get("/orders/{order_id}")
def order_page(request: Request, order_id: int, user: User = Depends(member("orders")), session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(404)
    orders.open_order(session, order)
    line_ids = [l.id for l in order.lines]
    history = base.history(session, "orders", "order", order.id) + list(session.scalars(
        select(Change).where(Change.entity == "order_line", Change.entity_id.in_(line_ids)).order_by(Change.id)))
    return page(request, "order.html", user, order=order, source_lines=order.source.text.split("\n"),
                marked={l.evidence["line"]: l.line_no for l in order.lines}, history=sorted(history, key=lambda c: c.id),
                names={u.id: u.name for u in session.scalars(select(User))}, blocking=orders.blocking(order),
                products=orders.Catalog.load(session).products)


@app.post("/orders/{order_id}/edit")
def order_edit(request: Request, order_id: int, field: str = Form(), value: str = Form(""),
               user: User = Depends(member("orders")), session: Session = Depends(get_session)):
    orders.edit_order(session, user, order_id, field, value)
    return back(request, ok="已修改")


@app.post("/orders/lines/{line_id}/edit")
def line_edit(request: Request, line_id: int, field: str = Form(), value: str = Form(""),
              user: User = Depends(member("orders")), session: Session = Depends(get_session)):
    if not session.get(OrderLine, line_id):
        raise HTTPException(404)
    orders.edit_line(session, user, line_id, field, value)
    return back(request, ok="已修改")


@app.post("/orders/{order_id}/confirm")
def order_confirm(request: Request, order_id: int, user: User = Depends(member("orders")), session: Session = Depends(get_session)):
    orders.confirm(session, user, order_id)
    return back(request, ok="已確認，可以匯出")


@app.post("/orders/{order_id}/release")
def order_release(request: Request, order_id: int, reason: str = Form(""), user: User = Depends(member("orders")),
                  session: Session = Depends(get_session)):
    orders.release_duplicate(session, user, order_id, reason)
    return back(request, ok="已解除重複標記")


@app.post("/orders-export")
def orders_export(request: Request, user: User = Depends(member("orders")), session: Session = Depends(get_session)):
    batch = orders.export_confirmed(session, user)
    return back(request, "/orders", ok=f"已產生匯出批次 {batch.id}，共 {batch.order_count} 張訂單")


@app.get("/orders-export/{batch_id}.csv")
def orders_export_file(batch_id: int, encoding: str = "utf-8-sig", user: User = Depends(member("orders")),
                       session: Session = Depends(get_session)):
    batch = session.get(ExportBatch, batch_id)
    if not batch or encoding not in ("utf-8-sig", "big5"):
        raise HTTPException(404)
    try:
        data = batch.csv.encode("cp950" if encoding == "big5" else "utf-8-sig")
    except UnicodeEncodeError as exc:
        raise HTTPException(409, "內容含 Big5 沒有的字，請改用 UTF-8 下載") from exc
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="orders-{batch_id}.csv"'})


# ---------------------------------------------------------------- receivables

def as_of_date(request: Request) -> date:
    try:
        return date.fromisoformat(request.query_params.get("as_of", ""))
    except ValueError:
        return datetime.now(repairs.TAIPEI).date()


@app.get("/receivables")
def receivables_page(request: Request, user: User = Depends(member("receivables")), session: Session = Depends(get_session)):
    as_of = as_of_date(request)
    signals = {s.id: s for s in session.scalars(select(MailSignal).where(MailSignal.linked_by == "nobody"))}
    return page(request, "receivables.html", user, todo=ar.todo(session, as_of), as_of=as_of, signals=signals,
                days=lambda item: ar.days_overdue(item, as_of), balance=ar.balance,
                customers=sorted(set(session.scalars(select(ArItem.customer)))), consents=base.active_consents(session, "receivables"),
                total=session.scalar(select(func.count()).select_from(ArItem)))


@app.post("/receivables/files")
async def receivables_files(request: Request, files: list[UploadFile], kind: str = Form(), data_class: str = Form("synthetic"),
                            consent_id: str = Form(""), user: User = Depends(member("receivables")),
                            session: Session = Depends(get_session)):
    consent = consent_of(data_class, consent_id)
    if kind == "export":
        counts = [ar.import_export(session, user, f.filename or "export", await read(f), data_class, consent) for f in files]
        return back(request, ok="；".join("先前已匯入，未重複處理" if c["repeat"] else f"新增 {c['new']} 筆、更新 {c['updated']} 筆" for c in counts))
    loaded = [ar.import_mail(session, user, f.filename or "mail", await read(f), data_class, consent) for f in files]
    new = sum(m is not None for m in loaded)
    return back(request, ok=f"讀入 {new} 封郵件" + (f"；{len(loaded) - new} 封先前已讀入或無法讀取" if new < len(loaded) else ""))


@app.post("/receivables/draft")
def receivables_draft(request: Request, as_of: str = Form(), user: User = Depends(member("receivables")),
                      session: Session = Depends(get_session)):
    made = ar.draft_reminders(session, date.fromisoformat(as_of))
    return back(request, ok=f"產生 {len(made)} 份草稿。每一份都要由人核准後才會寄送")


@app.get("/receivables/items/{item_id}")
def ar_item_page(request: Request, item_id: int, user: User = Depends(member("receivables")), session: Session = Depends(get_session)):
    item = session.get(ArItem, item_id)
    if not item:
        raise HTTPException(404)
    as_of = as_of_date(request)
    return page(request, "ar_item.html", user, item=item, as_of=as_of, days=ar.days_overdue(item, as_of), balance=ar.balance(item),
                signals=ar.signals_for(session, item), blockers=ar.blockers(item, as_of),
                history=base.history(session, "receivables", "ar_item", item.id),
                names={u.id: u.name for u in session.scalars(select(User))})


@app.post("/receivables/items/{item_id}/clear")
def ar_clear(request: Request, item_id: int, reason: str = Form(""), user: User = Depends(member("receivables")),
             session: Session = Depends(get_session)):
    ar.clear_hold(session, user, item_id, reason)
    return back(request, ok="已解除暫停")


@app.post("/receivables/signals/{signal_id}/assign")
def ar_assign(request: Request, signal_id: int, customer: str = Form(""), user: User = Depends(member("receivables")),
              session: Session = Depends(get_session)):
    ar.assign_mail(session, user, signal_id, customer)
    return back(request, ok=f"已歸戶到 {customer}，該客戶的帳款暫停催收")


@app.post("/receivables/reminders/{reminder_id}/{action}")
def reminder_action(request: Request, reminder_id: int, action: str, as_of: str = Form(""), body: str = Form(""),
                    user: User = Depends(member("receivables")), session: Session = Depends(get_session)):
    if not session.get(Reminder, reminder_id):
        raise HTTPException(404)
    when = date.fromisoformat(as_of) if as_of else datetime.now(repairs.TAIPEI).date()
    if action == "approve":
        ar.approve(session, user, reminder_id, when)
        return back(request, ok="已核准")
    if action == "send":
        path = ar.send(session, user, reminder_id, when)
        return back(request, ok=f"已寫入寄件匣：{path.name}")
    if action == "edit":
        ar.edit_draft(session, user, reminder_id, body)
        return back(request, ok="已修改草稿")
    raise HTTPException(404)


# -------------------------------------------------------------------- repairs

@app.get("/repair")
def repair_form(request: Request):
    return page(request, "repair_form.html")


@app.post("/repair")
def repair_submit(request: Request, area: str = Form(""), equipment: str = Form(""), problem: str = Form(""), slot: str = Form(""),
                  contact: str = Form(""), website: str = Form(""), session: Session = Depends(get_session)):
    if website:                       # a field people cannot see; only scripts fill it
        return RedirectResponse("/repair?" + urlencode({"ok": "已收到"}), status_code=303)
    e = repairs.submit_form(session, area, equipment, problem, slot, contact)
    message = repairs.EMERGENCY if e.status == "handoff" else "已收到您的報修資料，我們會由專人與您聯絡並確認到府時間。這不代表預約已成立。"
    return RedirectResponse("/repair?" + urlencode({"ok": message}), status_code=303)


@app.post("/line/webhook")
async def line_webhook(request: Request, session: Session = Depends(get_session)):
    secret = os.environ.get("ONEFLOW_LINE_SECRET")
    if not secret:
        raise HTTPException(503, "LINE channel secret is not configured")
    body = await request.body()
    if not repairs.line_signature_ok(body, request.headers.get("x-line-signature"), secret):
        raise HTTPException(403, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "not JSON") from exc
    return {"handled": repairs.line_events(session, payload)}


@app.get("/repairs")
def repairs_list(request: Request, user: User = Depends(member("repairs")), session: Session = Depends(get_session)):
    rows = session.scalars(select(Enquiry).order_by(Enquiry.id.desc()).limit(300)).all()
    return page(request, "repairs.html", user, rows=rows, missing=repairs.missing)


@app.get("/repairs/{enquiry_id}")
def enquiry_page(request: Request, enquiry_id: int, user: User = Depends(member("repairs")), session: Session = Depends(get_session)):
    e = session.get(Enquiry, enquiry_id)
    if not e:
        raise HTTPException(404)
    return page(request, "enquiry.html", user, e=e, missing=repairs.missing(e), history=base.history(session, "repairs", "enquiry", e.id),
                names={u.id: u.name for u in session.scalars(select(User))},
                handoffs=session.scalars(select(Handoff).where(Handoff.flow == "repairs", Handoff.entity_id == e.id)).all())


@app.post("/repairs/{enquiry_id}/{action}")
def enquiry_action(request: Request, enquiry_id: int, action: str, field: str = Form(""), value: str = Form(""),
                   user: User = Depends(member("repairs")), session: Session = Depends(get_session)):
    if not session.get(Enquiry, enquiry_id):
        raise HTTPException(404)
    if action == "edit":
        repairs.edit(session, user, enquiry_id, field, value)
        return back(request, ok="已修改")
    if action == "confirm":
        repairs.confirm(session, user, enquiry_id, value)
        return back(request, ok="已確認預約。請由專人通知客戶，系統不會自動通知")
    if action == "close":
        repairs.close(session, user, enquiry_id, value)
        return back(request, ok="已結案")
    raise HTTPException(404)
