import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from council_meetings import escribe, scraper
from council_meetings.config import settings
from council_meetings.models import Document

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://pub-campbell.escribemeetings.com"


def test_transient_pdf_failure_preserves_archive_and_delivery_state(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "pdf_storage_dir", str(tmp_path))
    monkeypatch.setattr(scraper, "DOWNLOAD_DELAY", 0)
    data = escribe.parse_meeting(
        json.loads((FIXTURES / "escribe-calendar.json").read_text())["d"][0], BASE
    )
    assert data is not None
    meeting, _ = scraper.upsert_meeting(db_session, data)
    state = {"status": 200, "body": b"%PDF-original"}
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.url.host == httpx.URL(BASE).host
        assert request.url.path.endswith("FileStream.ashx")
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(state["status"], content=state["body"])

    with scraper._client(httpx.MockTransport(respond)) as client:
        assert (
            scraper.ensure_document(db_session, client, meeting, "agenda", data["agenda_url"])
            == "created"
        )
        doc = db_session.query(Document).one()
        doc.summary = "saved PDF summary"
        doc.notified_email = doc.notified_bluesky = True
        doc.email_delivered_to = ["sub@example.invalid"]
        original = (doc.pdf_path, doc.pdf_hash, doc.pdf_size, doc.source_url)
        state["status"] = 403
        for _ in range(2):
            assert (
                scraper.ensure_document(db_session, client, meeting, "agenda", data["agenda_url"])
                is None
            )
            assert (
                scraper.ensure_html_document(
                    db_session,
                    client,
                    meeting,
                    "agenda",
                    data["agenda_html_url"],
                    pdf_url=data["agenda_url"],
                )
                == "unchanged"
            )
        state["status"] = 200
        assert (
            scraper.ensure_document(db_session, client, meeting, "agenda", data["agenda_url"])
            == "unchanged"
        )
        assert (doc.pdf_path, doc.pdf_hash, doc.pdf_size, doc.source_url) == original
        assert doc.pdf_path is not None
        assert Path(doc.pdf_path).read_bytes() == b"%PDF-original"
        assert doc.summary == "saved PDF summary"
        assert doc.notified_email and doc.notified_bluesky
        assert doc.revised_at is None
        assert doc.email_delivered_to == ["sub@example.invalid"]
        state["body"] = b"%PDF-genuine revision"
        assert (
            scraper.ensure_document(db_session, client, meeting, "agenda", data["agenda_url"])
            == "revised"
        )
        assert doc.summary is None
        assert not doc.notified_email and not doc.notified_bluesky
        assert doc.email_delivered_to == []


def test_source_cutover_uses_new_html_without_deleting_old_pdf(
    db_session: Session, tmp_path: Path
) -> None:
    data = escribe.parse_meeting(
        json.loads((FIXTURES / "escribe-calendar.json").read_text())["d"][-1], BASE
    )
    assert data is not None
    meeting, _ = scraper.upsert_meeting(db_session, data)
    archive = tmp_path / "legacy.pdf"
    archive.write_bytes(b"%PDF-legacy archive")
    doc = Document(
        meeting_id=meeting.id,
        doc_type="agenda",
        source_url="/legacy.pdf",
        pdf_path=str(archive),
        pdf_hash="old-hash",
        pdf_size=archive.stat().st_size,
        summary="old source summary",
        notified_email=True,
        email_delivered_to=["sub@example.invalid"],
        notified_bluesky=True,
    )
    db_session.add(doc)
    db_session.flush()
    document_id = doc.id
    with scraper._client(
        httpx.MockTransport(
            lambda _: httpx.Response(200, text=(FIXTURES / "escribe-document.html").read_text())
        )
    ) as client:
        assert (
            scraper.ensure_html_document(
                db_session,
                client,
                meeting,
                "agenda",
                data["agenda_html_url"],
                pdf_url=data["agenda_url"],
            )
            == "revised"
        )
        assert doc.id == document_id and meeting.civicplus_id == "3159"
        assert archive.read_bytes() == b"%PDF-legacy archive"
        assert doc.text_content and doc.pdf_path is None
        assert doc.summary is None and not doc.notified_email and not doc.notified_bluesky
        assert doc.email_delivered_to == []
        doc.summary = "new HTML summary"
        doc.notified_email = True
        doc.email_delivered_to = ["sub@example.invalid"]
        assert (
            scraper.ensure_html_document(
                db_session,
                client,
                meeting,
                "agenda",
                data["agenda_html_url"],
                pdf_url=data["agenda_url"],
            )
            == "unchanged"
        )
        assert doc.summary == "new HTML summary" and doc.notified_email
        assert doc.email_delivered_to == ["sub@example.invalid"]
