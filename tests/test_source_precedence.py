from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from council_meetings import scraper
from council_meetings.config import city
from council_meetings.models import Document, Meeting

BASE = "https://pub-campbell.escribemeetings.com"
LEGACY = '<tr><td><h3 class="noMargin" id="h410072025-3159"></h3><a name="3159">City Council Regular Meeting</a></td></tr>'


@pytest.mark.parametrize("failed_year", [False, True])
@pytest.mark.parametrize("representation", ["pdf", "html"])
def test_legacy_only_cycle_cannot_undo_portal_cutover(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_year: bool,
    representation: str,
) -> None:
    legacy_url = "/AgendaCenter/ViewFile/Agenda/_10072025-3159"
    portal_pdf = BASE + "/FileStream.ashx?DocumentId=207"
    meeting = Meeting(
        date=date(2025, 10, 7),
        title="Existing meeting",
        civicplus_id="3159",
        url_date_slug="_10072025-3159",
        agenda_url=portal_pdf if representation == "pdf" else legacy_url,
    )
    db_session.add(meeting)
    db_session.flush()
    archive = tmp_path / "archive.pdf"
    archive.write_bytes(b"%PDF-existing")
    doc = Document(
        meeting_id=meeting.id,
        doc_type="agenda",
        source_url=portal_pdf if representation == "pdf" else BASE + "/Meeting.aspx?Agenda=Agenda",
        pdf_path=str(archive) if representation == "pdf" else None,
        text_content="Migrated HTML" if representation == "html" else None,
        summary="Migrated summary",
        notified_email=True,
        notified_bluesky=True,
    )
    db_session.add(doc)
    db_session.commit()
    doc_id = doc.id
    monkeypatch.setattr(city, "escribe_base_url", BASE)
    monkeypatch.setattr(scraper, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(scraper, "fetch_current_year", lambda _: LEGACY)
    monkeypatch.setattr(scraper, "fetch_year", lambda *_: "")
    real_client = scraper._client

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == httpx.URL(BASE).host
        # No legacy or current document request should be attempted this cycle.
        assert request.url.path.endswith("GetCalendarMeetings")
        return httpx.Response(503) if failed_year else httpx.Response(200, json={"d": []})

    monkeypatch.setattr(scraper, "_client", lambda: real_client(httpx.MockTransport(respond)))
    log = scraper.scrape_meetings(years=[2025] if failed_year else None)
    assert log.meetings_found == 1
    assert log.new_documents == log.revised_documents == 0
    if failed_year:
        assert log.errors and "Failed to fetch eSCRIBE year" in log.errors
    else:
        assert log.errors is None
    db_session.expire_all()
    saved = db_session.get(Document, doc_id)
    assert saved is not None and saved.summary == "Migrated summary"
    assert saved.notified_email and saved.notified_bluesky
    assert saved.source_url.startswith(BASE + "/")
    assert saved.text_content == ("Migrated HTML" if representation == "html" else None)
    assert saved.pdf_path == (str(archive) if representation == "pdf" else None)
    assert archive.read_bytes() == b"%PDF-existing"
    assert db_session.query(Meeting).count() == 1
