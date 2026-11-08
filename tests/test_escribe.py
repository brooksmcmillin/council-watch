import copy
import json
from datetime import date
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from council_meetings import escribe, scraper
from council_meetings.config import CityConfig, city, settings
from council_meetings.models import Document, Meeting

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://pub-campbell.escribemeetings.com"


def test_portal_defaults_do_not_mix_other_cities() -> None:
    assert CityConfig().escribe_url == BASE
    assert CityConfig(base_url="https://other.example").escribe_url == ""
    assert CityConfig(category_id="7").escribe_url == ""
    assert CityConfig(escribe_base_url="").escribe_url == ""
    assert (
        CityConfig(escribe_base_url="https://other.example/").escribe_url == "https://other.example"
    )


def records() -> list[dict]:
    return json.loads((FIXTURES / "escribe-calendar.json").read_text())["d"]


def parse_valid(record: dict, base_url: str = BASE) -> dict:
    result = escribe.parse_meeting(record, base_url)
    assert result is not None
    return result


def test_calendar_document_selection_and_legacy_overlap() -> None:
    rows = [parse_valid(record) for record in records()]
    agenda, minutes, executive, legacy = rows
    assert agenda["date"] == date(2026, 11, 2)
    assert agenda["agenda_url"] == BASE + "/FileStream.ashx?DocumentId=2377"
    assert agenda["minutes_url"] is None
    assert agenda["video_url"].startswith(BASE + "/Players/")
    assert minutes["minutes_url"] == BASE + "/FileStream.ashx?DocumentId=2416"
    assert executive["agenda_url"] == BASE + "/FileStream.ashx?DocumentId=1188"
    assert legacy["civicplus_id"] == "3159"
    assert legacy["minutes_url"] == BASE + "/FileStream.ashx?DocumentId=324"
    assert parse_valid(records()[-1], "https://other.example")["civicplus_id"].startswith(
        "escribe-"
    )


@pytest.mark.parametrize("kind", ["City Council Study Session", "City Council Special Meeting"])
def test_all_council_types_and_future_local_dates(kind: str) -> None:
    record = records()[0]
    record.update(MeetingType=kind, StartDate="2027/01/01 00:05:00")
    assert parse_valid(record)["date"] == date(2027, 1, 1)
    record["Cancelled"] = True
    assert escribe.parse_meeting(record, BASE) is None
    record.update(Cancelled=False, MeetingType="Planning Commission")
    assert escribe.parse_meeting(record, BASE) is None


def test_year_ranges_have_no_first_page_limit_and_continue_after_failure() -> None:
    requests = []
    data = []
    for index in range(44):
        record = copy.deepcopy(records()[0])
        record["ID"] = str(UUID(int=index + 1))
        data.append(record)

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body["calendarStartDate"] == "2025-01-01":
            return httpx.Response(503)
        return httpx.Response(200, json={"d": data + [{"ID": "bad"}]})

    errors = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        meetings = escribe.fetch_meetings(client, BASE, {2025, 2026}, errors)
    assert len(meetings) == 44
    assert requests == [
        {"calendarStartDate": "2025-01-01", "calendarEndDate": "2026-01-01"},
        {"calendarStartDate": "2026-01-01", "calendarEndDate": "2027-01-01"},
    ]
    assert len(errors) == 2


@pytest.mark.parametrize(
    "invalid",
    [
        {"MeetingType": None},
        {"MeetingType": 5},
        {"StartDate": None},
        {"StartDate": ""},
        {"StartDate": "   "},
        {"StartDate": "2026/99/99"},
        {"ID": 4},
        {"MeetingDocumentLink": [None]},
        {"MeetingDocumentLink": [{"Type": "Agenda", "Format": None, "Url": "/doc"}]},
        {"MeetingDocumentLink": "not a list"},
    ],
)
def test_malformed_record_does_not_discard_valid_neighbors(invalid: dict) -> None:
    bad = copy.deepcopy(records()[0])
    bad.update(invalid)
    errors = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"d": [records()[0], bad, records()[1]]})
        )
    ) as client:
        meetings = escribe.fetch_meetings(client, BASE, {2026}, errors)
    assert len(meetings) == 2
    assert len(errors) == 1
    assert "Invalid eSCRIBE meeting" in errors[0]


def test_meeting_without_published_links_is_still_discovered() -> None:
    record = records()[0]
    record["MeetingDocumentLink"] = None
    assert parse_valid(record)["agenda_url"] is None


