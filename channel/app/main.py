"""loop-channel FastAPI service — outbound message delivery (stub)."""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="loop-channel")


class SendRequest(BaseModel):
    message_id: str
    recipient: str
    channel: str
    body: str


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/send")
async def send(req: SendRequest):
    # Accept the message and return immediately; delivery happens out-of-band.
    # TODO: async callback simulator (sent/delivered/failed webhooks) added later.
    return {"status": "accepted", "message_id": req.message_id}
