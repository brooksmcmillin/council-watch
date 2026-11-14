from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from alembic import command
from council_meetings import db, main, scheduler
from council_meetings.models import Base


@pytest.mark.parametrize(
    ("revision", "missing"),
    [
        ("d3e4f5a6b7c8", "documents.text_content"),  # pragma: allowlist secret
        ("e4f5a6b7c8d9", "documents.text_content"),  # pragma: allowlist secret
        ("f5a6b7c8d9e0", "documents.email_delivered_to"),  # pragma: allowlist secret
    ],
)
def test_stale_schema_stops_before_scheduler_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str, missing: str
) -> None:
    url = f"sqlite:///{tmp_path / 'stale.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, revision)
    engine = create_engine(url)
    monkeypatch.setattr(db, "engine", engine)
    start = Mock()
    monkeypatch.setattr(scheduler, "start_scheduler", start)
    with engine.connect() as connection:
        before = list(connection.execute(text("SELECT sql FROM sqlite_master ORDER BY name")))
    try:
        with pytest.raises(RuntimeError) as error, TestClient(main.app):
            pass
        message = str(error.value)
        assert missing in message
        assert "back up" in message
        assert "uv run alembic upgrade head" in message
        assert "alembic.ini" in message
        start.assert_not_called()
        with engine.connect() as connection:
            assert (
                list(connection.execute(text("SELECT sql FROM sqlite_master ORDER BY name")))
                == before
            )
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == revision
            )
        command.upgrade(config, "head")
        db.init_db()
    finally:
        engine.dispose()


@pytest.mark.parametrize("migrate_first", [False, True])
def test_fresh_and_migrate_first_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrate_first: bool
) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    if migrate_first:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
    engine = create_engine(url)
    monkeypatch.setattr(db, "engine", engine)
    start = Mock()
    monkeypatch.setattr(scheduler, "start_scheduler", start)
    try:
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
            start.assert_called_once_with()
            inspector = inspect(engine)
            for table in Base.metadata.sorted_tables:
                assert {column.name for column in table.columns} <= {
                    column["name"] for column in inspector.get_columns(table.name)
                }
        start.return_value.shutdown.assert_called_once_with(wait=False)
    finally:
        engine.dispose()
