"""loop-channel FastAPI service — outbound message delivery + callback simulator."""

import asyncio
import logging

from fastapi import FastAPI
from pydantic import BaseModel

from .simulator import simulate_message

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="loop-channel")

# Keep references to in-flight background tasks so they aren't garbage-collected.
_tasks: set[asyncio.Task] = set()


class SendRequest(BaseModel):
    message_id: str
    recipient: str
    channel: str
    body: str


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/send", status_code=202)
async def send(req: SendRequest):
    # Fire-and-forget: schedule the lifecycle simulator and return immediately.
    task = asyncio.create_task(
        simulate_message(req.message_id, req.recipient, req.channel, req.body)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"status": "accepted", "message_id": req.message_id}
