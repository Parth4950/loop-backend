"""The four agent tools as real, DB-hitting async functions.

The Gemini model decides the arguments; these functions do the work. All SQL
over customer/order data is PARAMETERIZED (bound params via SQLAlchemy) — values
are never string-interpolated into the query. ``compiled_sql`` is a separate,
display-only rendering of the WHERE clause.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import and_, func, select

from ..db import AsyncSessionLocal
from ..models import Campaign, Customer, Message, MessageEvent, Order

# Absolute per-channel rates: (reach, open, click).
CHANNEL_RATES: dict[str, tuple[float, float, float]] = {
    "whatsapp": (0.93, 0.62, 0.28),
    "sms": (0.87, 0.41, 0.14),
    "email": (0.79, 0.35, 0.09),
}


def _orders_agg():
    """Subquery: per-customer total_spend, last_order, order_count."""
    return (
        select(
            Order.customer_id.label("customer_id"),
            func.sum(Order.amount).label("total_spend"),
            func.max(Order.created_at).label("last_order"),
            func.count().label("order_count"),
        )
        .group_by(Order.customer_id)
        .subquery()
    )


def _conditions(agg, filters: dict[str, Any]) -> tuple[list, str]:
    """Translate provided filters into bound SQLAlchemy conditions + a readable
    WHERE clause for display. Only provided keys are applied."""
    f = filters or {}
    conds: list = []
    display: list[str] = []

    if f.get("min_total_spend") is not None:
        conds.append(agg.c.total_spend > f["min_total_spend"])
        display.append(f"total_spend > {f['min_total_spend']:g}")
    if f.get("max_total_spend") is not None:
        conds.append(agg.c.total_spend < f["max_total_spend"])
        display.append(f"total_spend < {f['max_total_spend']:g}")
    if f.get("days_inactive") is not None:
        days = int(f["days_inactive"])
        # bound param via make_interval — no string interpolation into SQL
        conds.append(agg.c.last_order < func.now() - func.make_interval(0, 0, 0, days))
        display.append(f"last_order < now() - interval '{days} days'")
    if f.get("min_orders") is not None:
        conds.append(agg.c.order_count >= f["min_orders"])
        display.append(f"order_count >= {int(f['min_orders'])}")
    if f.get("city"):
        conds.append(Customer.city == f["city"])
        display.append(f"city = '{f['city']}'")

    compiled_sql = " AND ".join(display) if display else "TRUE"
    return conds, compiled_sql


def estimate_metrics(segment_size: int, channel: str) -> dict[str, int]:
    """Deterministic projection from absolute per-channel rates. No model call."""
    reach_r, open_r, click_r = CHANNEL_RATES.get(channel, CHANNEL_RATES["email"])
    return {
        "projected_reach": round(segment_size * reach_r),
        "projected_opens": round(segment_size * open_r),
        "projected_clicks": round(segment_size * click_r),
    }


async def segment_customers(filters: dict[str, Any]) -> dict[str, Any]:
    """Count + sample the audience matching the given filters."""
    agg = _orders_agg()
    conds, compiled_sql = _conditions(agg, filters)

    base = (
        select(Customer.name, agg.c.total_spend, agg.c.last_order)
        .join(agg, agg.c.customer_id == Customer.id)
        .where(and_(*conds))
    )

    async with AsyncSessionLocal() as session:
        count = await session.scalar(select(func.count()).select_from(base.subquery()))
        rows = (
            await session.execute(base.order_by(agg.c.total_spend.desc()).limit(5))
        ).all()

    sample = [
        {
            "name": r.name,
            "last_order": r.last_order.isoformat() if r.last_order else None,
            "total_spend": float(r.total_spend),
        }
        for r in rows
    ]
    return {"count": int(count or 0), "sample": sample, "compiled_sql": compiled_sql}


async def get_campaign_history() -> list[dict[str, Any]]:
    """Last 5 campaigns with audience size and engagement rates (empty if none)."""
    async with AsyncSessionLocal() as session:
        campaigns = (
            await session.execute(
                select(Campaign).order_by(Campaign.created_at.desc()).limit(5)
            )
        ).scalars().all()
        if not campaigns:
            return []

        ids = [c.id for c in campaigns]

        audience = dict(
            (
                await session.execute(
                    select(Message.campaign_id, func.count())
                    .where(Message.campaign_id.in_(ids))
                    .group_by(Message.campaign_id)
                )
            ).all()
        )

        event_rows = (
            await session.execute(
                select(
                    Message.campaign_id,
                    MessageEvent.event_type,
                    func.count(func.distinct(MessageEvent.message_id)),
                )
                .join(MessageEvent, MessageEvent.message_id == Message.id)
                .where(Message.campaign_id.in_(ids))
                .group_by(Message.campaign_id, MessageEvent.event_type)
            )
        ).all()

    events: dict[Any, dict[str, int]] = {}
    for cid, event_type, cnt in event_rows:
        events.setdefault(cid, {})[event_type] = cnt

    history = []
    for c in campaigns:
        size = audience.get(c.id, 0)
        ev = events.get(c.id, {})
        opened, clicked, converted = ev.get("opened", 0), ev.get("clicked", 0), ev.get("converted", 0)
        history.append(
            {
                "name": c.name,
                "channel": c.channel,
                "audience_size": size,
                "open_rate": round(opened / size, 3) if size else 0.0,
                "click_rate": round(clicked / size, 3) if size else 0.0,
                "conversions": converted,
            }
        )
    return history


async def create_campaign(
    segment_filters: dict[str, Any],
    message: str,
    channel: str,
    confidence: int,
    reason: str,
) -> dict[str, Any]:
    """Compile filters, materialize the audience, and persist the campaign +
    one queued message per matched customer."""
    agg = _orders_agg()
    conds, compiled_sql = _conditions(agg, segment_filters)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        customer_ids = (
            await session.execute(
                select(Customer.id)
                .join(agg, agg.c.customer_id == Customer.id)
                .where(and_(*conds))
            )
        ).scalars().all()

        audience_size = len(customer_ids)
        projected = estimate_metrics(audience_size, channel)

        campaign = Campaign(
            id=uuid4(),
            name=f"Loop {channel} campaign",
            segment_filters=segment_filters or {},
            compiled_sql=compiled_sql,
            message=message,
            channel=channel,
            status="pending_approval",
            confidence=confidence,
            projected_metrics=projected,
            created_at=now,
        )
        session.add(campaign)

        session.add_all(
            [
                Message(
                    id=uuid4(),
                    campaign_id=campaign.id,
                    customer_id=cid,
                    status="queued",
                    retry_count=0,
                    last_event_at=None,
                )
                for cid in customer_ids
            ]
        )
        await session.commit()
        campaign_id = str(campaign.id)

    return {
        "campaign_id": campaign_id,
        "audience_size": audience_size,
        "channel": channel,
        "confidence": confidence,
        "reason": reason,
        "message": message,
        "compiled_sql": compiled_sql,
        "segment_filters": segment_filters or {},
        "projected": projected,
    }
