"""Notify subscribers about new meeting summaries via email and Bluesky."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from council_meetings.config import settings
from council_meetings.db import SessionLocal
from council_meetings.models import Document, Meeting

logger = logging.getLogger(__name__)


def _build_email_html(meeting: Meeting, doc: Document) -> str:
    doc_label = "Agenda" if doc.doc_type == "agenda" else "Minutes"
    base = settings.app_base_url.rstrip("/")
    city_base = "https://www.campbellca.gov"
    source_url = f"{city_base}{doc.source_url}"
    meeting_url = f"{base}/meeting/{meeting.id}"

    summary_html = (doc.summary or "").replace("\n", "<br>")

    return f"""\
<h2>{meeting.title} — {doc_label} Summary</h2>
<p><strong>{meeting.date.strftime("%B %d, %Y")}</strong></p>

{summary_html}

<hr>
<p style="font-size: 0.85em; color: #666;">
AI-generated summary — may contain errors.
<a href="{source_url}">Read the original {doc_label.lower()} PDF</a> |
<a href="{meeting_url}">View on site</a>
</p>
"""


def _build_email_text(meeting: Meeting, doc: Document) -> str:
    doc_label = "Agenda" if doc.doc_type == "agenda" else "Minutes"
    base = settings.app_base_url.rstrip("/")
    city_base = "https://www.campbellca.gov"

    return f"""\
{meeting.title} — {doc_label} Summary
{meeting.date.strftime("%B %d, %Y")}

{doc.summary or "(no summary)"}

---
AI-generated summary — may contain errors.
Original PDF: {city_base}{doc.source_url}
View on site: {base}/meeting/{meeting.id}
"""


def send_email(meeting: Meeting, doc: Document) -> bool:
    """Send an email notification for a new document summary."""
    if not settings.email_enabled:
        return False

    doc_label = "Agenda" if doc.doc_type == "agenda" else "Minutes"
    subject = (
        f"Campbell Council: {meeting.title} — {doc_label} ({meeting.date.strftime('%m/%d/%Y')})"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = settings.email_to

    msg.attach(MIMEText(_build_email_text(meeting, doc), "plain"))
    msg.attach(MIMEText(_build_email_html(meeting, doc), "html"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            recipients = [r.strip() for r in settings.email_to.split(",")]
            server.sendmail(settings.email_from, recipients, msg.as_string())
        logger.info("Email sent for document %d", doc.id)
        return True
    except Exception as e:
        logger.error("Failed to send email for document %d: %s", doc.id, e)
        return False


def post_bluesky(meeting: Meeting, doc: Document) -> bool:
    """Post a Bluesky notification for a new document summary."""
    if not settings.bluesky_enabled:
        return False

    try:
        from atproto import Client, client_utils

        at_client = Client()
        at_client.login(settings.bluesky_handle, settings.bluesky_app_password)

        doc_label = "Agenda" if doc.doc_type == "agenda" else "Minutes"
        date_str = meeting.date.strftime("%m/%d/%Y")
        meeting_url = f"{settings.app_base_url.rstrip('/')}/meeting/{meeting.id}"

        # Build text — Bluesky has a 300-grapheme limit
        summary_snippet = (doc.summary or "")[:150]
        if len(doc.summary or "") > 150:
            summary_snippet += "…"

        tb = client_utils.TextBuilder()
        tb.text(f"Campbell City Council {doc_label} — {date_str}\n\n")
        tb.text(f"{summary_snippet}\n\n")
        tb.link("View full summary", meeting_url)

        at_client.send_post(tb)
        logger.info("Bluesky post sent for document %d", doc.id)
        return True
    except Exception as e:
        logger.error("Failed to post to Bluesky for document %d: %s", doc.id, e)
        return False


def notify_new_summaries() -> int:
    """Send notifications for documents that have summaries but haven't been notified yet.

    Returns the number of documents notified.
    """
    db = SessionLocal()
    try:
        docs = (
            db.query(Document)
            .filter(
                Document.summary.isnot(None),
                (Document.notified_email == False)  # noqa: E712
                | (Document.notified_bluesky == False),  # noqa: E712
            )
            .all()
        )

        if not docs:
            return 0

        count = 0
        for doc in docs:
            meeting = db.query(Meeting).filter_by(id=doc.meeting_id).first()
            if not meeting:
                continue

            if not doc.notified_email and settings.email_enabled and send_email(meeting, doc):
                doc.notified_email = True

            if not doc.notified_bluesky and settings.bluesky_enabled and post_bluesky(meeting, doc):
                doc.notified_bluesky = True

            # If neither channel is configured, mark as notified to avoid re-processing
            if not settings.email_enabled:
                doc.notified_email = True
            if not settings.bluesky_enabled:
                doc.notified_bluesky = True

            db.commit()
            count += 1

        return count
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = notify_new_summaries()
    print(f"Notified {count} documents")
