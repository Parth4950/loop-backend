"""SQLAlchemy 2.0 models mapping the existing Loop database tables.

These mirror tables that already exist in Supabase Postgres. Do NOT call
Base.metadata.create_all — the schema is owned and migrated elsewhere.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import ForeignKey, Numeric, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str]
    phone: Mapped[Optional[str]]
    email: Mapped[Optional[str]]
    city: Mapped[Optional[str]]
    created_at: Mapped[datetime]


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str]
    segment_filters: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    compiled_sql: Mapped[Optional[str]]
    message: Mapped[Optional[str]]
    channel: Mapped[Optional[str]]
    status: Mapped[str]
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    projected_metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    mid_campaign_recommendation: Mapped[Optional[str]]
    created_at: Mapped[datetime]


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric)
    campaign_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("campaigns.id"))
    created_at: Mapped[datetime]


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id"))
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str]
    retry_count: Mapped[int]
    last_event_at: Mapped[Optional[datetime]]


class MessageEvent(Base):
    __tablename__ = "message_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id"))
    event_type: Mapped[str]
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    received_at: Mapped[datetime]
