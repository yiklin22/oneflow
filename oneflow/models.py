"""All tables. The first block is the shared base; then one block per flow."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


Stamp = DateTime(timezone=True)

# ---------------------------------------------------------------- shared base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))          # admin | sales | accounting | dispatch
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Login(Base):
    __tablename__ = "logins"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(Stamp)


class Consent(Base):
    """A recorded permission to load one party's real documents into one flow."""
    __tablename__ = "consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    party: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(20))         # orders | receivables | repairs | all
    basis: Mapped[str] = mapped_column(Text)               # how it was obtained, in the recorder's words
    recorded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(Stamp, default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(Stamp)


class Source(Base):
    """One received file or message batch, kept byte for byte for side-by-side review."""
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("flow", "sha256"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    flow: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    data_class: Mapped[str] = mapped_column(String(10))    # synthetic | real
    consent_id: Mapped[int | None] = mapped_column(ForeignKey("consents.id"))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    text: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Stamp, default=now)


class Change(Base):
    """Edit history: who changed which field from what to what."""
    __tablename__ = "changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    flow: Mapped[str] = mapped_column(String(20))
    entity: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(40))
    old: Mapped[str | None] = mapped_column(Text)
    new: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))   # None = the system
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(Stamp, default=now)


class Handoff(Base):
    """Something the program could not or must not finish; a person takes it."""
    __tablename__ = "handoffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    flow: Mapped[str] = mapped_column(String(20))
    entity: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="open")      # open | taken | closed
    opened_at: Mapped[datetime] = mapped_column(Stamp, default=now)
    taken_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(Stamp)
    resolution: Mapped[str] = mapped_column(Text, default="")


class ActionKey(Base):
    """One row per outward action; the primary key makes a repeat impossible."""
    __tablename__ = "action_keys"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    at: Mapped[datetime] = mapped_column(Stamp, default=now)


# --------------------------------------------------------------------- orders

class Product(Base):
    __tablename__ = "products"
    part_no: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(10))
    pack_unit: Mapped[str | None] = mapped_column(String(10))
    pack_qty: Mapped[int | None] = mapped_column(Integer)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CustomerPart(Base):
    __tablename__ = "customer_parts"
    __table_args__ = (UniqueConstraint("customer", "customer_part_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    customer: Mapped[str] = mapped_column(String(200))
    customer_part_no: Mapped[str] = mapped_column(String(60))
    part_no: Mapped[str] = mapped_column(ForeignKey("products.part_no"))


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    customer: Mapped[str | None] = mapped_column(String(200))
    po_no: Mapped[str | None] = mapped_column(String(80))
    order_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))        # needs_review | duplicate | handoff | confirmed | exported
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    issues: Mapped[list] = mapped_column(JSON, default=list)
    opened_at: Mapped[datetime | None] = mapped_column(Stamp)
    confirmed_at: Mapped[datetime | None] = mapped_column(Stamp)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    export_batch_id: Mapped[int | None] = mapped_column(ForeignKey("export_batches.id"))
    lines: Mapped[list["OrderLine"]] = relationship(order_by="OrderLine.line_no", cascade="all, delete-orphan")
    source: Mapped[Source] = relationship()


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSON)                # the cells as written by the customer
    evidence: Mapped[dict] = mapped_column(JSON)           # page, row, row text
    part_no: Mapped[str | None] = mapped_column(String(60))
    qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    due_date: Mapped[date | None] = mapped_column(Date)
    match_kind: Mapped[str] = mapped_column(String(20))
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)   # {field, code, msg, blocking}


class ExportBatch(Base):
    __tablename__ = "export_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Stamp, default=now)
    order_count: Mapped[int] = mapped_column(Integer)
    csv: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------- receivables

class ArItem(Base):
    __tablename__ = "ar_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)   # customer + invoice number, or a row hash
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    row_no: Mapped[int] = mapped_column(Integer)
    row_text: Mapped[str] = mapped_column(Text)
    customer: Mapped[str] = mapped_column(String(200))
    invoice_no: Mapped[str | None] = mapped_column(String(40))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    received: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    contact_email: Mapped[str | None] = mapped_column(String(200))
    has_receipt: Mapped[bool | None] = mapped_column(Boolean)    # signed delivery receipt on file
    issues: Mapped[list] = mapped_column(JSON, default=list)     # rule failures and missing pieces
    hold: Mapped[str | None] = mapped_column(String(30))         # claims_paid | dispute | customer_claims_paid | manual
    reminders: Mapped[list["Reminder"]] = relationship(order_by="Reminder.id")


class Mail(Base):
    __tablename__ = "mails"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    message_id: Mapped[str] = mapped_column(String(300), unique=True)
    sender: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(Stamp)
    body: Mapped[str] = mapped_column(Text)


class MailSignal(Base):
    """A sentence in a mail that bears on an item (or, with no invoice number, on a customer)."""
    __tablename__ = "mail_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    mail_id: Mapped[int] = mapped_column(ForeignKey("mails.id"))
    ar_item_id: Mapped[int | None] = mapped_column(ForeignKey("ar_items.id"))
    customer: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))          # claims_paid | dispute | promise
    linked_by: Mapped[str] = mapped_column(String(20))     # invoice_no | sender
    evidence: Mapped[str] = mapped_column(Text)
    mail: Mapped[Mail] = relationship()


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (UniqueConstraint("ar_item_id", "stage"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ar_item_id: Mapped[int] = mapped_column(ForeignKey("ar_items.id"))
    stage: Mapped[int] = mapped_column(Integer)            # 1: 1-30 days, 2: 31-60, 3: over 60
    to: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="draft")   # draft | approved | sent | cancelled
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    sent_at: Mapped[datetime | None] = mapped_column(Stamp)
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    item: Mapped[ArItem] = relationship(back_populates="reminders")


# -------------------------------------------------------------------- repairs

class Enquiry(Base):
    __tablename__ = "enquiries"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(10))       # form | line
    external_user: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(12), default="collecting")  # collecting | ready | handoff | confirmed | closed
    area: Mapped[str | None] = mapped_column(String(100))
    equipment: Mapped[str | None] = mapped_column(String(100))
    problem: Mapped[str | None] = mapped_column(Text)
    slot_text: Mapped[str | None] = mapped_column(String(200))   # the customer's own words
    slot_date: Mapped[date | None] = mapped_column(Date)         # set only when the words are unambiguous
    slot_part: Mapped[str | None] = mapped_column(String(10))
    contact: Mapped[str | None] = mapped_column(String(100))
    after_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    asked: Mapped[dict] = mapped_column(JSON, default=dict)      # field -> times asked
    appointment: Mapped[datetime | None] = mapped_column(Stamp)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Stamp, default=now)
    updated_at: Mapped[datetime] = mapped_column(Stamp, default=now)
    messages: Mapped[list["Message"]] = relationship(order_by="Message.id", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(ForeignKey("enquiries.id"))
    direction: Mapped[str] = mapped_column(String(3))      # in | out
    text: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(String(100), unique=True)   # LINE webhookEventId
    delivered: Mapped[bool | None] = mapped_column(Boolean)                  # out only; None = nothing to deliver to
    at: Mapped[datetime] = mapped_column(Stamp, default=now)
