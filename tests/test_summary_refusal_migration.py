from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def test_refusal_migration_preserves_summary_and_delivery_state(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'archive.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "a6b7c8d9e0f1")  # pragma: allowlist secret
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO meetings(id,date,title,civicplus_id,url_date_slug) VALUES(35,'2025-02-04','Fixture','fixture','fixture')"
            )
        )
        connection.execute(
            text("""
            INSERT INTO documents(id,meeting_id,doc_type,source_url,summary,notified_email,
                notified_bluesky,email_delivered_to) VALUES
            (67,35,'agenda','/agenda',NULL,0,0,'[]'),
            (68,35,'minutes','/minutes','Published summary',1,1,'["delivered@example.test"]')
        """)
        )
    command.upgrade(config, "head")
    with engine.begin() as connection:
        before = connection.execute(
            text(
                "SELECT id,summary,notified_email,notified_bluesky,email_delivered_to FROM documents ORDER BY id"
            )
        ).all()
        assert connection.execute(text("SELECT summary_refused_hash FROM documents")).all() == [
            (None,),
            (None,),
        ]
        connection.execute(
            text("UPDATE documents SET summary_refused_hash='pdf:fixture' WHERE id=67")
        )
    command.downgrade(config, "a6b7c8d9e0f1")  # pragma: allowlist secret
    assert "summary_refused_hash" not in {
        c["name"] for c in inspect(engine).get_columns("documents")
    }
    with engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT id,summary,notified_email,notified_bluesky,email_delivered_to FROM documents ORDER BY id"
            )
        ).all()
    assert (
        before
        == after
        == [(67, None, 0, 0, "[]"), (68, "Published summary", 1, 1, '["delivered@example.test"]')]
    )
    engine.dispose()
