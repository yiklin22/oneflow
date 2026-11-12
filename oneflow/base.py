"""The shared base under all three flows: login, consent gate, kept sources,
edit history, repeat protection and hand-off to a person."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import ActionKey, Change, Consent, Handoff, Login, Source, User, now

FLOWS = {"orders": "訂單輸入", "receivables": "應收帳款", "repairs": "維修接單"}
ROLES = {"admin": "管理者", "sales": "業務助理", "accounting": "會計", "dispatch": "調度"}
ROLE_FLOWS = {"admin": set(FLOWS), "sales": {"orders"}, "accounting": {"receivables"}, "dispatch": {"repairs"}}


class Refused(Exception):
    """The base said no. The message is shown to the user as is."""


# ---------------------------------------------------------------------- login

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), digest)


def add_user(session: Session, email: str, name: str, role: str, password: str) -> User:
    if role not in ROLES:
        raise Refused(f"沒有這個角色：{role}")
    user = User(email=email.strip().lower(), name=name, role=role, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    return user


def log_in(session: Session, email: str, password: str) -> str | None:
    """Returns a session token, or None. The database keeps only the token's hash."""
    user = session.scalar(select(User).where(User.email == email.strip().lower(), User.active))
    if not user or not verify_password(password, user.password_hash):
        return None
    token = secrets.token_urlsafe(32)
    session.add(Login(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                      expires_at=now() + timedelta(hours=12)))
    session.commit()
    return token


def user_for(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    login = session.get(Login, hashlib.sha256(token.encode()).hexdigest())
    if not login or login.expires_at < now():
        return None
    user = session.get(User, login.user_id)
    return user if user and user.active else None


def log_out(session: Session, token: str | None) -> None:
    if token and (login := session.get(Login, hashlib.sha256(token.encode()).hexdigest())):
        session.delete(login)
        session.commit()


def may(user: User, flow: str) -> bool:
    return flow in ROLE_FLOWS.get(user.role, set())


# -------------------------------------------------------------------- consent

def record_consent(session: Session, user: User, party: str, scope: str, basis: str) -> Consent:
    if scope not in FLOWS and scope != "all":
        raise Refused(f"沒有這個範圍：{scope}")
    if not party.strip() or not basis.strip():
        raise Refused("同意紀錄必須寫明對象與取得方式")
    consent = Consent(party=party.strip(), scope=scope, basis=basis.strip(), recorded_by=user.id)
    session.add(consent)
    session.commit()
    return consent


def revoke_consent(session: Session, user: User, consent_id: int) -> None:
    consent = session.get(Consent, consent_id)
    if consent and not consent.revoked_at:
        consent.revoked_at = now()
        log_change(session, "base", "consent", consent.id, "revoked_at", None, str(consent.revoked_at), user)
        session.commit()


def active_consents(session: Session, flow: str) -> list[Consent]:
    return list(session.scalars(select(Consent).where(
        Consent.revoked_at.is_(None), Consent.scope.in_([flow, "all"])).order_by(Consent.id)))


# -------------------------------------------------------------------- sources

def keep_source(session: Session, user: User | None, flow: str, kind: str, filename: str, content: bytes,
                data_class: str = "synthetic", consent_id: int | None = None) -> tuple[Source, bool]:
    """Store a received file. Returns (source, is_new).

    Real documents are refused unless an unrevoked consent covering this flow
    is named. The same bytes arriving twice give back the first record.
    """
    if data_class not in ("synthetic", "real"):
        raise Refused("資料類別必須是 synthetic（模擬）或 real（真實）")
    if data_class == "real":
        consent = session.get(Consent, consent_id) if consent_id else None
        if not consent or consent.revoked_at or consent.scope not in (flow, "all"):
            raise Refused("真實文件必須先有涵蓋此流程、未撤回的資料使用同意紀錄")
    else:
        consent_id = None
    sha = hashlib.sha256(content).hexdigest()
    found = session.scalar(select(Source).where(Source.flow == flow, Source.sha256 == sha))
    if found:
        return found, False
    source = Source(flow=flow, kind=kind, filename=filename, sha256=sha, data_class=data_class,
                    consent_id=consent_id, content=content, uploaded_by=user.id if user else None)
    session.add(source)
    try:
        session.flush()
    except IntegrityError:            # another request stored the same bytes first
        session.rollback()
        return session.scalar(select(Source).where(Source.flow == flow, Source.sha256 == sha)), False
    return source, True


# ----------------------------------------------- history, repeats, hand-off

def log_change(session: Session, flow: str, entity: str, entity_id: int, field: str, old, new,
               user: User | None, note: str = "") -> None:
    session.add(Change(flow=flow, entity=entity, entity_id=entity_id, field=field,
                       old=None if old is None else str(old), new=None if new is None else str(new),
                       user_id=user.id if user else None, note=note))


def history(session: Session, flow: str, entity: str, entity_id: int) -> list[Change]:
    return list(session.scalars(select(Change).where(
        Change.flow == flow, Change.entity == entity, Change.entity_id == entity_id).order_by(Change.id)))


def claim(session: Session, key: str) -> bool:
    """True the first time `key` is claimed, False ever after. Uses a savepoint,
    so the caller's transaction survives a refusal."""
    try:
        with session.begin_nested():
            session.add(ActionKey(key=key))
        return True
    except IntegrityError:
        return False


def hand_off(session: Session, flow: str, entity: str, entity_id: int, reason: str, detail: str = "") -> Handoff:
    open_one = session.scalar(select(Handoff).where(
        Handoff.flow == flow, Handoff.entity == entity, Handoff.entity_id == entity_id,
        Handoff.reason == reason, Handoff.status != "closed"))
    if open_one:
        return open_one
    h = Handoff(flow=flow, entity=entity, entity_id=entity_id, reason=reason, detail=detail)
    session.add(h)
    session.flush()
    return h


def take_handoff(session: Session, user: User, handoff_id: int) -> None:
    h = session.get(Handoff, handoff_id)
    if h and h.status == "open":
        h.status, h.taken_by = "taken", user.id
        session.commit()


def close_handoff(session: Session, user: User, handoff_id: int, resolution: str) -> None:
    h = session.get(Handoff, handoff_id)
    if not h or h.status == "closed":
        return
    if not resolution.strip():
        raise Refused("結案必須寫明處理結果")
    h.status, h.taken_by, h.closed_at, h.resolution = "closed", h.taken_by or user.id, now(), resolution.strip()
    session.commit()
