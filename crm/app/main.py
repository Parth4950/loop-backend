"""loop-crm FastAPI service."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import analyze, campaigns, explain, sender, stream, webhooks
from .agent import runner as agent_runner
from .db import get_session
from .models import Campaign, Customer, Message

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="loop-crm")

# Permissive CORS so the frontend can call this service directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(webhooks.router)
app.include_router(sender.router)
app.include_router(stream.router)
app.include_router(analyze.router)
app.include_router(campaigns.router)
app.include_router(explain.router)
app.include_router(agent_runner.router)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/customers/count")
async def customers_count(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(func.count()).select_from(Customer))
    return {"count": result.scalar_one()}


@app.post("/dev/test-campaign")
async def dev_test_campaign(session: AsyncSession = Depends(get_session)):
    """Create a small approved campaign + 15 queued messages to exercise the loop."""
    now = datetime.now(timezone.utc)
    campaign = Campaign(
        id=uuid4(),
        name="Test",
        channel="whatsapp",
        status="approved",
        message="We miss you — here's 20% off your next Brew & Co. order",
        created_at=now,
    )
    session.add(campaign)

    customers = (
        (await session.execute(select(Customer).order_by(func.random()).limit(15))).scalars().all()
    )
    for customer in customers:
        session.add(
            Message(
                id=uuid4(),
                campaign_id=campaign.id,
                customer_id=customer.id,
                status="queued",
                retry_count=0,
                last_event_at=None,
            )
        )

    await session.commit()
    return {"campaign_id": str(campaign.id)}
