import datetime as dt

import pytest
from sqlalchemy.orm import Session

from council_meetings import notifier, subscriptions
from council_meetings.config import settings
from council_meetings.models import Document, Meeting


@pytest.fixture
def meeting_and_doc(db_session: Session) -> tuple[Meeting, Document]:
    meeting = Meeting(
        date=dt.date(2025, 9, 16),
        title="Regular Meeting",
        civicplus_id="3145",
        url_date_slug="_09162025-3145",
    )
    db_session.add(meeting)
    db_session.flush()
    doc = Document(
        meeting_id=meeting.id,
        doc_type="agenda",
        source_url="/AgendaCenter/ViewFile/Agenda/_09162025-3145",
        summary="A summary of the meeting.",
    )
    db_session.add(doc)
    db_session.flush()
    return meeting, doc


class FakeSMTP:
    """Records sendmail calls; usable as a context manager like smtplib.SMTP."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sent: list[tuple[str, list[str], str]] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: str) -> None:
        self.sent.append((from_addr, to_addrs, msg))


@pytest.fixture
def _email_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "email_from", "bot@example.com")
    monkeypatch.setattr(settings, "email_to", "")
    monkeypatch.setattr(settings, "app_base_url", "https://site.example")


@pytest.fixture
def _fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_recipients_combine_subscribers_and_admin(
    db_session: Session, _email_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscriber, _ = subscriptions.subscribe(db_session, "sub@example.com")
    subscriptions.confirm(db_session, subscriber.confirmation_token)
    monkeypatch.setattr(settings, "email_to", "admin@example.com, sub@example.com")

    recipients = notifier._email_recipients(db_session)
    by_addr = dict(recipients)
    # subscriber keeps its token; admin has None; the admin dup of the
    # subscriber is dropped (no duplicate send)
    assert by_addr["sub@example.com"] is not None
    assert by_addr["admin@example.com"] is None
    assert len(recipients) == 2


def test_send_email_personalizes_unsubscribe(
    db_session: Session,
    meeting_and_doc: tuple[Meeting, Document],
    _email_config: None,
    _fake_smtp: type[FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "email_to", "admin@example.com")
    sub, _ = subscriptions.subscribe(db_session, "sub@example.com")
    subscriptions.confirm(db_session, sub.confirmation_token)
    meeting, doc = meeting_and_doc

    assert notifier.send_email(meeting, doc, db_session) is True

    smtp = FakeSMTP.instances[-1]
    assert len(smtp.sent) == 2
    by_to = {to[0]: body for _, to, body in smtp.sent}

    # Subscriber message carries the one-click unsubscribe header + link
    sub_msg = by_to["sub@example.com"]
    assert f"/unsubscribe/{sub.unsubscribe_token}" in sub_msg
    assert "List-Unsubscribe:" in sub_msg
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in sub_msg

    # Admin message has no unsubscribe machinery
    admin_msg = by_to["admin@example.com"]
    assert "unsubscribe" not in admin_msg.lower()


def test_send_confirmation_email(
    _email_config: None,
    _fake_smtp: type[FakeSMTP],
) -> None:
    assert notifier.send_confirmation_email("new@example.com", "confirmation-token") is True

    smtp = FakeSMTP.instances[-1]
    assert len(smtp.sent) == 1
    _, recipients, body = smtp.sent[0]
    assert recipients == ["new@example.com"]
    assert "https://site.example/confirm/confirmation-token" in body


def test_send_email_no_recipients_is_satisfied(
    db_session: Session,
    meeting_and_doc: tuple[Meeting, Document],
    _email_config: None,
    _fake_smtp: type[FakeSMTP],
) -> None:
    meeting, doc = meeting_and_doc
    # No subscribers, no static list -> nothing to send, but channel satisfied
    assert notifier.send_email(meeting, doc, db_session) is True
    assert FakeSMTP.instances == []


def test_send_email_smtp_failure_returns_false(
    db_session: Session,
    meeting_and_doc: tuple[Meeting, Document],
    _email_config: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscriber, _ = subscriptions.subscribe(db_session, "sub@example.com")
    subscriptions.confirm(db_session, subscriber.confirmation_token)

    def _boom(host: str, port: int) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(notifier.smtplib, "SMTP", _boom)
    meeting, doc = meeting_and_doc
    assert notifier.send_email(meeting, doc, db_session) is False
