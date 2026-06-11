"""Async database layer for the loop-crm service.

Reads DATABASE_URL from .env (a psycopg-style ``postgresql://`` URL) and builds
an async SQLAlchemy engine on top of asyncpg. The schema already exists; this
module never creates or drops tables.
"""

import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

_url = os.environ.get("DATABASE_URL")
if not _url:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to your .env file, e.g. "
        "postgresql://user:pass@host:5432/dbname"
    )

# The URL ships with the psycopg scheme; the async engine needs asyncpg.
ASYNC_DATABASE_URL = _url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(ASYNC_DATABASE_URL, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_session():
    """FastAPI dependency that yields an async session and closes it after use."""
    async with AsyncSessionLocal() as session:
        yield session
