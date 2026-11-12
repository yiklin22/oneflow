import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from oneflow import acceptance
from oneflow.models import Enquiry, Order, Reminder
from tests.conftest import PASSWORD

ORDERS = acceptance.FIXTURES / "orders"
AR = acceptance.FIXTURES / "receivables"
MANIFEST = json.loads((ORDERS / "manifest.json").read_text())["orders"]
CLEAN = next(o["file"] for o in MANIFEST if o["expect"] == "review" and o["layout"] == "A" and not any(l["planted"] for l in o["lines"]))
TYPO = next(o["file"] for o in MANIFEST for l in o["lines"] if "typo" in l["planted"])


def post_file(client, url, path, field="files", **data):
    return client.post(url, files={field: (path.name, path.read_bytes())}, data=data, follow_redirects=True)


def load_tables(client):
    for kind in ("products", "customer_parts"):
        assert "已匯入" in post_file(client, "/orders/tables", ORDERS / f"{kind}.csv", field="file", kind=kind).text


def test_pages_need_a_login_except_the_public_ones(session, admin):
    from oneflow.app import app
    with TestClient(app) as anon:
        for path in ("/", "/orders", "/receivables", "/repairs", "/handoffs", "/changes", "/consents", "/orders-export/1.csv"):
            r = anon.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path
        assert anon.post("/orders-export", follow_redirects=False).headers["location"] == "/login"
        assert "線上報修" in anon.get("/repair").text and 'lang="zh-Hant-TW"' in anon.get("/login").text
        assert "帳號或密碼不正確" in anon.post("/login", data={"email": "admin@example.test", "password": "no"}, follow_redirects=True).text


def test_roles_and_cross_site_posts(session, admin):
    from oneflow.app import app
    with TestClient(app) as sales:
        sales.post("/login", data={"email": "sales@example.test", "password": PASSWORD})
        assert sales.get("/orders").status_code == 200
        assert sales.get("/receivables").status_code == 403 and sales.get("/repairs").status_code == 403
        assert "應收帳款" not in sales.get("/").text
        assert sales.post("/orders-export", headers={"origin": "https://evil.example"}).status_code == 403


def test_order_from_upload_to_export(client, session):
    load_tables(client)
    r = post_file(client, "/orders/upload", ORDERS / "files" / CLEAN)
    assert "讀入 1 張訂單" in r.text
    assert "未重複處理" in post_file(client, "/orders/upload", ORDERS / "files" / CLEAN).text
    order = session.query(Order).one()
    page = client.get(f"/orders/{order.id}").text
    assert "原文" in page and order.po_no in page and "第 1 項" in page and "確認這張訂單" in page
    assert "已確認" in client.post(f"/orders/{order.id}/confirm", follow_redirects=True).text
    assert "已產生匯出批次 1" in client.post("/orders-export", follow_redirects=True).text
    utf8 = client.get("/orders-export/1.csv")
    assert utf8.content.startswith(b"\xef\xbb\xbf") and order.po_no in utf8.text
    assert "客戶".encode("cp950") in client.get("/orders-export/1.csv?encoding=big5").content
    assert "沒有已確認而尚未匯出的訂單" in client.post("/orders-export", follow_redirects=True).text


def test_blocked_order_shows_why_and_edit_is_recorded(client, session):
    load_tables(client)
    post_file(client, "/orders/upload", ORDERS / "files" / TYPO)
    order = session.query(Order).one()
    page = client.get(f"/orders/{order.id}").text
    assert "必須處理" in page and "商品表查無料號" in page and "disabled" in page
    assert "必須處理的問題" in client.post(f"/orders/{order.id}/confirm", follow_redirects=True).text
    line = next(l for l in order.lines if any(i["blocking"] for i in l.issues))
    r = client.post(f"/orders/lines/{line.id}/edit", data={"field": "part_no", "value": "不存在"}, follow_redirects=True,
                    headers={"referer": f"http://testserver/orders/{order.id}"})
    assert "商品表沒有料號" in r.text
    r = client.post(f"/orders/lines/{line.id}/edit", data={"field": "part_no", "value": "NT-M4-ZN"}, follow_redirects=True,
                    headers={"referer": f"http://testserver/orders/{order.id}"})
    assert "已修改" in r.text and "NT-M4-ZN" in r.text and "管理者" in r.text      # history shows who
    assert "order_line" in client.get("/changes").text


