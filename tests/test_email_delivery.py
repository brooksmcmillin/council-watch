"""Notification cycles use only fake SMTP and an isolated on-disk SQLite DB."""

import datetime as dt
import smtplib
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from council_meetings import notifier, scraper, subscriptions
from council_meetings.config import settings
from council_meetings.models import Base, Document, Meeting


class DeliverySMTP:
    attempts: list[str] = []
    failing: set[str] = set()
    refuse: bool = False
    fail_exit: bool = False

    def __init__(self, host: str, port: int) -> None:
        pass

    def __enter__(self) -> "DeliverySMTP":
        return self

    def __exit__(self, *args: object) -> None:
        if self.fail_exit:
            raise OSError("connection lost on quit")

    def starttls(self) -> None:
        pass

    def sendmail(
        self, sender: str, recipients: list[str], message: str
    ) -> dict[str, tuple[int, bytes]]:
        address = recipients[0]
        self.attempts.append(address)
        if address in self.failing:
            if self.refuse:
                return {address: (450, b"try again")}
            raise smtplib.SMTPRecipientsRefused({address: (450, b"try again")})
        return {}


@pytest.fixture
def delivery_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(notifier, "SessionLocal", factory)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "email_from", "bot@example.invalid")
    monkeypatch.setattr(settings, "email_to", "admin@example.invalid")
    monkeypatch.setattr(settings, "bluesky_handle", "")
    monkeypatch.setattr(notifier.smtplib, "SMTP", DeliverySMTP)
    DeliverySMTP.attempts = []
    DeliverySMTP.failing = set()
    DeliverySMTP.refuse = False
    DeliverySMTP.fail_exit = False
    with factory() as db:
        meeting = Meeting(
            date=dt.date(2026, 11, 3), title="Meeting", civicplus_id="1", url_date_slug="1"
        )
        db.add(meeting)
        db.flush()
        db.add(Document(meeting_id=meeting.id, doc_type="agenda", source_url="/a", summary="S"))
        sub, _ = subscriptions.subscribe(db, "sub@example.invalid")
        subscriptions.confirm(db, sub.confirmation_token)
        db.commit()
    yield factory
    engine.dispose()


def state(factory: sessionmaker[Session]) -> tuple[bool, list[str]]:
    with factory() as db:
        doc = db.query(Document).one()
        return doc.notified_email, doc.email_delivered_to


@pytest.mark.parametrize("refuse", [False, True])
def test_partial_delivery_retries_only_failed_recipient_across_sessions(
    delivery_db: sessionmaker[Session], refuse: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    DeliverySMTP.failing = {"sub@example.invalid"}
    DeliverySMTP.refuse = refuse
    assert notifier.notify_new_summaries() == 1
    assert state(delivery_db) == (False, ["admin@example.invalid"])
    assert DeliverySMTP.attempts == ["sub@example.invalid", "admin@example.invalid"]

    # Same mailbox with different casing must not duplicate a successful send.
    monkeypatch.setattr(settings, "email_to", "ADMIN@example.invalid")
    DeliverySMTP.failing = set()
    DeliverySMTP.attempts = []
    assert notifier.notify_new_summaries() == 1
    assert DeliverySMTP.attempts == ["sub@example.invalid"]
    assert state(delivery_db) == (True, ["admin@example.invalid", "sub@example.invalid"])
    assert notifier.notify_new_summaries() == 0


def test_all_failure_remains_retryable(delivery_db: sessionmaker[Session]) -> None:
    DeliverySMTP.failing = {"sub@example.invalid", "admin@example.invalid"}
    assert notifier.notify_new_summaries() == 1
    assert state(delivery_db) == (False, [])
    DeliverySMTP.failing = set()
    DeliverySMTP.attempts = []
    notifier.notify_new_summaries()
    assert set(DeliverySMTP.attempts) == {"sub@example.invalid", "admin@example.invalid"}
    assert state(delivery_db)[0] is True


def test_quit_failure_retains_accepted_recipients(delivery_db: sessionmaker[Session]) -> None:
    DeliverySMTP.fail_exit = True
    notifier.notify_new_summaries()
    assert state(delivery_db) == (False, ["admin@example.invalid", "sub@example.invalid"])
    DeliverySMTP.fail_exit = False
    DeliverySMTP.attempts = []
    notifier.notify_new_summaries()
    assert DeliverySMTP.attempts == []
    assert state(delivery_db)[0] is True


@pytest.mark.parametrize("mode", ["no-recipients", "disabled"])
def test_empty_or_disabled_channel_is_satisfied_without_smtp(
    delivery_db: sessionmaker[Session], mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if mode == "disabled":
        monkeypatch.setattr(settings, "smtp_host", "")
    else:
        monkeypatch.setattr(settings, "email_to", "")
        with delivery_db() as db:
            sub = subscriptions.active_subscribers(db)[0]
            subscriptions.unsubscribe(db, sub.unsubscribe_token)
            db.commit()
    notifier.notify_new_summaries()
    assert state(delivery_db) == (True, [])
    assert DeliverySMTP.attempts == []
    assert notifier.notify_new_summaries() == 0


def test_unsubscribed_failed_recipient_is_not_retried(delivery_db: sessionmaker[Session]) -> None:
    DeliverySMTP.failing = {"sub@example.invalid"}
    notifier.notify_new_summaries()
    with delivery_db() as db:
        sub = subscriptions.active_subscribers(db)[0]
        subscriptions.unsubscribe(db, sub.unsubscribe_token)
        db.commit()
    DeliverySMTP.attempts = []
    notifier.notify_new_summaries()
    assert DeliverySMTP.attempts == []
    assert state(delivery_db) == (True, ["admin@example.invalid"])


def test_revised_summary_can_notify_previously_successful_recipients(
    delivery_db: sessionmaker[Session],
) -> None:
    notifier.notify_new_summaries()
    assert state(delivery_db)[0] is True
    with delivery_db() as db:
        doc = db.query(Document).one()
        scraper._clear_summary(doc)
        assert doc.email_delivered_to == []
        doc.summary = "Revised summary"
        db.commit()
    DeliverySMTP.attempts = []
    notifier.notify_new_summaries()
    assert set(DeliverySMTP.attempts) == {"admin@example.invalid", "sub@example.invalid"}
    assert state(delivery_db)[0] is True
