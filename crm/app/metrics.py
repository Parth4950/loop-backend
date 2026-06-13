"""Shared campaign funnel + rate math.

Used by the SSE tracker, /analyze, and the campaigns list so every surface
reports identical numbers. Counts are CUMULATIVE — "reached at least this
stage" — derived from message status, which is a forward-only ladder
(queued < sent < delivered < read < opened < clicked < converted), so a
message's status IS the furthest stage it reached:

    sent >= delivered >= read >= opened >= clicked >= converted

Every dispatched message counts as SENT (anything not still 'queued', including
ones that later failed or are retrying). 'failed' is tracked separately — a
failed message was sent but never delivered.
"""

from __future__ import annotations

from typing import Any

# Statuses that count as having reached (at least) each ladder stage.
_AT_LEAST: dict[str, tuple[str, ...]] = {
    "delivered": ("delivered", "read", "opened", "clicked", "converted"),
    "read": ("read", "opened", "clicked", "converted"),
    "opened": ("opened", "clicked", "converted"),
    "clicked": ("clicked", "converted"),
    "converted": ("converted",),
}


def safe_rate(numerator: int, denominator: int) -> float:
    """Percentage rounded to 1 dp; 0.0 when the denominator is zero (no NaN)."""
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def campaign_metrics(status_counts: dict[str, int]) -> dict[str, Any]:
    """Cumulative funnel counts + engagement rates from per-status message counts.

    The rates are defined ONCE here so the live tracker and /analyze can never
    diverge: open_rate = opened/delivered, click_rate = clicked/opened,
    conversion_rate = converted/clicked (all safe against divide-by-zero).
    """
    total = sum(status_counts.values())

    def reached(stage: str) -> int:
        return sum(status_counts.get(s, 0) for s in _AT_LEAST[stage])

    sent = total - status_counts.get("queued", 0)  # every dispatched message
    delivered = reached("delivered")
    read = reached("read")
    opened = reached("opened")
    clicked = reached("clicked")
    converted = reached("converted")
    failed = status_counts.get("failed", 0)

    return {
        "sent": sent,
        "delivered": delivered,
        "read": read,
        "opened": opened,
        "clicked": clicked,
        "converted": converted,
        "failed": failed,
        "open_rate": safe_rate(opened, delivered),
        "click_rate": safe_rate(clicked, opened),
        "conversion_rate": safe_rate(converted, clicked),
    }
