"""Async engine/session setup.

Neon's pooled connection string goes through PgBouncer in transaction mode,
which breaks asyncpg's server-side prepared statements. Hence:
  - statement_cache_size=0 (disable asyncpg statement cache)
  - unique prepared-statement names (avoid DuplicatePreparedStatementError)
  - NullPool (PgBouncer is the pool)
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        "timeout": 10,
        "command_timeout": 10,
    },
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
