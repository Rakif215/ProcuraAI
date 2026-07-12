"""
app/db/client.py
----------------
Supabase client (for auth + simple ops) and SQLAlchemy async engine
(for complex queries and portability to self-hosted Postgres).
"""
from functools import lru_cache
from supabase import create_client, Client
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings


# ── Supabase client (auth, storage, realtime) ────────────────────────────────

@lru_cache
def get_supabase() -> Client:
    """Returns a Supabase client using the service role key (backend use only)."""
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


@lru_cache
def get_supabase_anon() -> Client:
    """Returns a Supabase client with anon key (for user-facing auth flows)."""
    return create_client(settings.supabase_url, settings.supabase_anon_key)


supabase: Client = get_supabase()


# ── SQLAlchemy async engine (direct Postgres, no Supabase abstraction) ────────

db_url = settings.database_url
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    db_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=not settings.is_production,   # log SQL in dev, silent in prod
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncSession:
    """FastAPI dependency: yields an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
