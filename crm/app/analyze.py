"""Post-campaign analysis: final funnel stats + ONE Flash insight call."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from google.genai import types
from sqlalchemy import func, select

from .agent import llm
from .db import AsyncSessionLocal
from .models import Campaign, Message, MessageEvent, Order

logger = logging.getLogger("loop-crm.analyze")

router = APIRouter()

_INSIGHT_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "what_worked": types.Schema(type=types.Type.STRING),
        "what_didnt": types.Schema(type=types.Type.STRING),
        "recommendation": types.Schema(type=types.Type.STRING),
    },
    required=["what_worked", "what_didnt", "recommendation"],
)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


async def _insight(campaign: Campaign, stats: dict[str, Any]) -> dict[str, str]:
    rates = stats["rates"]
    prompt = (
        f"Analyze this finished Brew & Co. campaign on {stats['channel']}.\n"
        f"Audience {stats['audience_size']}, sent {stats['sent']}, delivered {stats['delivered']}, "
        f"opened {stats['opened']}, clicked {stats['clicked']}, converted {stats['converted']}.\n"
        f"Rates — delivery {rates['delivery_rate']:.0%}, open {rates['open_rate']:.0%}, "
        f"click {rates['click_rate']:.0%}, conversion {rates['conversion_rate']:.0%}.\n"
        f"Attributed revenue ₹{stats['attributed_revenue']:.0f}.\n"
        "Give a crisp post-mortem as JSON with keys what_worked, what_didnt, recommendation "
        "(one short sentence each)."
    )
    try:
        raw = await llm.generate(
            llm.GEMINI_MODEL,
            prompt,
            system_instruction="You are Loop's campaign analyst for Brew & Co. Be specific and concise.",
            temperature=0.4,
            response_schema=_INSIGHT_SCHEMA,
        )
        data = json.loads(raw)
        return {
            "what_worked": str(data.get("what_worked", "")).strip(),
            "what_didnt": str(data.get("what_didnt", "")).strip(),
            "recommendation": str(data.get("recommendation", "")).strip(),
        }
    except Exception as exc:
        logger.warning("analysis LLM call failed (%s); returning fallback", exc)
        return {
            "what_worked": "Analysis unavailable (model call failed).",
            "what_didnt": "",
            "recommendation": "Retry the analysis shortly.",
        }


@router.post("/campaigns/{campaign_id}/analyze")
async def analyze(campaign_id: str):
    try:
        cid = UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="campaign_id must be a UUID")

    async with AsyncSessionLocal() as session:
        campaign = await session.get(Campaign, cid)
        if campaign is None:
            raise HTTPException(status_code=404, detail="unknown campaign")

        status_rows = (
            await session.execute(
                select(Message.status, func.count())
                .where(Message.campaign_id == cid)
                .group_by(Message.status)
            )
        ).all()
        counts = {status: n for status, n in status_rows}
        total = sum(counts.values())
        sent = total - counts.get("queued", 0)  # everything that left the queue

        event_rows = (
            await session.execute(
                select(MessageEvent.event_type, func.count(func.distinct(MessageEvent.message_id)))
                .join(Message, MessageEvent.message_id == Message.id)
                .where(Message.campaign_id == cid)
                .group_by(MessageEvent.event_type)
            )
        ).all()
        ev = {event_type: n for event_type, n in event_rows}
        delivered = ev.get("delivered", 0)
        opened = ev.get("opened", 0)
        clicked = ev.get("clicked", 0)
        converted = ev.get("converted", 0)

        revenue = await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(Order.campaign_id == cid)
        )

    stats: dict[str, Any] = {
        "campaign_id": str(cid),
        "channel": campaign.channel,
        "audience_size": total,
        "sent": sent,
        "delivered": delivered,
        "opened": opened,
        "clicked": clicked,
        "converted": converted,
        "attributed_revenue": float(revenue or 0),
        "rates": {
            "delivery_rate": _rate(delivered, sent),
            "open_rate": _rate(opened, delivered),
            "click_rate": _rate(clicked, opened),
            "conversion_rate": _rate(converted, clicked),
        },
    }

    insight = await _insight(campaign, stats)
    return {**stats, "insight": insight}
