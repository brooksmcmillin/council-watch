from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def test_email_delivery_migration_preserves_archive_and_subscribers(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "f5a6b7c8d9e0")  # pragma: allowlist secret
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO meetings (id,date,title,civicplus_id,url_date_slug) "
                "VALUES (1,'2026-11-03','Meeting','1','1')"
            )
        )
        for notified in (0, 1):
            conn.execute(
                text(
                    "INSERT INTO documents (meeting_id,doc_type,source_url,summary,"
                    "notified_email,notified_bluesky) VALUES (1,'agenda','/a','Summary',:n,:n)"
                ),
                {"n": notified},
            )
        conn.execute(
            text(
                "INSERT INTO subscribers (email,unsubscribe_token,confirmation_token,"
                "confirmed,active) VALUES ('sub@example.invalid','unsubscribe','confirm',1,1)"
            )
        )
        subscribers_before = conn.execute(text("SELECT * FROM subscribers")).all()
        original_documents = conn.execute(text("SELECT * FROM documents"))
        original_columns = tuple(original_documents.keys())
        documents_before = original_documents.all()
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT * FROM subscribers")).all() == subscribers_before
        rows = conn.execute(text("SELECT * FROM documents")).mappings().all()
        assert [tuple(row[name] for name in original_columns) for row in rows] == [
            tuple(row) for row in documents_before
        ]
        assert [row["email_delivered_to"] for row in rows] == ["[]", "[]"]
    command.downgrade(config, "f5a6b7c8d9e0")  # pragma: allowlist secret
    assert "email_delivered_to" not in {
        col["name"] for col in inspect(engine).get_columns("documents")
    }
    with engine.connect() as conn:
        assert conn.execute(text("SELECT * FROM documents")).all() == documents_before
        assert conn.execute(text("SELECT * FROM subscribers")).all() == subscribers_before
    engine.dispose()
