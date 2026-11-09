"""Scrape a CivicPlus city council's agendas and minutes.

The city being monitored is configured via :data:`council_meetings.config.city`
(Campbell, CA by default), so no URLs or category ids are hard-coded here.
"""

import hashlib
import logging
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from council_meetings.config import city, settings
from council_meetings.db import SessionLocal
from council_meetings.escribe import extract_document
from council_meetings.escribe import fetch_meetings as fetch_escribe_meetings
from council_meetings.models import Document, Meeting, ScrapeLog

logger = logging.getLogger(__name__)

DOWNLOAD_DELAY = 2  # seconds between PDF downloads

# Pattern: MMDDYYYY-ID (optionally prefixed with _ or h4)
SLUG_RE = re.compile(r"(\d{8})-(\d+)")


def _check_source_request(request: httpx.Request) -> None:
    """Apply the same source boundary to initial requests and every redirect hop."""
    url = request.url
    allowed = [httpx.URL(base) for base in (city.base_url, city.escribe_url) if base]
    if (
        url.scheme not in {"http", "https"}
        or url.userinfo
        or not any(
            (url.scheme, url.host, url.port) == (base.scheme, base.host, base.port)
            for base in allowed
        )
    ):
        raise httpx.RequestError("Request outside configured city source origins", request=request)


def _client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        event_hooks={"request": [_check_source_request]},
        headers={
            "User-Agent": city.user_agent,
            # Force identity encoding so a GET's stored byte size (len of the
            # decoded body) and a HEAD's raw Content-Length are always in the
            # same units, keeping the size pre-check in ensure_document exact.
            # PDFs are already compressed, so this costs no extra transfer.
            "Accept-Encoding": "identity",
        },
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
        return datetime.strptime(mmddyyyy, "%m%d%Y").date()  # noqa: DTZ007
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
        h3_id = str(h3.get("id", ""))
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
    resp = client.get(city.agenda_center_url)
    resp.raise_for_status()
    return resp.text


