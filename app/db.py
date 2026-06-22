from collections.abc import Generator

from sqlmodel import Session, create_engine

from app.config import settings

# Synchronous engine; write volume is low for this service.
engine = create_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
