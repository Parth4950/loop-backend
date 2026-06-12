"""Post-campaign analysis: final funnel stats + ONE Flash insight call.

Returns a STABLE, flat shape with safe (never-NaN) rates and insight fields that
are never empty — even if Gemini rate-limits or fails, the text falls back to
templated sentences derived from the stats.
"""

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
        "next_step": types.Schema(type=types.Type.STRING),
    },
    required=["what_worked", "what_didnt", "next_step"],
)


def _safe_rate(numerator: int, denominator: int) -> float:
    """Percentage rounded to 1 dp; 0.0 when the denominator is zero (no NaN)."""
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def _fallback_text(stats: dict[str, Any]) -> dict[str, str]:
    """Templated, stat-derived sentences — used whenever a model field is blank."""
    delivered = stats["delivered"]
    opened = stats["opened"]
    clicked = stats["clicked"]
    converted = stats["converted"]
    return {
        "what_worked": (
            f"{delivered} of {stats['sent']} messages delivered, "
            f"{clicked} clicked and {converted} converted."
        ),
        "what_didnt": f"{opened - clicked} opened but didn't click.",
        "next_step": (
            f"Re-engage the {clicked - converted} who clicked but didn't convert "
            "with a sharper offer."
        ),
    }


async def _insight(channel: str, stats: dict[str, Any]) -> dict[str, str]:
    """One Gemini call for the three text fields, guaranteed non-empty per field."""
    fallback = _fallback_text(stats)
    prompt = (
        f"Analyze this finished Brew & Co. campaign on {channel}.\n"
        f"Sent {stats['sent']}, delivered {stats['delivered']}, opened {stats['opened']}, "
        f"clicked {stats['clicked']}, converted {stats['converted']}, "
        f"revenue Rs {stats['revenue']}.\n"
        f"Rates — open {stats['open_rate']}%, click {stats['click_rate']}%, "
        f"conversion {stats['conversion_rate']}%.\n"
        "Give a crisp post-mortem as JSON with keys what_worked, what_didnt, next_step "
        "(one short sentence each)."
    )

    raw = ""
    try:
        # Keep retries short: there's a solid fallback, so prefer a fast response
        # over a long 429 backoff that would stall the insight card.
        raw = await llm.generate(
            llm.GEMINI_MODEL,
            prompt,
            system_instruction="You are Loop's campaign analyst for Brew & Co. Be specific and concise.",
            temperature=0.4,
            response_schema=_INSIGHT_SCHEMA,
            max_attempts=3,
        )
        data = json.loads(raw)
    except Exception as exc:
        # Rate limit, network, or unparseable output — log raw and fall back entirely.
        logger.warning("analysis insight failed (%s); raw response=%r", exc, raw)
        return fallback

    # Per-field guard: any missing/blank field is filled from the template.
    result = {}
    for key in ("what_worked", "what_didnt", "next_step"):
        value = str(data.get(key, "")).strip()
        result[key] = value or fallback[key]
    return result


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
        read = ev.get("read", 0)
        opened = ev.get("opened", 0)
        clicked = ev.get("clicked", 0)
        converted = ev.get("converted", 0)

        revenue = await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(Order.campaign_id == cid)
        )

    stats: dict[str, Any] = {
        "sent": sent,
        "delivered": delivered,
        "read": read,
        "opened": opened,
        "clicked": clicked,
        "converted": converted,
        "revenue": int(round(float(revenue or 0))),
        "open_rate": _safe_rate(opened, delivered),
        "click_rate": _safe_rate(clicked, opened),
        "conversion_rate": _safe_rate(converted, clicked),
    }

    insight = await _insight(campaign.channel, stats)
    return {**stats, **insight}
