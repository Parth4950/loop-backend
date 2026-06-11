"""Receipt webhook: the idempotent, forward-only message state machine.

The channel service POSTs lifecycle callbacks here. Each callback advances the
message along the ladder queued < sent < delivered < opened < clicked < converted,
never regressing, and is idempotent via the UNIQUE(message_id, event_type)
constraint on message_events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import events, monitor
from .db import get_session
from .models import Customer, Message, MessageEvent, Order

router = APIRouter()

# Forward-only ladder. Status values outside it (retrying/failed) rank below
# the ladder so any genuine progress event still upgrades them.
LADDER = ["queued", "sent", "delivered", "opened", "clicked", "converted"]
RANK = {name: i for i, name in enumerate(LADDER)}
LADDER_EVENTS = {"delivered", "opened", "clicked", "converted"}


def _rank(status: Optional[str]) -> int:
    return RANK.get(status or "", -1)


class Receipt(BaseModel):
    message_id: str
    event_type: str
    payload: Optional[dict[str, Any]] = None


@router.post("/webhooks/receipt")
async def receipt(body: Receipt, session: AsyncSession = Depends(get_session)):
    try:
        message_id = UUID(body.message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id must be a UUID")

    message = await session.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="unknown message_id")

    now = datetime.now(timezone.utc)

    # Idempotency: try to record the event; a duplicate callback hits the unique
    # constraint, in which case we ACK and change nothing else.
    session.add(
        MessageEvent(
            id=uuid4(),
            message_id=message_id,
            event_type=body.event_type,
            payload=body.payload,
            received_at=now,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return {"status": "duplicate_ignored"}

    # --- New event: apply the state machine ---------------------------------
    if body.event_type in LADDER_EVENTS:
        if _rank(body.event_type) > _rank(message.status):
            message.status = body.event_type
    elif body.event_type == "retry":
        # Only meaningful before delivery; never downgrade a delivered+ message.
        if _rank(message.status) < RANK["delivered"]:
            message.status = "retrying"
            message.retry_count = (message.retry_count or 0) + 1
    elif body.event_type == "failed":
        message.status = "failed"

    # Conversion attribution: record an order for the converting customer.
    if body.event_type == "converted":
        amount = (body.payload or {}).get("amount")
        session.add(
            Order(
                id=uuid4(),
                customer_id=message.customer_id,
                amount=amount,
                campaign_id=message.campaign_id,
                created_at=now,
            )
        )

    message.last_event_at = now
    await session.commit()

    # Notify any SSE listeners for this campaign.
    customer = await session.get(Customer, message.customer_id)
    events.publish(
        str(message.campaign_id),
        {
            "message_id": str(message.id),
            "customer_name": customer.name if customer else None,
            "status": message.status,
            "event_type": body.event_type,
        },
    )

    # Throttled mid-campaign delivery check (only calls the model if underperforming).
    monitor.schedule_check(message.campaign_id)

    return {"status": "ok", "message_status": message.status}
