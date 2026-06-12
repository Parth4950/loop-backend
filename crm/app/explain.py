"""Lazy, cached per-customer segment explanation.

Called only when a marketer clicks a customer row. Makes ONE Flash-Lite call to
explain why the customer fits a campaign's segment (recency, spend, order
pattern, churn risk), then caches it so repeat views cost zero model calls.

Cache is in-memory and keyed by (customer_id, campaign_id) — single-instance
assumption (see events.py); use a shared cache/Redis at scale.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from .agent import llm
from .db import AsyncSessionLocal
from .models import Campaign, Customer, Order

logger = logging.getLogger("loop-crm.explain")

router = APIRouter()

# (customer_id, campaign_id) -> explanation string.
# In-memory, single-instance assumption (see events.py); persist/Redis at scale.
_cache: dict[tuple[str, str], str] = {}


def _build_prompt(customer: Customer, campaign: Campaign, stats: dict[str, Any]) -> str:
    days = stats["days_inactive"]
    recency = f"last ordered {days} days ago" if days is not None else "has never ordered"
    criteria = campaign.compiled_sql or "the campaign's target segment"
    return (
        f"Customer: {customer.name} from {customer.city or 'unknown city'}.\n"
        f"Lifetime spend Rs {stats['total_spend']:.0f} across {stats['order_count']} orders "
        f"(avg Rs {stats['avg_order_value']:.0f}); {recency}.\n"
        f"Target segment '{campaign.name}' criteria: {criteria}.\n"
        "In 2-3 sentences, explain to a marketer why this customer fits the segment, "
        "touching on recency, spend, order pattern, and churn risk. Be specific and concise."
    )


def _fallback(customer: Customer, stats: dict[str, Any]) -> str:
    """Templated explanation from the customer's stats — never empty."""
    days = stats["days_inactive"]
    if days is None:
        recency, churn = "has never placed an order", "high churn risk"
    else:
        recency = f"last ordered {days} days ago"
        churn = "high churn risk" if days > 60 else "moderate churn risk" if days > 30 else "low churn risk"
    return (
        f"{customer.name} has spent Rs {stats['total_spend']:.0f} across "
        f"{stats['order_count']} orders (avg Rs {stats['avg_order_value']:.0f}) and {recency}, "
        f"which signals {churn} and a strong fit for this segment."
    )


@router.get("/customers/{customer_id}/explain")
async def explain_customer(
    customer_id: str,
    campaign_id: str = Query(..., description="Campaign whose segment to explain against"),
):
    try:
        cust_uuid = UUID(customer_id)
        camp_uuid = UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="customer_id and campaign_id must be UUIDs")

    cache_key = (str(cust_uuid), str(camp_uuid))
    if cache_key in _cache:  # repeat view -> zero model calls
        return {"explanation": _cache[cache_key]}

    async with AsyncSessionLocal() as session:
        customer = await session.get(Customer, cust_uuid)
        if customer is None:
            raise HTTPException(status_code=404, detail="unknown customer")
        campaign = await session.get(Campaign, camp_uuid)
        if campaign is None:
            raise HTTPException(status_code=404, detail="unknown campaign")

        total_spend, last_order, order_count = (
            await session.execute(
                select(
                    func.coalesce(func.sum(Order.amount), 0),
                    func.max(Order.created_at),
                    func.count(Order.id),
                ).where(Order.customer_id == cust_uuid)
            )
        ).one()

    days_inactive = (
        (datetime.now(timezone.utc) - last_order).days if last_order is not None else None
    )
    total_spend = float(total_spend or 0)
    order_count = int(order_count or 0)
    stats: dict[str, Any] = {
        "total_spend": total_spend,
        "order_count": order_count,
        "days_inactive": days_inactive,
        "avg_order_value": round(total_spend / order_count, 2) if order_count else 0.0,
    }

    try:
        # Keep retries short: there's a solid fallback, so prefer a fast response
        # over a long 429 backoff for a UI drill-down.
        explanation = (
            await llm.generate(
                llm.GEMINI_MODEL_LITE,
                _build_prompt(customer, campaign, stats),
                system_instruction="You are Loop's audience analyst for Brew & Co. Write crisp, marketer-friendly explanations.",
                temperature=0.4,
                max_attempts=3,
            )
        ).strip()
    except Exception as exc:
        logger.warning("explain LLM call failed (%s); using fallback", exc)
        explanation = ""

    if not explanation:  # model failure or blank -> never return empty
        explanation = _fallback(customer, stats)

    _cache[cache_key] = explanation
    return {"explanation": explanation}
