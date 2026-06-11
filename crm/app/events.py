"""In-memory pub/sub used to push live campaign updates to SSE subscribers.

NOTE: this is per-process state. It assumes the CRM runs as a SINGLE instance.
To scale horizontally, replace this with Redis pub/sub (or similar) so updates
published on one instance reach SSE clients connected to another.
"""

from __future__ import annotations

import asyncio
from typing import Any

# campaign_id (canonical str) -> set of subscriber queues
_subscribers: dict[str, set[asyncio.Queue]] = {}


def subscribe(campaign_id: str) -> asyncio.Queue:
    """Register a new subscriber queue for a campaign and return it."""
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(campaign_id, set()).add(queue)
    return queue


def unsubscribe(campaign_id: str, queue: asyncio.Queue) -> None:
    """Remove a subscriber queue; drop the campaign entry once empty."""
    subs = _subscribers.get(campaign_id)
    if not subs:
        return
    subs.discard(queue)
    if not subs:
        _subscribers.pop(campaign_id, None)


def publish(campaign_id: str, data: dict[str, Any]) -> None:
    """Fan a payload out to every subscriber currently listening on a campaign."""
    for queue in list(_subscribers.get(campaign_id, ())):
        queue.put_nowait(data)
