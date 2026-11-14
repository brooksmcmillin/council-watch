from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from anthropic.types import TextBlock
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session, sessionmaker

from council_meetings import notifier, summarizer
from council_meetings.config import city, settings
from council_meetings.models import Document, Meeting


@pytest.mark.parametrize("doc_type", ["agenda", "minutes"])
def test_html_text_uses_messages_not_pdf_upload(
    monkeypatch: pytest.MonkeyPatch, doc_type: str
) -> None:
    api = MagicMock()
    api.__enter__.return_value = api
    api.messages.create.return_value = SimpleNamespace(
        content=[TextBlock(type="text", text="Fixture summary")]
    )
    monkeypatch.setattr(summarizer.anthropic, "Anthropic", lambda **_: api)
    monkeypatch.setattr(settings, "summarization_model", "fixture-model")
    assert summarizer.summarize_text("Public meeting text", doc_type) == "Fixture summary"
    api.beta.files.upload.assert_not_called()
    call = api.messages.create.call_args.kwargs
    assert call["model"] == "fixture-model"
    blocks = call["messages"][0]["content"]
    assert "Linked attachments and staff reports are not included" in blocks[0]["text"]
    assert city.location in blocks[0]["text"]
    assert blocks[1] == {"type": "text", "text": "Public meeting text"}


def test_text_eligibility_failure_retry_and_pdf_path(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    meeting = Meeting(
        date=date(2026, 11, 2),
        title="Regular",
        civicplus_id="escribe-test",
        url_date_slug="escribe-test",
    )
    db_session.add(meeting)
    db_session.flush()
    for kwargs in [
        {"text_content": "First agenda"},
        {"text_content": "Second minutes"},
        {"pdf_path": "fixture.pdf"},
        {},
        {"text_content": "Already summarized", "summary": "Keep this"},
    ]:
        db_session.add(
            Document(meeting_id=meeting.id, doc_type="agenda", source_url="/source", **kwargs)
        )
    db_session.commit()
    monkeypatch.setattr(summarizer, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(settings, "anthropic_api_key", "fixture-key")
    text = Mock(side_effect=[RuntimeError("temporary failure"), "Text summary"])
    pdf = Mock(return_value="PDF summary")
    monkeypatch.setattr(summarizer, "summarize_text", text)
    monkeypatch.setattr(summarizer, "summarize_pdf", pdf)
    assert summarizer.summarize_unsummarized() == 2
    assert text.call_count == 2
    pdf.assert_called_once_with("fixture.pdf", "agenda")
    assert db_session.query(Document).filter_by(summary="Keep this").count() == 1
    text.side_effect = None
    text.return_value = "Retried summary"
    assert summarizer.summarize_unsummarized() == 1
    assert summarizer.summarize_unsummarized() == 0
    db_session.expire_all()
    assert db_session.query(Document).filter(Document.summarized_at.isnot(None)).count() == 3


@pytest.mark.parametrize("pdf_path", [None, "private-path.pdf"])
def test_failed_document_logs_public_mapping_not_source_contents(
    pdf_path: str | None,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    meeting = Meeting(
        id=35,
        date=date(2025, 2, 4),
        title="Fixture meeting",
        civicplus_id="fixture",
        url_date_slug="fixture",
    )
    document = Document(
        id=67,
        meeting=meeting,
        doc_type="agenda",
        pdf_path=pdf_path,
        source_url="https://example.com/private-source?token=private-token",
        text_content="private-document-text",
    )
    db_session.add(document)
    db_session.commit()
    monkeypatch.setattr(summarizer, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(settings, "anthropic_api_key", "fixture-key")
    pdf = Mock(side_effect=ValueError("Summarization response contained no text"))
    text = Mock(side_effect=ValueError("Summarization response contained no text"))
    monkeypatch.setattr(summarizer, "summarize_pdf", pdf)
    monkeypatch.setattr(summarizer, "summarize_text", text)
    caplog.set_level("INFO", logger=summarizer.__name__)
    assert summarizer.summarize_unsummarized() == 0
    source = "pdf" if pdf_path else "html"
    assert f"id=67, type=agenda) public_meeting=/meeting/35 source={source}" in caplog.text
    assert "private" not in caplog.text
    assert pdf.call_count == int(pdf_path is not None)
    assert text.call_count == int(pdf_path is None)
    db_session.expire_all()
    saved = db_session.get(Document, 67)
    assert saved is not None and saved.summary is None


def test_missing_key_never_calls_html_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    api = Mock()
    monkeypatch.setattr(summarizer.anthropic, "Anthropic", api)
    assert summarizer.summarize_unsummarized() == 0
    api.assert_not_called()


def test_email_source_links_and_limitations_without_sending() -> None:
    meeting = Meeting(id=2, date=date(2026, 11, 2), title="Regular <meeting>")
    source = "https://pub-campbell.escribemeetings.com/Meeting.aspx?Id=test&Agenda=Agenda"
    doc = Document(
        doc_type="agenda",
        source_url=source,
        text_content="Public text",
        summary="Plain <script>summary</script>",
    )
    html = notifier._build_email_html(meeting, doc, None)
    plain = notifier._build_email_text(meeting, doc, None)
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("script") is None
    link = soup.find("a")
    assert link is not None
    assert link["href"] == source
    assert "HTML" in notifier._doc_label(doc)
    assert doc.source_notice in soup.get_text()
    assert doc.source_notice in plain
    assert f"Original document: {source}" in plain
    doc.text_content = None
    doc.source_url = "/AgendaCenter/ViewFile/Agenda/_10072025-3159"
    assert city.resolve_url(doc.source_url) in notifier._build_email_text(meeting, doc, None)
    assert doc.source_notice == ""
