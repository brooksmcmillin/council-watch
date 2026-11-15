from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from anthropic.types import Message, TextBlock
from sqlalchemy.orm import Session, sessionmaker

from council_meetings import notifier, summarizer
from council_meetings.config import settings
from council_meetings.models import Document, Meeting


@pytest.fixture
def document(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Document:
    meeting = Meeting(
        date=date(2025, 2, 4), title="Fixture", civicplus_id="fixture", url_date_slug="fixture"
    )
    doc = Document(
        meeting=meeting,
        doc_type="agenda",
        source_url="/source",
        text_content="Original source",
        notified_email=False,
        notified_bluesky=True,
        email_delivered_to=["already@example.test"],
    )
    db_session.add(doc)
    db_session.commit()
    monkeypatch.setattr(summarizer, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(settings, "anthropic_api_key", "offline-fixture")
    return doc


@pytest.mark.parametrize("pdf", [False, True])
def test_refusal_survives_restart_without_blocking_other_documents(
    document: Document,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pdf: bool,
) -> None:
    if pdf:
        source = tmp_path / "source.pdf"
        source.write_bytes(b"%PDF-fixture")
        document.pdf_path = str(source)
        db_session.commit()
    completed = Document(
        meeting_id=document.meeting_id,
        doc_type="minutes",
        source_url="/minutes",
        summary="Keep published summary",
        notified_email=True,
        notified_bluesky=True,
        email_delivered_to=["delivered@example.test"],
    )
    other = Document(
        meeting_id=document.meeting_id,
        doc_type="agenda",
        source_url="/other",
        text_content="Other source",
    )
    db_session.add_all([completed, other])
    db_session.commit()
    refused = Mock(side_effect=summarizer.SummaryRefusedError("refused"))
    text = (
        Mock(return_value="Other summary")
        if pdf
        else Mock(side_effect=[summarizer.SummaryRefusedError("refused"), "Other summary"])
    )
    monkeypatch.setattr(summarizer, "summarize_pdf", refused)
    monkeypatch.setattr(summarizer, "summarize_text", text)
    assert summarizer.summarize_unsummarized() == 1
    db_session.expire_all()
    assert document.summary is None
    assert document.summary_refused_hash
    assert document.notified_email is False
    assert document.notified_bluesky is True
    assert document.email_delivered_to == ["already@example.test"]
    assert completed.summary == "Keep published summary"
    assert completed.notified_email and completed.notified_bluesky
    assert completed.email_delivered_to == ["delivered@example.test"]
    calls = (refused.call_count, text.call_count)
    monkeypatch.setattr(settings, "summarization_model", "another-model")
    assert summarizer.summarize_unsummarized() == 0  # new DB session, same persisted marker
    assert (refused.call_count, text.call_count) == calls


@pytest.mark.parametrize(
    "error", [ValueError("empty non-refusal"), RuntimeError("temporary failure")]
)
def test_nonrefusal_failures_remain_retryable(
    document: Document, db_session: Session, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    text = Mock(side_effect=[error, "Recovered summary"])
    monkeypatch.setattr(summarizer, "summarize_text", text)
    assert summarizer.summarize_unsummarized() == 0
    db_session.expire_all()
    assert document.summary_refused_hash is None
    assert summarizer.summarize_unsummarized() == 1
    assert text.call_count == 2


@pytest.mark.parametrize("pdf", [False, True])
def test_operator_seed_and_changed_source(
    document: Document,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pdf: bool,
) -> None:
    source = tmp_path / "source.pdf"
    if pdf:
        source.write_bytes(b"%PDF-original")
        document.pdf_path = str(source)
        db_session.commit()
    provider = Mock(return_value="Changed source summary")
    monkeypatch.setattr(summarizer, "summarize_pdf", provider)
    monkeypatch.setattr(summarizer, "summarize_text", provider)
    monkeypatch.setattr("sys.argv", ["summarizer", "--quarantine-refusal", str(document.id)])
    summarizer.main()
    provider.assert_not_called()
    assert summarizer.summarize_unsummarized() == 0
    provider.assert_not_called()
    if pdf:
        # HTML changes must not bypass the quarantine while PDF remains selected.
        document.text_content = "Different fallback text"
        db_session.commit()
        assert summarizer.summarize_unsummarized() == 0
        source.write_bytes(b"%PDF-changed")
    else:
        document.text_content = "Changed source"
        db_session.commit()
    assert summarizer.summarize_unsummarized() == 1
    provider.assert_called_once()
    db_session.expire_all()
    assert document.summary == "Changed source summary"
    assert document.summary_refused_hash is None
    assert document.notified_email is False
    assert document.notified_bluesky is True
    assert document.email_delivered_to == ["already@example.test"]


@pytest.mark.parametrize("state", ["missing", "summarized", "no_source"])
def test_seed_rejects_ineligible_documents(
    document: Document, db_session: Session, state: str
) -> None:
    if state == "summarized":
        document.summary = "Published"
    elif state == "no_source":
        document.text_content = None
    db_session.commit()
    with pytest.raises(ValueError):
        summarizer.quarantine_refused_document(-1 if state == "missing" else document.id)
    db_session.expire_all()
    assert document.summary_refused_hash is None
    assert document.email_delivered_to == ["already@example.test"]


def test_quarantine_is_not_a_notification_candidate(
    document: Document, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    summarizer.quarantine_refused_document(document.id)
    monkeypatch.setattr(notifier, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    email = Mock()
    bluesky = Mock()
    monkeypatch.setattr(notifier, "send_email", email)
    monkeypatch.setattr(notifier, "post_bluesky", bluesky)
    assert notifier.notify_new_summaries() == 0
    email.assert_not_called()
    bluesky.assert_not_called()
    db_session.expire_all()
    assert document.summary is None
    assert document.notified_email is False
    assert document.notified_bluesky is True
    assert document.email_delivered_to == ["already@example.test"]


def test_refusal_with_text_is_not_a_publishable_summary(caplog: pytest.LogCaptureFixture) -> None:
    message = SimpleNamespace(
        stop_reason="refusal", content=[TextBlock(type="text", text="private-refusal-body")]
    )
    with pytest.raises(summarizer.SummaryRefusedError):
        summarizer._response_text(cast(Message, message))
    assert "private-refusal-body" not in caplog.text
