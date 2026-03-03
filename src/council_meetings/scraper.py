"""Scrape Campbell City Council agendas and minutes from CivicPlus."""

import hashlib
import logging
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from council_meetings.config import settings
from council_meetings.db import SessionLocal
from council_meetings.models import Document, Meeting, ScrapeLog

logger = logging.getLogger(__name__)

BASE_URL = "https://www.campbellca.gov"
AGENDA_CENTER = f"{BASE_URL}/AgendaCenter/City-Council-10"
BACKFILL_URL = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
USER_AGENT = "CampbellCouncilMonitor/1.0 (+https://github.com/brooksmcmillin/council-meetings)"
DOWNLOAD_DELAY = 2  # seconds between PDF downloads

# Pattern: MMDDYYYY-ID (optionally prefixed with _ or h4)
SLUG_RE = re.compile(r"(\d{8})-(\d+)")


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=30.0,
    )


def _parse_date_from_slug(slug: str) -> date | None:
    """Parse date from slug like '_10072025-3159' -> 2025-10-07."""
    m = SLUG_RE.search(slug)
    if not m:
        return None
    mmddyyyy = m.group(1)
    try:
        return datetime.strptime(mmddyyyy, "%m%d%Y").date()
    except ValueError:
        return None


def parse_meetings_html(html: str) -> list[dict]:
    """Parse meeting entries from CivicPlus HTML.

    Returns list of dicts with keys:
        civicplus_id, url_date_slug, date, title, agenda_url, minutes_url, video_url
    """
    soup = BeautifulSoup(html, "html.parser")
    meetings: list[dict] = []

    for h3 in soup.find_all("h3", class_="noMargin"):
        h3_id = h3.get("id", "")
        # h3 id is like "h409162025-3145"
        slug_match = SLUG_RE.search(h3_id)
        if not slug_match:
            continue

        url_date_slug = f"_{slug_match.group(1)}-{slug_match.group(2)}"
        civicplus_id = slug_match.group(2)
        meeting_date = _parse_date_from_slug(url_date_slug)
        if not meeting_date:
            continue

        # Find the title in the <a> after the h3 (in the next <p>)
        title = ""
        # The <a> with name=ID has the meeting title text
        title_link = h3.find_next("a", attrs={"name": civicplus_id})
        if title_link:
            title = title_link.get_text(strip=True)

        agenda_url = f"/AgendaCenter/ViewFile/Agenda/{url_date_slug}"

        # Minutes: look for td.minutes within the same row
        minutes_url = None
        row = h3.find_parent("tr")
        if row:
            minutes_td = row.find("td", class_="minutes")
            if minutes_td:
                minutes_link = minutes_td.find("a")
                if minutes_link and minutes_link.get("href"):
                    minutes_url = minutes_link["href"]

        # Video: look for td.media
        video_url = None
        if row:
            media_td = row.find("td", class_="media")
            if media_td:
                video_link = media_td.find("a")
                if video_link and video_link.get("href"):
                    video_url = video_link["href"]

        meetings.append(
            {
                "civicplus_id": civicplus_id,
                "url_date_slug": url_date_slug,
                "date": meeting_date,
                "title": title,
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
                "video_url": video_url,
            }
        )

    return meetings


def fetch_current_year(client: httpx.Client) -> str:
    """Fetch the main agenda page (current year)."""
    resp = client.get(AGENDA_CENTER)
    resp.raise_for_status()
    return resp.text


