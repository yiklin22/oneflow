import pytest

from oneflow import base
from oneflow.base import Refused
from oneflow.models import Handoff


def test_password_and_login(session, admin):
    assert base.log_in(session, "admin@example.test", "wrong") is None
    token = base.log_in(session, "ADMIN@example.test ", "correct horse battery")
    assert base.user_for(session, token).id == admin.id
    base.log_out(session, token)
    assert base.user_for(session, token) is None and base.user_for(session, "made up") is None


def test_real_documents_need_a_live_consent_for_that_flow(session, admin):
    with pytest.raises(Refused):
        base.keep_source(session, admin, "orders", "order", "a.pdf", b"real one", data_class="real")
    other = base.record_consent(session, admin, "某客戶", "repairs", "2026-11-10 簽署同意書")
    with pytest.raises(Refused):
        base.keep_source(session, admin, "orders", "order", "a.pdf", b"real one", "real", other.id)
    consent = base.record_consent(session, admin, "某客戶", "orders", "2026-11-10 簽署同意書")
    source, is_new = base.keep_source(session, admin, "orders", "order", "a.pdf", b"real one", "real", consent.id)
    assert is_new and source.consent_id == consent.id
    base.revoke_consent(session, admin, consent.id)
    with pytest.raises(Refused):
        base.keep_source(session, admin, "orders", "order", "b.pdf", b"real two", "real", consent.id)
    with pytest.raises(Refused):
        base.record_consent(session, admin, "某客戶", "orders", "  ")


def test_same_bytes_are_kept_once_per_flow(session, admin):
    first, new1 = base.keep_source(session, admin, "orders", "order", "a.pdf", b"same")
    again, new2 = base.keep_source(session, admin, "orders", "order", "renamed.pdf", b"same")
    elsewhere, new3 = base.keep_source(session, admin, "receivables", "ar", "a.pdf", b"same")
    assert (new1, new2, new3) == (True, False, True) and first.id == again.id != elsewhere.id


def test_an_action_key_can_be_claimed_once(session, admin):
    assert base.claim(session, "send:1") and not base.claim(session, "send:1") and base.claim(session, "send:2")
    session.commit()


def test_handoff_is_opened_once_and_closed_with_a_reason(session, admin):
    a = base.hand_off(session, "orders", "order", 7, "unreadable", "沒有文字")
    b = base.hand_off(session, "orders", "order", 7, "unreadable", "沒有文字")
    session.commit()
    assert a.id == b.id and session.query(Handoff).count() == 1
    base.take_handoff(session, admin, a.id)
    with pytest.raises(Refused):
        base.close_handoff(session, admin, a.id, " ")
    base.close_handoff(session, admin, a.id, "已人工輸入")
    assert session.get(Handoff, a.id).status == "closed"
    assert base.hand_off(session, "orders", "order", 7, "unreadable").id != a.id


def test_roles_limit_flows(session, admin):
    from oneflow.models import User
    sales = session.query(User).filter_by(role="sales").one()
    assert base.may(sales, "orders") and not base.may(sales, "receivables") and base.may(admin, "repairs")
