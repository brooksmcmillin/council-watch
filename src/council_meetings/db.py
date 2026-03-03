from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from council_meetings.config import settings
from council_meetings.models import Base

# Ensure the directory for SQLite exists
db_path = settings.database_url.replace("sqlite:///", "")
Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    """Create all tables (used for initial setup / tests)."""
    Base.metadata.create_all(engine)


def get_db():
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
