"""Campbell's public eSCRIBE calendar and HTML document adapter."""

from datetime import datetime
from uuid import UUID

import httpx
from bs4 import BeautifulSoup

from council_meetings.config import resolve_public_url

# Both city portals publish this same regular meeting. Keep its public /meeting
# row and downloaded archive instead of inventing a second meeting at cutover.
CAMPBELL_OVERLAP = {"fdd357dd-6794-4d51-8709-ff788aeb167e": "3159"}


def parse_meeting(record: dict, base_url: str) -> dict | None:
    """Normalize calendar fields to the existing scraper's meeting contract."""
    meeting_type = record["MeetingType"]
    start_date = record["StartDate"]
    if not isinstance(meeting_type, str) or not isinstance(start_date, str):
        raise ValueError("expected meeting type and start date strings")
    meeting_type = meeting_type.strip()
    if not meeting_type.startswith("City Council ") or record.get("Cancelled", False):
        return None
    uid = str(UUID(str(record["ID"])))
    # Parse the portal's local calendar date, not a UTC-converted timestamp.
    meeting_date = datetime.strptime(start_date.split(" ", 1)[0], "%Y/%m/%d").date()  # noqa: DTZ007
    links = record["MeetingDocumentLink"] or []
    if not isinstance(links, list) or any(
        not isinstance(item, dict)
        or any(not isinstance(item.get(key), str) for key in ("Type", "Format", "Url"))
        for item in links
    ):
        raise ValueError("expected document link objects with type, format and URL strings")

    def link(types: tuple[str, ...], fmt: str) -> str | None:
        for kind in types:
            for item in links:
                if item["Type"] == kind and item["Format"].lower() == fmt:
                    resolved = resolve_public_url(base_url, item["Url"])
                    if resolved:
                        return resolved
        return None

    minutes_pdf = link(("PostMinutes", "Minutes"), ".pdf")
    if not minutes_pdf:
        # Early Campbell minutes were uploaded as additional documents; a
        # regular meeting can also link executive minutes, so match the type.
        session = "Regular" if "Regular" in meeting_type else "Executive"
        for item in links:
            if (
                item["Type"] == "AdditionalDocuments"
                and item["Format"].lower() == ".pdf"
                and isinstance(item.get("Title"), str)
                and item["Title"].startswith(f"Approved {session} Session Min.")
                and ("Regular" in meeting_type or "Executive" in meeting_type)
            ):
                minutes_pdf = resolve_public_url(base_url, item["Url"]) or None
                if minutes_pdf:
                    break

    source_id = f"escribe-{uid}"
    if base_url == "https://pub-campbell.escribemeetings.com":
        source_id = CAMPBELL_OVERLAP.get(uid, source_id)
    return {
        "civicplus_id": source_id,
        "url_date_slug": source_id,
        "date": meeting_date,
        "title": f"{meeting_type} {meeting_date.isoformat()}",
        "agenda_url": link(("Agenda", "AgendaCover"), ".pdf"),
        "minutes_url": minutes_pdf,
        "video_url": link(("Video",), "video"),
        "agenda_html_url": link(("Agenda", "AgendaCover"), "html"),
        "minutes_html_url": link(("PostMinutes", "Minutes"), "html"),
    }


def fetch_meetings(
    client: httpx.Client, base_url: str, years: set[int], errors: list[str]
) -> list[dict]:
    """Calendar ranges include all Council types, past and upcoming, without paging.

    This is the same endpoint/range contract used by the public calendar UI.
    Request years separately so a failure doesn't discard other years.
    """
    meetings: list[dict] = []
    for year in sorted(years):
        try:
            response = client.post(
                f"{base_url}/MeetingsCalendarView.aspx/GetCalendarMeetings",
                json={
                    "calendarStartDate": f"{year}-01-01",
                    "calendarEndDate": f"{year + 1}-01-01",
                },
            )
            response.raise_for_status()
            records = response.json()["d"]
            if not isinstance(records, list):
                raise ValueError("expected eSCRIBE calendar list")
            for record in records:
                try:
                    meeting = parse_meeting(record, base_url)
                    if meeting is not None:
                        meetings.append(meeting)
                except (KeyError, TypeError, ValueError) as error:
                    errors.append(f"Invalid eSCRIBE meeting in {year}: {error}")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            errors.append(f"Failed to fetch eSCRIBE year {year}: {error}")
    return meetings


def extract_document(html: str) -> str:
    """Read only the public agenda/minutes body, not scripts or portal controls."""
    soup = BeautifulSoup(html, "html.parser")
    agenda = soup.select_one(".Agenda")
    if agenda is None:
        raise ValueError("eSCRIBE document body missing (possibly a browser challenge)")
    for element in agenda.select("script, style, button, .AgendaItemIcons"):
        element.decompose()
    text = agenda.get_text("\n", strip=True)
    if not text:
        raise ValueError("eSCRIBE document body is empty")
    return text
