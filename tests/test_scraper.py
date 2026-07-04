import hashlib
from datetime import date

import pytest
from sqlalchemy.orm import Session

from council_meetings import scraper
from council_meetings.config import settings
from council_meetings.models import Document, Meeting
from tests.conftest import FakeClient


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scraper, "DOWNLOAD_DELAY", 0)


@pytest.fixture
def meeting(db_session: Session) -> Meeting:
    m = Meeting(
        date=date(2025, 9, 16),
        title="Regular Meeting",
        civicplus_id="3145",
        url_date_slug="_09162025-3145",
        agenda_url="/AgendaCenter/ViewFile/Agenda/_09162025-3145",
    )
    db_session.add(m)
    db_session.flush()
    return m


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _existing_doc(db: Session, meeting: Meeting, content: bytes, size: int | None) -> Document:
    doc = Document(
        meeting_id=meeting.id,
        doc_type="agenda",
        source_url=meeting.agenda_url,
        pdf_path="data/pdfs/existing.pdf",
        pdf_hash=_sha(content),
        pdf_size=size,
        summary="old summary",
        summary_model="claude-x",
        notified_email=True,
        notified_bluesky=True,
    )
    db.add(doc)
    db.flush()
    return doc


def test_creates_new_document_and_records_size(
    db_session: Session, meeting: Meeting, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "pdf_storage_dir", str(tmp_path))
    content = b"agenda-v1"
    client = FakeClient(get_content=content)

    status = scraper.ensure_document(db_session, client, meeting, "agenda", meeting.agenda_url)

    assert status == "created"
    assert client.head_calls == 0  # no pre-check for brand-new docs
    assert client.get_calls == 1
    doc = db_session.query(Document).filter_by(meeting_id=meeting.id).one()
    assert doc.pdf_hash == _sha(content)
    assert doc.pdf_size == len(content)


def test_head_precheck_skips_unchanged_without_download(
    db_session: Session, meeting: Meeting
) -> None:
    content = b"agenda-v1"
    doc = _existing_doc(db_session, meeting, content, size=len(content))
    client = FakeClient(head_length=len(content))

    status = scraper.ensure_document(db_session, client, meeting, "agenda", meeting.agenda_url)

    assert status == "unchanged"
    assert client.head_calls == 1
    assert client.get_calls == 0  # full download skipped
    assert doc.summary == "old summary"  # untouched


def test_size_change_triggers_download_and_marks_revised(
    db_session: Session, meeting: Meeting
) -> None:
    doc = _existing_doc(db_session, meeting, b"old-1", size=len("old-1"))
    new_content = b"agenda-v2"  # different length and hash
    client = FakeClient(head_length=len(new_content), get_content=new_content)

    status = scraper.ensure_document(db_session, client, meeting, "agenda", meeting.agenda_url)

    assert status == "revised"
    assert client.head_calls == 1
    assert client.get_calls == 1
    assert doc.pdf_hash == _sha(new_content)
    assert doc.pdf_size == len(new_content)
    assert doc.summary is None
    assert doc.summarized_at is None
    assert doc.notified_email is False
    assert doc.notified_bluesky is False
    assert doc.revised_at is not None


def test_head_failure_falls_back_to_full_download(db_session: Session, meeting: Meeting) -> None:
    content = b"agenda-v1"
    doc = _existing_doc(db_session, meeting, content, size=len(content))
    client = FakeClient(head_error=True, get_content=content)

    status = scraper.ensure_document(db_session, client, meeting, "agenda", meeting.agenda_url)

    assert status == "unchanged"  # hash matches after download
    assert client.head_calls == 1
    assert client.get_calls == 1
    assert doc.pdf_size == len(content)


def test_legacy_row_without_size_downloads_and_backfills_size(
    db_session: Session, meeting: Meeting
) -> None:
    content = b"agenda-v1"
    doc = _existing_doc(db_session, meeting, content, size=None)
    client = FakeClient(get_content=content)

    status = scraper.ensure_document(db_session, client, meeting, "agenda", meeting.agenda_url)

    assert status == "unchanged"
    assert client.head_calls == 0  # no stored size -> no pre-check
    assert client.get_calls == 1
    assert doc.pdf_size == len(content)  # backfilled for next cycle


def test_head_content_length_parses_and_handles_missing() -> None:
    assert scraper.head_content_length(FakeClient(head_length=42), "/x") == 42
    assert scraper.head_content_length(FakeClient(head_length=None), "/x") is None
    assert scraper.head_content_length(FakeClient(head_error=True), "/x") is None


def test_download_pdf_returns_hash_and_size(tmp_path) -> None:
    content = b"hello-pdf"
    client = FakeClient(get_content=content)
    pdf_hash, size = scraper.download_pdf(client, "/x", tmp_path / "out.pdf")
    assert pdf_hash == _sha(content)
    assert size == len(content)
    assert (tmp_path / "out.pdf").read_bytes() == content
