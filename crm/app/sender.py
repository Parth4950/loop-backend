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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Campaign, Customer, Message

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
