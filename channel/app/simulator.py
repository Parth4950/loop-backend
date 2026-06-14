"""Async callback simulator for the loop-channel service.

When the channel "sends" a message it immediately returns 202, then runs this
fire-and-forget lifecycle in the background: it walks the message through a
per-channel engagement funnel and POSTs each lifecycle event back to the CRM at
``{CRM_URL}/webhooks/receipt``.

Environment:
  CRM_URL                  base URL of the CRM service (default http://localhost:8000)
  DEMO_SPEED               divides every sleep so demos run fast (default 1)
  RIG_LOW_DELIVERY_CHANNEL if a message's channel matches, its delivery
                           probability is forced low (~0.18) so the
                           mid-campaign-switch demo triggers reliably
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Optional

import httpx

logger = logging.getLogger("loop-channel.simulator")

CRM_URL = os.environ.get("CRM_URL", "http://localhost:8000").rstrip("/")
RIG_LOW_DELIVERY_CHANNEL = os.environ.get("RIG_LOW_DELIVERY_CHANNEL")
RIGGED_DELIVERY_PROB = 0.18

# Per-channel funnel as CONDITIONAL probabilities at each stage. Each stage is
# conditioned on the previous one: read|delivered, open|read, click|opened,
# convert|clicked.
FUNNEL: dict[str, dict[str, float]] = {
    "whatsapp": {"deliver": 0.93, "read": 0.78, "open": 0.67, "click": 0.45, "convert": 0.30},
    "sms": {"deliver": 0.87, "read": 0.55, "open": 0.47, "click": 0.34, "convert": 0.30},
    "email": {"deliver": 0.79, "read": 0.50, "open": 0.44, "click": 0.26, "convert": 0.30},
    "rcs": {"deliver": 0.90, "read": 0.80, "open": 0.85, "click": 0.42, "convert": 0.30},
}
_DEFAULT_FUNNEL = FUNNEL["email"]


def _demo_speed() -> float:
    """Read DEMO_SPEED at call time; fall back to 1 for bad/zero values."""
    try:
        speed = float(os.environ.get("DEMO_SPEED", "1"))
    except ValueError:
        speed = 1.0
    return speed if speed > 0 else 1.0


async def _sleep(lo: float, hi: float) -> None:
    """Sleep a random duration in [lo, hi] seconds, scaled by DEMO_SPEED."""
    await asyncio.sleep(random.uniform(lo, hi) / _demo_speed())


async def _post_callback(
    client: httpx.AsyncClient,
    message_id: str,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """POST one lifecycle event to the CRM, retrying a few times on transient failure."""
    body: dict[str, Any] = {"message_id": message_id, "event_type": event_type}
    if payload is not None:
        body["payload"] = payload

    url = f"{CRM_URL}/webhooks/receipt"
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.post(url, json=body, timeout=10.0)
            resp.raise_for_status()
            logger.info("callback %s -> %s (msg=%s)", event_type, resp.status_code, message_id)
            return
        except (httpx.HTTPError, httpx.TransportError) as exc:
            if attempt == attempts:
                logger.error(
                    "callback %s FAILED after %d attempts (msg=%s): %s",
                    event_type,
                    attempts,
                    message_id,
                    exc,
                )
                return
            backoff = 0.5 * attempt
            logger.warning(
                "callback %s attempt %d/%d failed (msg=%s): %s — retrying in %.1fs",
                event_type,
                attempt,
                attempts,
                message_id,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)


async def simulate_message(
    message_id: str,
    recipient: str,
    channel: str,
    body: str,
) -> None:
    """Run the full delivery/engagement funnel for one message.

    Every dispatched message ends in a terminal receipt — `delivered` or
    `failed` — so the CRM never leaves it stuck in 'sent'. On a rigged channel
    only ~18% deliver, so the other ~82% each fire a `failed` receipt; that is
    what drives the CRM's processed count (delivered + failed) up to the
    25/50/75% milestones.
    """
    funnel = FUNNEL.get(channel, _DEFAULT_FUNNEL)

    deliver_prob = funnel["deliver"]
    if RIG_LOW_DELIVERY_CHANNEL and channel == RIG_LOW_DELIVERY_CHANNEL:
        deliver_prob = RIGGED_DELIVERY_PROB
        logger.info("RIGGED low delivery for channel=%s (msg=%s)", channel, message_id)

    async with httpx.AsyncClient() as client:
        delivered = False
        try:
            # 1. initial send latency
            await _sleep(2, 8)

            # 2. 10% first-attempt failure -> a retry, then proceed
            if random.random() < 0.10:
                await _post_callback(client, message_id, "retry")
                await _sleep(5, 5)

            # 3. delivery gate — a miss ALWAYS fires `failed` (after the send
            #    delay), so nothing is left undelivered. Rigged channels miss ~82%.
            if random.random() >= deliver_prob:
                await _post_callback(client, message_id, "failed")
                return
            await _post_callback(client, message_id, "delivered")
            delivered = True

            # 4. read (between delivered and opened)
            await _sleep(2, 6)
            if random.random() >= funnel["read"]:
                return
            await _post_callback(client, message_id, "read")

            # 5. open
            await _sleep(3, 10)
            if random.random() >= funnel["open"]:
                return
            await _post_callback(client, message_id, "opened")

            # 6. click
            await _sleep(2, 6)
            if random.random() >= funnel["click"]:
                return
            await _post_callback(client, message_id, "clicked")

            # 7. convert
            await _sleep(2, 5)
            if random.random() >= funnel["convert"]:
                return
            amount = random.randint(150, 1200)
            await _post_callback(client, message_id, "converted", payload={"amount": amount})
        except Exception as exc:  # safety net: never leave a message stuck in 'sent'
            logger.exception("lifecycle error (msg=%s): %s", message_id, exc)
            if not delivered:
                await _post_callback(client, message_id, "failed")
