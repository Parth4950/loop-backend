"""Throttled mid-campaign delivery monitor.

Hooked into the receipt webhook. As delivered/failed receipts accumulate, it
checks progress at the 25% / 50% / 75% milestones (each at most once, max 3
model calls per campaign). It only spends a Flash-Lite call when delivery is
running WELL below the channel's expected rate and messages are still pending —
otherwise it stays silent. The recommendation is persisted and pushed to the
live SSE banner.

In-memory throttle state assumes a single instance (see events.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from google.genai import types
from sqlalchemy import func, select

from . import events
from .agent import llm
from .agent.tools import CHANNEL_RATES
from .db import AsyncSessionLocal
from .models import Campaign, Message

logger = logging.getLogger("loop-crm.monitor")

MILESTONES = (25, 50, 75)
MAX_CALLS = 3
UNDERPERFORM_FACTOR = 0.60  # actual delivery < 60% of expected => underperforming

# Per-campaign throttle state.
_fired: dict[str, set[int]] = {}
_call_counts: dict[str, int] = {}
_locks: dict[str, asyncio.Lock] = {}
_tasks: set[asyncio.Task] = set()

_RECO_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "text": types.Schema(type=types.Type.STRING),
        "suggested_channel": types.Schema(type=types.Type.STRING),
    },
    required=["text", "suggested_channel"],
)


def schedule_check(campaign_id: UUID) -> None:
    """Fire-and-forget the mid-campaign check so the webhook returns fast."""
    task = asyncio.create_task(maybe_recommend(campaign_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _best_alternative(current: str) -> str:
    alts = {c: rates[0] for c, rates in CHANNEL_RATES.items() if c != current}
    return max(alts, key=alts.get) if alts else current


async def _ask_switch(
    campaign: Campaign,
    milestone: int,
    total: int,
    delivered: int,
    failed: int,
    pending: int,
    actual: float,
    expected: float,
) -> tuple[str, str]:
    """ONE Flash-Lite call for a short switch recommendation (with a safe fallback)."""
    fallback_channel = _best_alternative(campaign.channel)
    fallback_text = (
        f"Delivery on {campaign.channel} is only {actual:.0%} vs an expected "
        f"{expected:.0%} at the {milestone}% mark. Consider switching the "
        f"{pending} pending messages to {fallback_channel}."
    )

    alt_rates = ", ".join(
        f"{c} {rates[0]:.0%}" for c, rates in CHANNEL_RATES.items() if c != campaign.channel
    )
    prompt = (
        f"Brew & Co. campaign '{campaign.name}' is sending on {campaign.channel}.\n"
        f"At the {milestone}% checkpoint: {delivered} delivered, {failed} failed, "
        f"{pending} pending of {total}.\n"
        f"Actual delivery rate {actual:.0%} vs expected {expected:.0%} for this channel.\n"
        f"Alternative channels' expected delivery: {alt_rates}.\n"
        "Delivery is underperforming. In <=2 sentences for a live dashboard banner, "
        "recommend whether to switch channel and to which one, and why. "
        'Respond as JSON: {"text": "...", "suggested_channel": "whatsapp|sms|email"}.'
    )

    try:
        raw = await llm.generate(
            llm.GEMINI_MODEL_LITE,
            prompt,
            system_instruction=(
                "You are Loop's live campaign monitor for Brew & Co. Be terse and decisive."
            ),
            temperature=0.3,
            response_schema=_RECO_SCHEMA,
        )
        data = json.loads(raw)
        text = (str(data.get("text") or "")).strip() or fallback_text
        suggested = str(data.get("suggested_channel") or fallback_channel)
        if suggested not in CHANNEL_RATES:
            suggested = fallback_channel
        return text, suggested
    except Exception as exc:  # model/parse failure -> deterministic fallback
        logger.warning("mid-campaign LLM call failed (%s); using fallback", exc)
        return fallback_text, fallback_channel


async def maybe_recommend(campaign_id: UUID) -> None:
    key = str(campaign_id)
    if _call_counts.get(key, 0) >= MAX_CALLS:
        return

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if _call_counts.get(key, 0) >= MAX_CALLS:
            return

        async with AsyncSessionLocal() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None:
                return

            rows = (
                await session.execute(
                    select(Message.status, func.count())
                    .where(Message.campaign_id == campaign_id)
                    .group_by(Message.status)
                )
            ).all()
            counts = {status: n for status, n in rows}
            total = sum(counts.values())
            if total == 0:
                return

            delivered = sum(
                counts.get(s, 0) for s in ("delivered", "read", "opened", "clicked", "converted")
            )
            failed = counts.get("failed", 0)
            pending = counts.get("queued", 0) + counts.get("sent", 0) + counts.get("retrying", 0)
            processed = delivered + failed
            if processed == 0:
                return

            progress_pct = processed / total * 100
            reached = [m for m in MILESTONES if progress_pct >= m]
            fired = _fired.setdefault(key, set())
            new_milestones = [m for m in reached if m not in fired]
            if not new_milestones:
                return
            # Consume every reached milestone now (each at most once, stays frugal).
            fired.update(reached)
            milestone = max(new_milestones)

            expected = CHANNEL_RATES.get(campaign.channel, CHANNEL_RATES["email"])[0]
            actual = delivered / processed

            # Never call the model if delivery looks fine or nothing is pending.
            if pending <= 0 or actual >= UNDERPERFORM_FACTOR * expected:
                return

            text, suggested = await _ask_switch(
                campaign, milestone, total, delivered, failed, pending, actual, expected
            )
            _call_counts[key] = _call_counts.get(key, 0) + 1
            campaign.mid_campaign_recommendation = text
            await session.commit()

    events.publish(
        key,
        {
            "type": "recommendation",
            "campaign_id": key,
            "text": text,
            "suggested_channel": suggested,
        },
    )
