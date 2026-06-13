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
from .metrics import campaign_metrics
from .models import Campaign, Message, Order

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
            system_instruction=(
                "You are Loop's campaign analyst for Brew & Co. Be specific and concise."
            ),
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
        status_counts = {status: n for status, n in status_rows}

        revenue = await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(Order.campaign_id == cid)
        )

    # Same cumulative funnel + rates the live tracker uses, so they can't diverge.
    stats: dict[str, Any] = {
        **campaign_metrics(status_counts),
        "revenue": int(round(float(revenue or 0))),
    }

    insight = await _insight(campaign.channel, stats)
    return {**stats, **insight}
