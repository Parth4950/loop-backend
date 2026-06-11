"""loop-crm FastAPI service."""

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Customer

app = FastAPI(title="loop-crm")

# Permissive CORS so the frontend can call this service directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/customers/count")
async def customers_count(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(func.count()).select_from(Customer))
    return {"count": result.scalar_one()}
