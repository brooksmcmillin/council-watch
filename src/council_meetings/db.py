from pathlib import Path

from sqlalchemy import create_engine, inspect
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
    """Initialize fresh databases; reject existing tables that need migrations."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    missing_columns = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns.extend(
            f"{table.name}.{column.name}"
            for column in table.columns
            if column.name not in existing_columns
        )
    if missing_columns:
        raise RuntimeError(
            "Database schema is out of date; missing columns: "
            + ", ".join(sorted(missing_columns))
            + ". Stop the app and back up the database, then run "
            "`uv run alembic upgrade head` from the project directory against the same "
            "database (check sqlalchemy.url in alembic.ini). Restart the app afterward. "
            "Startup has not migrated the database."
        )
    Base.metadata.create_all(engine)


def get_db():
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
