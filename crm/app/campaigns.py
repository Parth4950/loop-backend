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
from .metrics import campaign_metrics
from .models import Campaign, Message, Order

router = APIRouter()


@router.get("/campaigns")
async def list_campaigns() -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        campaigns = (
            (await session.execute(select(Campaign).order_by(Campaign.created_at.desc())))
            .scalars()
            .all()
        )
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

    revenue_by: dict[UUID, float] = {cid: float(rev or 0) for cid, rev in revenue_rows}

    result: list[dict[str, Any]] = []
    for c in campaigns:
        statuses = status_by.get(c.id, {})
        total = sum(statuses.values())
        # Same cumulative funnel + rates as the SSE tracker and /analyze.
        metrics = campaign_metrics(statuses)
        if metrics["sent"] <= 0:
            continue  # not launched yet

        result.append(
            {
                "id": str(c.id),
                "name": c.name,
                "channel": c.channel,
                "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "audience_size": total,
                **metrics,
                "revenue": int(round(revenue_by.get(c.id, 0.0))),
            }
        )

    return result