def fetch_year(client: httpx.Client, year: int) -> str:
    """Fetch meetings for a specific year via the AJAX backfill endpoint."""
    resp = client.post(
        city.backfill_url,
        data={"year": str(year), "catID": city.category_id},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    resp.raise_for_status()
    return resp.text


def download_pdf(client: httpx.Client, relative_url: str, dest_path: Path) -> tuple[str, int]:
    """Download a PDF and return its (SHA-256 hash, byte size)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    url = urljoin(city.base_url + "/", relative_url)
    temp_path = dest_path.with_suffix(f"{dest_path.suffix}.part")
    digest = hashlib.sha256()
    size = 0

    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            if "text/html" in resp.headers.get("content-type", "").lower():
                raise httpx.HTTPError(f"Expected PDF, received HTML from {url}")
            with temp_path.open("wb") as output:
                for chunk in resp.iter_bytes():
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        temp_path.replace(dest_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return digest.hexdigest(), size


def head_content_length(client: httpx.Client, relative_url: str) -> int | None:
    """Return the server-reported Content-Length for a URL via a HEAD request.

    Used as a cheap pre-check to avoid re-downloading unchanged PDFs. Returns
    ``None`` when the request fails or the server omits a usable
    ``Content-Length`` header, in which case callers fall back to a full
    download. CivicPlus ViewFile responses carry no ``ETag`` / ``Last-Modified``
    validators, so a HEAD size check is the only body-free freshness signal
    available.
    """
    url = urljoin(city.base_url + "/", relative_url)
    try:
        resp = client.head(url)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("HEAD pre-check failed for %s: %s", url, e)
        return None
    raw = resp.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def upsert_meeting(db: Session, data: dict) -> tuple[Meeting, bool]:
    """Insert or update a meeting row. Returns (meeting, is_new)."""
    existing = db.query(Meeting).filter_by(civicplus_id=data["civicplus_id"]).first()
    if existing:
        changed = False
        for key in ("agenda_url", "minutes_url", "video_url"):
            # Current portal links can change. Legacy listings must not replace
            # links already migrated to eSCRIBE or remove a published link.
            if (
                data[key]
                and ("agenda_html_url" in data or not getattr(existing, key))
                and getattr(existing, key) != data[key]
            ):
                setattr(existing, key, data[key])
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
) -> str | None:
    """Ensure a document exists and is current for this meeting/type.

    New documents are downloaded. For existing documents, a cheap HEAD
    ``Content-Length`` pre-check is issued first: when the server-reported size
    matches the stored ``pdf_size`` and the local PDF exists, the source is
    assumed unchanged and the full download (which can be 100+ MB for agenda
    packets) is skipped. Otherwise the
    PDF is re-downloaded and its SHA-256 compared against the stored ``pdf_hash``
    so that revisions published on the city side are detected. When a source PDF
    has changed, the stored file is replaced and the summary + notification state
    are cleared so the pipeline re-summarizes and re-notifies.

    The size pre-check is a weaker signal than the hash: a revision that keeps
    the exact same byte count is skipped. This is an accepted trade-off — PDF
    edits virtually always change the byte size, revisions are rare, and full
    hash verification still runs whenever a download does occur. CivicPlus does
    not send ``ETag`` / ``Last-Modified``, so proper conditional GETs are not an
    option; a HEAD size check is the only body-free freshness signal available.

    Returns a status string:
        "created"   – a brand-new document was downloaded
        "revised"   – an existing document's source PDF changed
        "unchanged" – an existing document's source PDF was identical (by hash,
                      or skipped via a matching HEAD Content-Length)
        None        – the download failed
    """
    existing = db.query(Document).filter_by(meeting_id=meeting.id, doc_type=doc_type).first()

    # HEAD pre-check: when we have a local file and stored size, avoid the full GET if
    # the server-reported Content-Length is unchanged. Skips both the (large)
    # body transfer and the DOWNLOAD_DELAY for the common unchanged case.
    if (
        existing is not None
        and existing.pdf_path
        and existing.pdf_size is not None
        and existing.source_url == source_url
        and Path(existing.pdf_path).is_file()
    ):
        remote_size = head_content_length(client, source_url)
        if remote_size is not None and remote_size == existing.pdf_size:
            logger.debug(
                "Skipping unchanged %s for %s (size %d)", doc_type, meeting.title, remote_size
            )
            return "unchanged"

    # Build local path: data/pdfs/2025-09-16_agenda_3145.pdf. Reuse the existing
    # path when present so a revised PDF overwrites the file already on disk.
    if existing and existing.pdf_path:
        pdf_path = Path(existing.pdf_path)
    else:
        date_str = meeting.date.isoformat()
        filename = f"{date_str}_{doc_type}_{meeting.civicplus_id}.pdf"
        pdf_path = Path(settings.pdf_storage_dir) / filename

    action = "Re-downloading" if existing else "Downloading"
    logger.info("%s %s for %s (%s)", action, doc_type, meeting.title, source_url)
    try:
        pdf_hash, pdf_size = download_pdf(client, source_url, pdf_path)
    except httpx.HTTPError as e:
        logger.error("Failed to download %s: %s", source_url, e)
        return None

    time.sleep(DOWNLOAD_DELAY)

    if existing is None:
        doc = Document(
            meeting_id=meeting.id,
            doc_type=doc_type,
            source_url=source_url,
            pdf_path=str(pdf_path),
            pdf_hash=pdf_hash,
            pdf_size=pdf_size,
        )
        db.add(doc)
        db.flush()
        return "created"

    if existing.pdf_hash == pdf_hash:
        # Backfill pdf_size for legacy rows so future scrapes can use the HEAD
        # pre-check even though nothing changed this cycle.
        existing.pdf_size = pdf_size
        existing.source_url = source_url
        db.flush()
        return "unchanged"

    logger.info(
        "Detected revised %s for %s (%s -> %s)",
        doc_type,
        meeting.title,
        existing.pdf_hash,
        pdf_hash,
    )
    existing.pdf_path = str(pdf_path)
    existing.pdf_hash = pdf_hash
    existing.pdf_size = pdf_size
    existing.source_url = source_url
    existing.text_content = None
    _clear_summary(existing)
    db.flush()
    return "revised"


def _clear_summary(doc: Document) -> None:
    doc.summary = None
    doc.summary_model = None
    doc.summarized_at = None
    doc.notified_email = False
    doc.email_delivered_to = []
    doc.notified_bluesky = False
    doc.revised_at = datetime.now(UTC)


def ensure_html_document(
    db: Session,
    client: httpx.Client,
    meeting: Meeting,
    doc_type: str,
    source_url: str,
    pdf_url: str | None = None,
) -> str:
    """Use HTML when no current PDF is saved, not on a transient refresh failure."""
    existing = db.query(Document).filter_by(meeting_id=meeting.id, doc_type=doc_type).first()
    if (
        existing is not None
        and existing.source_url == pdf_url
        and existing.pdf_path
        and Path(existing.pdf_path).is_file()
    ):
        return "unchanged"
    response = client.get(source_url)
    response.raise_for_status()
    text = extract_document(response.text)
    if existing is None:
        db.add(
            Document(
                meeting_id=meeting.id, doc_type=doc_type, source_url=source_url, text_content=text
            )
        )
        db.flush()
        return "created"
    if existing.text_content == text:
        existing.source_url = source_url
        return "unchanged"
    existing.text_content = text
    existing.source_url = source_url
    existing.pdf_path = None
    existing.pdf_hash = None
    existing.pdf_size = None
    _clear_summary(existing)
    db.flush()
    return "revised"


def scrape_meetings(years: list[int] | None = None) -> ScrapeLog:
    """Run a full scrape cycle. Returns the ScrapeLog entry."""
    log = ScrapeLog(
        started_at=datetime.now(UTC),
        meetings_found=0,
        new_documents=0,
        revised_documents=0,
    )
    errors: list[str] = []

    db = SessionLocal()
    client: httpx.Client | None = None
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

        if city.escribe_url:
            # Current-source records take precedence at the archive overlap.
            current = fetch_escribe_meetings(
                client, city.escribe_url, {datetime.now(UTC).year, *(years or [])}, errors
            )
            all_meetings = current + all_meetings

        # Deduplicate by civicplus_id (prefer first occurrence)
        seen: set[str] = set()
        unique_meetings: list[dict] = []
        for m in all_meetings:
            if m["civicplus_id"] not in seen:
                seen.add(m["civicplus_id"])
                unique_meetings.append(m)

        log.meetings_found = len(unique_meetings)

        new_docs = 0
        revised_docs = 0
        for data in unique_meetings:
            savepoint = db.begin_nested()
            try:
                meeting, _is_new = upsert_meeting(db, data)

                for doc_type, url_key in (("agenda", "agenda_url"), ("minutes", "minutes_url")):
                    # A legacy-only run must not undo an earlier source cutover,
                    # including when the portal's overlap year failed this cycle.
                    if "agenda_html_url" not in data and city.escribe_url:
                        saved = (
                            db.query(Document)
                            .filter_by(meeting_id=meeting.id, doc_type=doc_type)
                            .first()
                        )
                        sources = (getattr(meeting, url_key), saved.source_url if saved else None)
                        if any(
                            source and source.startswith(city.escribe_url + "/")
                            for source in sources
                        ):
                            continue
                    pdf_url = data[url_key]
                    html_url = data.get(f"{doc_type}_html_url")
                    if not pdf_url and not html_url:
                        continue
                    status = (
                        ensure_document(db, client, meeting, doc_type, pdf_url) if pdf_url else None
                    )
                    if status is None and html_url:
                        try:
                            status = ensure_html_document(
                                db, client, meeting, doc_type, html_url, pdf_url=pdf_url
                            )
                        except (httpx.HTTPError, ValueError) as error:
                            errors.append(
                                f"Failed {doc_type} HTML for {data['civicplus_id']}: {error}"
                            )
                    if status is None:
                        errors.append(
                            f"No readable {doc_type} for {data['civicplus_id']} (PDF={pdf_url}, HTML={html_url})"
                        )
                    elif status == "created":
                        new_docs += 1
                    elif status == "revised":
                        revised_docs += 1

                savepoint.commit()
            except Exception as e:
                savepoint.rollback()
                msg = f"Error processing meeting {data.get('civicplus_id')}: {e}"
                logger.error(msg)
                errors.append(msg)

        log.new_documents = new_docs
        log.revised_documents = revised_docs
        log.finished_at = datetime.now(UTC)
        log.errors = "\n".join(errors) if errors else None

        db.add(log)
        db.commit()
        # Commit expires columns; reload them before returning a detached result.
        db.refresh(log)
        db.expunge(log)

    except Exception as e:
        logger.exception("Scrape failed")
        log.errors = str(e)
        log.finished_at = datetime.now(UTC)
        try:
            db.add(log)
            db.commit()
            db.refresh(log)
            db.expunge(log)
        except Exception:
            db.rollback()
    finally:
        db.close()
        if client is not None:
            client.close()

    return log


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log = scrape_meetings()
    print(
        f"Scrape complete: {log.meetings_found} meetings found, "
        f"{log.new_documents} new documents, {log.revised_documents} revised"
    )
    if log.errors:
        print(f"Errors:\n{log.errors}")