def test_real_upload_needs_a_consent_record(client, session):
    load_tables(client)
    r = post_file(client, "/orders/upload", ORDERS / "files" / CLEAN, data_class="real")
    assert "資料使用同意" in r.text and session.query(Order).count() == 0
    assert "已記錄同意" in client.post("/consents", data={"party": "禾臻精密", "scope": "orders", "basis": "2026-11-10 簽署同意書"}, follow_redirects=True).text
    r = post_file(client, "/orders/upload", ORDERS / "files" / CLEAN, data_class="real", consent_id="1")
    assert "讀入 1 張訂單" in r.text and "真實" in r.text


def test_receivables_pages(client, session, tmp_path, monkeypatch):
    monkeypatch.setenv("ONEFLOW_OUTBOX", str(tmp_path))
    assert "新增 60 筆" in post_file(client, "/receivables/files", AR / "ar-2026-11-30.csv", kind="export").text
    mails = [("files", (p.name, p.read_bytes())) for p in sorted((AR / "mails").iterdir())]
    assert "讀入 15 封郵件" in client.post("/receivables/files", files=mails, data={"kind": "mail"}, follow_redirects=True).text
    page = client.get("/receivables?as_of=2026-11-30").text
    for heading in ("無法歸戶", "核對：客戶稱已付款", "爭議：不催收", "有來信待閱讀", "資料有誤", "缺件"):
        assert heading in page, heading
    assert "產生 14 份草稿" in client.post("/receivables/draft", data={"as_of": "2026-11-30"}, follow_redirects=True).text
    draft = session.query(Reminder).first()
    r = client.post(f"/receivables/reminders/{draft.id}/approve", data={"as_of": "2026-11-30"}, follow_redirects=True)
    assert "尚未歸戶" in r.text
    assert "已歸戶" in client.post("/receivables/signals/14/assign", data={"customer": "宏碩模具有限公司"}, follow_redirects=True).text
    ok = session.query(Reminder).filter_by(status="draft").first()
    assert "已核准" in client.post(f"/receivables/reminders/{ok.id}/approve", data={"as_of": "2026-11-30"}, follow_redirects=True).text
    assert "已寫入寄件匣" in client.post(f"/receivables/reminders/{ok.id}/send", data={"as_of": "2026-11-30"}, follow_redirects=True).text
    assert len(list(tmp_path.iterdir())) == 1
    item_page = client.get(f"/receivables/items/{ok.item.id}?as_of=2026-11-30").text
    assert "原文" in item_page and "已寫入寄件匣" in item_page


def test_repair_form_and_line_webhook(client, session, monkeypatch):
    form = dict(area="台中市西屯區", equipment="冷氣", problem="不冷", slot="週三下午", contact="0912-000-000")
    assert "不代表預約已成立" in client.post("/repair", data=form, follow_redirects=True).text
    client.post("/repair", data=dict(form, website="http://spam.example"))
    assert session.query(Enquiry).count() == 1
    assert "請填寫" in client.post("/repair", data=dict(form, problem=""), follow_redirects=True, headers={"referer": "http://testserver/repair"}).text

    body = json.dumps({"events": [{"type": "message", "webhookEventId": "E1", "replyToken": "T", "timestamp": 1794880000000,
                                   "source": {"userId": "U1"}, "message": {"type": "text", "text": "冰箱不冷 高雄市鳳山區"}}]}).encode()
    assert client.post("/line/webhook", content=body).status_code == 503
    monkeypatch.setenv("ONEFLOW_LINE_SECRET", "s3cret")
    assert client.post("/line/webhook", content=body, headers={"x-line-signature": "nope"}).status_code == 403
    good = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    assert client.post("/line/webhook", content=body, headers={"x-line-signature": good}).json() == {"handled": 1}
    assert client.post("/line/webhook", content=body, headers={"x-line-signature": good}).json() == {"handled": 0}
    listing = client.get("/repairs").text
    assert "高雄市鳳山區" in listing and "方便時間" in listing
    e = session.query(Enquiry).filter_by(channel="form").one()
    page = client.get(f"/repairs/{e.id}").text
    assert "接單摘要" in page and "原文" in page
    r = client.post(f"/repairs/{e.id}/confirm", data={"value": "2030-01-05T14:00"}, follow_redirects=True)
    assert "已確認預約" in r.text and "2030-01-05 14:00" in client.get(f"/repairs/{e.id}").text
    assert "人工接手" in client.get("/handoffs").text
