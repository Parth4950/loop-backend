"""Read-only listing of launched campaigns for the Past Campaigns UI.

A campaign is "launched" once at least one of its messages has left the queue
(status != 'queued'). All aggregates are computed with grouped queries (no N+1).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import func, select

from .db import AsyncSessionLocal
from .models import Campaign, Message, MessageEvent, Order

router = APIRouter()


def _safe_rate(numerator: int, denominator: int) -> float:
    """Percentage rounded to 1 dp; 0.0 when the denominator is zero (no NaN)."""
    return round(100 * numerator / denominator, 1) if denominator else 0.0


@router.get("/campaigns")
async def list_campaigns() -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        campaigns = (
            await session.execute(select(Campaign).order_by(Campaign.created_at.desc()))
        ).scalars().all()
        if not campaigns:
            return []

        ids = [c.id for c in campaigns]

        status_rows = (
            await session.execute(
                select(Message.campaign_id, Message.status, func.count())
                .where(Message.campaign_id.in_(ids))
                .group_by(Message.campaign_id, Message.status)
            )
        ).all()

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

        revenue_rows = (
            await session.execute(
                select(Order.campaign_id, func.coalesce(func.sum(Order.amount), 0))
                .where(Order.campaign_id.in_(ids))
                .group_by(Order.campaign_id)
            )
        ).all()

    status_by: dict[UUID, dict[str, int]] = {}
    for campaign_id, status, count in status_rows:
        status_by.setdefault(campaign_id, {})[status] = count

    events_by: dict[UUID, dict[str, int]] = {}
    for campaign_id, event_type, count in event_rows:
        events_by.setdefault(campaign_id, {})[event_type] = count

    revenue_by: dict[UUID, float] = {cid: float(rev or 0) for cid, rev in revenue_rows}

    result: list[dict[str, Any]] = []
    for c in campaigns:
        statuses = status_by.get(c.id, {})
        total = sum(statuses.values())
        sent = total - statuses.get("queued", 0)  # everything that left the queue
        if sent <= 0:
            continue  # not launched yet

        ev = events_by.get(c.id, {})
        delivered = ev.get("delivered", 0)
        read = ev.get("read", 0)
        opened = ev.get("opened", 0)
        clicked = ev.get("clicked", 0)
        converted = ev.get("converted", 0)
        failed = ev.get("failed", 0)

        result.append(
            {
                "id": str(c.id),
                "name": c.name,
                "channel": c.channel,
                "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "audience_size": total,
                "sent": sent,
                "delivered": delivered,
                "read": read,
                "opened": opened,
                "clicked": clicked,
                "converted": converted,
                "failed": failed,
                "open_rate": _safe_rate(opened, delivered),
                "click_rate": _safe_rate(clicked, opened),
                "conversion_rate": _safe_rate(converted, clicked),
                "revenue": int(round(revenue_by.get(c.id, 0.0))),
            }
        )

    return result
