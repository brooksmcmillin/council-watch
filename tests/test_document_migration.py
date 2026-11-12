import logging
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def test_text_migration_preserves_existing_archive(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "e4f5a6b7c8d9")  # pragma: allowlist secret
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO meetings (id,date,title,civicplus_id,url_date_slug) VALUES (2,'2025-10-07','Legacy regular','3159','_10072025-3159')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO documents (meeting_id,doc_type,source_url,summary,notified_email,notified_bluesky) VALUES (2,'agenda','/legacy','Existing summary',1,1)"
            )
        )
    command.upgrade(config, "head")
    assert "text_content" in {column["name"] for column in inspect(engine).get_columns("documents")}
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT summary,notified_email,text_content FROM documents")
        ).one()
        assert row == ("Existing summary", 1, None)
    command.downgrade(config, "e4f5a6b7c8d9")  # pragma: allowlist secret
    assert "text_content" not in {
        column["name"] for column in inspect(engine).get_columns("documents")
    }
    engine.dispose()


def test_migration_preserves_application_loggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = logging.getLogger("council_meetings.diagnostics")
    monkeypatch.setattr(logger, "disabled", False)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'logging.db'}")
    command.upgrade(config, "head")
    assert not logger.disabled
