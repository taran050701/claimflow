"""Database engine and session wiring.

- The *engine* owns a pool of connections; create it once per process.
- A *session* is a unit of work: it tracks the objects you load/change and
  flushes them inside one transaction. We create one session per HTTP request.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from claimflow.config import get_settings

# pool_pre_ping checks a connection is alive before using it. Serverless Postgres
# (Neon) closes idle connections, so without this the first request after a quiet
# period would fail.
# Small pool: one free-tier instance, and Neon's free tier limits connections.
engine = create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with SessionLocal() as session:
        yield session
