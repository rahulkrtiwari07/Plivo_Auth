"""Local records that map Plivo resources (owned by OUR master account) to dashboard clients."""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClientAccount(Base):
    """A dashboard client and the Plivo subaccount that holds its KYC and numbers."""

    __tablename__ = "client_accounts"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    plivo_subaccount_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ComplianceApplication(Base):
    """One KYC submission by one client, mirrored from Plivo's PhoneNumber/Compliance resource."""

    __tablename__ = "compliance_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    plivo_compliance_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    plivo_subaccount_id: Mapped[str | None] = mapped_column(String(64))  # None = filed on the main account
    alias: Mapped[str] = mapped_column(String(99))
    country_iso: Mapped[str] = mapped_column(String(2))
    number_type: Mapped[str] = mapped_column(String(16))
    user_type: Mapped[str] = mapped_column(String(16))  # individual | business
    # draft | submitted | accepted | rejected | suspended (UCC complaints) | expired
    status: Mapped[str] = mapped_column(String(16), default="submitted", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    end_user: Mapped[dict] = mapped_column(JSON, default=dict)
    documents_meta: Mapped[list] = mapped_column(JSON, default=list)  # [{document_type_id, name, filename}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    numbers: Mapped[list["PhoneNumber"]] = relationship(back_populates="application")


class PhoneNumber(Base):
    """A Plivo number rented on our master account on behalf of a client."""

    __tablename__ = "phone_numbers"
    # A number can be held by only one client at a time; released/failed rows are history.
    __table_args__ = (
        Index("uq_phone_number_live", "number", unique=True,
              sqlite_where=text("status IN ('pending','pending_activation','active')"),
              postgresql_where=text("status IN ('pending','pending_activation','active')")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[str] = mapped_column(String(32))  # E.164 without '+', as Plivo returns it
    number_type: Mapped[str] = mapped_column(String(16))
    country_iso: Mapped[str] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))
    alias: Mapped[str | None] = mapped_column(String(99))
    monthly_rental_rate: Mapped[float] = mapped_column(Float, default=0.0)  # Plivo cost
    setup_rate: Mapped[float] = mapped_column(Float, default=0.0)  # Plivo cost
    # pending (buy in flight) | pending_activation (Plivo accepted, not yet active) | active | failed | released
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("compliance_applications.id"))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Idempotency-Key sent by the integrating system on POST .../numbers (unique per client).
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    application: Mapped[ComplianceApplication] = relationship(back_populates="numbers")


class NumberOffer(Base):
    """Numbers recently shown to a client in a search. A client can only buy a number it was offered,
    and the price is taken from here, not from the caller. Stored in the DB (not memory) so search and
    buy may hit different worker processes."""

    __tablename__ = "number_offers"
    __table_args__ = (UniqueConstraint("client_id", "number", name="uq_offer_client_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[str] = mapped_column(String(32))
    number_type: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # Plivo search object
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WebhookEvent(Base):
    """Outbox of event notifications for the integrating system, delivered with retries."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # evt_...
    type: Mapped[str] = mapped_column(String(64), index=True)
    client_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    # pending | delivered | failed (gave up after max attempts)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LedgerEntry(Base):
    """Charges to bill back to clients (setup + monthly rental). Feed this into your wallet/invoicing."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))  # setup | monthly_rental | release
    plivo_cost: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # what you charge (cost * markup)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