def fetch_year(client: httpx.Client, year: int) -> str:
    """Fetch meetings for a specific year via the AJAX backfill endpoint."""
    resp = client.post(
        BACKFILL_URL,
        data={"year": str(year), "catID": "10"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    resp.raise_for_status()
    return resp.text


def download_pdf(client: httpx.Client, relative_url: str, dest_path: Path) -> str:
    """Download a PDF and return its SHA-256 hash."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE_URL}{relative_url}"
    resp = client.get(url)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    return hashlib.sha256(resp.content).hexdigest()


def upsert_meeting(db: Session, data: dict) -> tuple[Meeting, bool]:
    """Insert or update a meeting row. Returns (meeting, is_new)."""
    existing = db.query(Meeting).filter_by(civicplus_id=data["civicplus_id"]).first()
    if existing:
        changed = False
        if data["minutes_url"] and not existing.minutes_url:
            existing.minutes_url = data["minutes_url"]
            changed = True
        if data["video_url"] and not existing.video_url:
            existing.video_url = data["video_url"]
            changed = True
        if changed:
            db.flush()
        return existing, False

    meeting = Meeting(
        date=data["date"],
        title=data["title"],
        civicplus_id=data["civicplus_id"],
        url_date_slug=data["url_date_slug"],
        agenda_url=data["agenda_url"],
        minutes_url=data["minutes_url"],
        video_url=data["video_url"],
    )
    db.add(meeting)
    db.flush()
    return meeting, True


def ensure_document(
    db: Session,
    client: httpx.Client,
    meeting: Meeting,
    doc_type: str,
    source_url: str,
) -> Document | None:
    """Ensure a document exists for this meeting/type. Download PDF if new.

    Returns the Document if newly created, None if already existed.
    """
    existing = (
        db.query(Document)
        .filter_by(meeting_id=meeting.id, doc_type=doc_type)
        .first()
    )
    if existing:
        return None

    # Build local path: data/pdfs/2025-09-16_agenda_3145.pdf
    date_str = meeting.date.isoformat()
    filename = f"{date_str}_{doc_type}_{meeting.civicplus_id}.pdf"
    pdf_path = Path(settings.pdf_storage_dir) / filename

    logger.info("Downloading %s for %s (%s)", doc_type, meeting.title, source_url)
    try:
        pdf_hash = download_pdf(client, source_url, pdf_path)
    except httpx.HTTPError as e:
        logger.error("Failed to download %s: %s", source_url, e)
        return None

    time.sleep(DOWNLOAD_DELAY)

    doc = Document(
        meeting_id=meeting.id,
        doc_type=doc_type,
        source_url=source_url,
        pdf_path=str(pdf_path),
        pdf_hash=pdf_hash,
    )
    db.add(doc)
    db.flush()
    return doc


def scrape_meetings(years: list[int] | None = None) -> ScrapeLog:
    """Run a full scrape cycle. Returns the ScrapeLog entry."""
    log = ScrapeLog(started_at=datetime.now(UTC), meetings_found=0, new_documents=0)
    errors: list[str] = []

    db = SessionLocal()
    try:
        client = _client()

        # Fetch HTML
        html_pages: list[str] = []
        try:
            html_pages.append(fetch_current_year(client))
        except httpx.HTTPError as e:
            errors.append(f"Failed to fetch current year: {e}")

        for year in years or []:
            try:
                html_pages.append(fetch_year(client, year))
            except httpx.HTTPError as e:
                errors.append(f"Failed to fetch year {year}: {e}")

        # Parse all pages
        all_meetings: list[dict] = []
        for html in html_pages:
            all_meetings.extend(parse_meetings_html(html))

        # Deduplicate by civicplus_id (prefer first occurrence)
        seen: set[str] = set()
        unique_meetings: list[dict] = []
        for m in all_meetings:
            if m["civicplus_id"] not in seen:
                seen.add(m["civicplus_id"])
                unique_meetings.append(m)

        log.meetings_found = len(unique_meetings)

        new_docs = 0
        for data in unique_meetings:
            savepoint = db.begin_nested()
            try:
                meeting, _is_new = upsert_meeting(db, data)

                # Agenda
                if data["agenda_url"]:
                    doc = ensure_document(
                        db, client, meeting, "agenda", data["agenda_url"]
                    )
                    if doc:
                        new_docs += 1

                # Minutes
                if data["minutes_url"]:
                    doc = ensure_document(
                        db, client, meeting, "minutes", data["minutes_url"]
                    )
                    if doc:
                        new_docs += 1

                savepoint.commit()
            except Exception as e:
                savepoint.rollback()
                msg = f"Error processing meeting {data.get('civicplus_id')}: {e}"
                logger.error(msg)
                errors.append(msg)

        log.new_documents = new_docs
        log.finished_at = datetime.now(UTC)
        log.errors = "\n".join(errors) if errors else None

        db.add(log)
        db.commit()
        db.expunge(log)

    except Exception as e:
        logger.exception("Scrape failed")
        log.errors = str(e)
        log.finished_at = datetime.now(UTC)
        try:
            db.add(log)
            db.commit()
            db.expunge(log)
        except Exception:
            db.rollback()
    finally:
        db.close()

    return log


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log = scrape_meetings()
    print(
        f"Scrape complete: {log.meetings_found} meetings found, "
        f"{log.new_documents} new documents"
    )
    if log.errors:
        print(f"Errors:\n{log.errors}")
