"""Server-Sent Events stream of live campaign progress.

On connect we emit a one-shot snapshot (every message + status aggregates),
then forward each pub/sub update as it happens, with periodic keepalives.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from starlette.responses import StreamingResponse

from . import events
from .db import AsyncSessionLocal
from .metrics import campaign_metrics
from .models import Campaign, Customer, Message

router = APIRouter()

KEEPALIVE_SECONDS = 15


async def _build_snapshot(campaign_id: UUID) -> dict:
    """Read the current message roster + aggregates for a campaign."""
    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="unknown campaign")

        rows = (
            await session.execute(
                select(Message, Customer)
                .join(Customer, Message.customer_id == Customer.id)
                .where(Message.campaign_id == campaign_id)
            )
        ).all()

    messages = [{"id": str(m.id), "customer_name": c.name, "status": m.status} for m, c in rows]

    status_counts: dict[str, int] = {}
    for m in messages:
        status_counts[m["status"]] = status_counts.get(m["status"], 0) + 1

    # Cumulative funnel + rates from the shared helper — identical math to /analyze.
    aggregates = campaign_metrics(status_counts)
    aggregates["total"] = len(messages)
    aggregates["queued"] = status_counts.get("queued", 0)
    aggregates["retrying"] = status_counts.get("retrying", 0)

    # Expose the stored channel so the tracker badge reads the single source of
    # truth (campaign.channel) rather than inferring it.
    return {
        "type": "snapshot",
        "channel": campaign.channel,
        "messages": messages,
        "aggregates": aggregates,
    }


@router.get("/campaigns/{campaign_id}/stream")
async def stream(campaign_id: str):
    try:
        cid = UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="campaign_id must be a UUID")

    key = str(cid)
    snapshot = await _build_snapshot(cid)

    async def event_gen():
        queue = events.subscribe(key)
        try:
            yield f"data: {json.dumps(snapshot)}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # default to "update", but preserve an explicit type (e.g. "recommendation")
                yield f"data: {json.dumps({'type': 'update', **data})}\n\n"
        finally:
            events.unsubscribe(key, queue)

    return StreamingResponse(event_gen(), media_type="text/event-stream")
