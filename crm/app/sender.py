"""Campaign send fan-out: flips queued messages to 'sent' and dispatches them
to the channel service in the background so the endpoint returns fast.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Campaign, Customer, Message, MessageEvent

# Messages that have NOT yet been delivered (delivered/opened/clicked/converted
# all imply delivery and are left alone on a channel switch).
_UNDELIVERED = ("queued", "sent", "retrying", "failed")

logger = logging.getLogger("loop-crm.sender")

CHANNEL_URL = os.environ.get("CHANNEL_URL", "http://localhost:8001").rstrip("/")

router = APIRouter()

# Retain background fan-out tasks so they aren't garbage-collected mid-flight.
_tasks: set[asyncio.Task] = set()


def _recipient(channel: str, customer: Customer) -> str | None:
    """Phone for whatsapp/sms, email for email."""
    if channel in ("whatsapp", "sms"):
        return customer.phone
    return customer.email


async def _deliver(payloads: list[dict[str, Any]]) -> None:
    """POST each prepared message to the channel service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        for payload in payloads:
            try:
                resp = await client.post(f"{CHANNEL_URL}/send", json=payload)
                resp.raise_for_status()
                logger.info("dispatched message %s -> %s", payload["message_id"], resp.status_code)
            except httpx.HTTPError as exc:
                logger.error("failed to dispatch message %s: %s", payload["message_id"], exc)


@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        cid = UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="campaign_id must be a UUID")

    campaign = await session.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="unknown campaign")

    rows = (
        await session.execute(
            select(Message, Customer)
            .join(Customer, Message.customer_id == Customer.id)
            .where(Message.campaign_id == cid, Message.status == "queued")
        )
    ).all()

    payloads: list[dict[str, Any]] = []
    for message, customer in rows:
        message.status = "sent"
        payloads.append(
            {
                "message_id": str(message.id),
                "recipient": _recipient(campaign.channel, customer),
                "channel": campaign.channel,
                "body": campaign.message,
            }
        )

    await session.commit()

    # Fan out in the background; endpoint returns immediately.
    task = asyncio.create_task(_deliver(payloads))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    return {"sent": len(payloads)}


class SwitchChannelRequest(BaseModel):
    new_channel: str


@router.post("/campaigns/{campaign_id}/switch-channel")
async def switch_channel(
    campaign_id: str,
    req: SwitchChannelRequest,
    session: AsyncSession = Depends(get_session),
):
    """Approval #2: move all not-yet-delivered messages to a new channel and re-send."""
    try:
        cid = UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="campaign_id must be a UUID")

    campaign = await session.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="unknown campaign")

    campaign.channel = req.new_channel

    rows = (
        await session.execute(
            select(Message, Customer)
            .join(Customer, Message.customer_id == Customer.id)
            .where(Message.campaign_id == cid, Message.status.in_(_UNDELIVERED))
        )
    ).all()

    payloads: list[dict[str, Any]] = []
    message_ids = []
    for message, customer in rows:
        message.status = "queued"
        message.retry_count = 0
        message.last_event_at = None
        message_ids.append(message.id)
        payloads.append(
            {
                "message_id": str(message.id),
                "recipient": _recipient(req.new_channel, customer),
                "channel": req.new_channel,
                "body": campaign.message,
            }
        )

    # A switch is a fresh delivery attempt: clear the prior event ledger so the
    # new channel's callbacks aren't swallowed by the idempotency constraint.
    if message_ids:
        await session.execute(
            delete(MessageEvent).where(MessageEvent.message_id.in_(message_ids))
        )

    await session.commit()

    task = asyncio.create_task(_deliver(payloads))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    return {"switched": len(payloads)}