def test_extracts_agenda_body_without_controls_or_challenge() -> None:
    text = escribe.extract_document((FIXTURES / "escribe-document.html").read_text())
    assert "Ayes: 5. Noes: 0." in text
    assert "Staff report attachment" in text
    assert "controls" not in text
    assert "Javascript" not in text
    assert "Download buttons" not in text
    with pytest.raises(ValueError, match="body missing"):
        escribe.extract_document("<html><h1>Verifying your browser</h1></html>")


@pytest.mark.parametrize("pdf_status", [200, 403])
def test_scrape_persists_fallback_and_reuses_archive_identity(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pdf_status: int
) -> None:
    legacy = Meeting(
        date=date(2025, 10, 7),
        title="City Council Regular Meeting 10/7/2025",
        civicplus_id="3159",
        url_date_slug="_10072025-3159",
        agenda_url="/AgendaCenter/ViewFile/Agenda/_10072025-3159",
    )
    db_session.add(legacy)
    db_session.commit()
    legacy_id = legacy.id
    monkeypatch.setattr(scraper, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(city, "escribe_base_url", BASE)
    monkeypatch.setattr(settings, "pdf_storage_dir", str(tmp_path))
    monkeypatch.setattr(scraper, "DOWNLOAD_DELAY", 0)
    body = (FIXTURES / "escribe-document.html").read_text()
    revision = [body]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetCalendarMeetings"):
            return httpx.Response(200, json={"d": records() + [records()[0]]})
        if request.url.path.endswith("FileStream.ashx"):
            return httpx.Response(
                pdf_status,
                text="<h1>Verifying your browser</h1>",
                headers={"content-type": "text/html"},
            )
        if request.url.path.endswith("Meeting.aspx"):
            return httpx.Response(200, text=revision[0])
        return httpx.Response(200, text="")

    monkeypatch.setattr(
        scraper, "_client", lambda: httpx.Client(transport=httpx.MockTransport(respond))
    )
    first = scraper.scrape_meetings(years=[2025])
    assert first.errors is not None  # Legacy additional-minutes PDF has no public HTML alternative.
    assert "No readable minutes for 3159" in first.errors
    assert first.meetings_found == 4
    assert first.new_documents == 6
    assert db_session.query(Meeting).count() == 4
    archived = db_session.get(Meeting, legacy_id)
    assert archived is not None
    assert archived.civicplus_id == "3159"
    assert archived.url_date_slug == "_10072025-3159"
    docs = db_session.query(Document).all()
    assert all(doc.text_content and doc.pdf_path is None for doc in docs)
    assert not list(tmp_path.glob("*.pdf"))
    for doc in docs:
        doc.summary = "existing summary"
        doc.notified_email = True
    db_session.commit()

    second = scraper.scrape_meetings(years=[2025])
    assert second.new_documents == second.revised_documents == 0
    db_session.expire_all()
    assert all(doc.summary == "existing summary" and doc.notified_email for doc in docs)
    revision[0] = body.replace("Ayes: 5", "Ayes: 4")
    third = scraper.scrape_meetings(years=[2025])
    assert third.revised_documents == 6
    db_session.expire_all()
    assert all(doc.summary is None and not doc.notified_email for doc in docs)


def test_absolute_pdf_and_html_to_pdf_upgrade(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "pdf_storage_dir", str(tmp_path))
    monkeypatch.setattr(scraper, "DOWNLOAD_DELAY", 0)
    data = parse_valid(records()[0])
    meeting, _ = scraper.upsert_meeting(db_session, data)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("Meeting.aspx"):
            return httpx.Response(200, text=(FIXTURES / "escribe-document.html").read_text())
        return httpx.Response(
            200, content=b"%PDF-1.7 fixture", headers={"content-type": "application/pdf"}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert (
            scraper.ensure_html_document(
                db_session, client, meeting, "agenda", data["agenda_html_url"]
            )
            == "created"
        )
        doc = db_session.query(Document).one()
        doc.summary = "old HTML summary"
        assert (
            scraper.ensure_document(db_session, client, meeting, "agenda", data["agenda_url"])
            == "revised"
        )
    assert requests[-1] == data["agenda_url"]
    assert doc.text_content is None
    assert doc.summary is None
    assert doc.pdf_path is not None
    assert Path(doc.pdf_path).read_bytes().startswith(b"%PDF-")
