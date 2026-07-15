"""Notify subscribers about new meeting summaries via email and Bluesky."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from council_meetings.config import city, settings
from council_meetings.db import SessionLocal
from council_meetings.models import Document, Meeting
from council_meetings.subscriptions import active_subscribers

logger = logging.getLogger(__name__)


def _doc_label(doc: Document) -> str:
    """Human label for a document, prefixed 'Revised' if the PDF was updated."""
    label = "Agenda" if doc.doc_type == "agenda" else "Minutes"
    return f"Revised {label}" if doc.revised_at else label


def _unsubscribe_url(token: str) -> str:
    base = settings.app_base_url.rstrip("/")
    return f"{base}/unsubscribe/{token}"


def _confirmation_url(token: str) -> str:
    base = settings.app_base_url.rstrip("/")
    return f"{base}/confirm/{token}"


def send_confirmation_email(address: str, token: str) -> bool:
    """Send the single opt-in message for a newly pending subscription."""
    if not settings.email_enabled:
        logger.warning("Cannot send subscription confirmation: SMTP is not configured")
        return False

    confirmation_url = _confirmation_url(token)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Confirm your {city.name} Council email subscription"
    msg["From"] = settings.email_from
    msg["To"] = address
    msg.attach(
        MIMEText(
            f"Confirm your subscription to {city.name} Council meeting summaries:\n"
            f"{confirmation_url}\n\n"
            "If you did not request this, you can ignore this email.",
            "plain",
        )
    )
    msg.attach(
        MIMEText(
            f'<p><a href="{confirmation_url}">Confirm your subscription</a> to '
            f"{city.name} Council meeting summaries.</p>"
            "<p>If you did not request this, you can ignore this email.</p>",
            "html",
        )
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.email_from, [address], msg.as_string())
    except Exception as e:
        logger.error("Failed to send subscription confirmation to %s: %s", address, e)
        return False
    return True


def _build_email_html(meeting: Meeting, doc: Document, unsubscribe_url: str | None) -> str:
    doc_label = _doc_label(doc)
    base = settings.app_base_url.rstrip("/")
    source_url = f"{city.base_url}{doc.source_url}"
    meeting_url = f"{base}/meeting/{meeting.id}"

    summary_html = (doc.summary or "").replace("\n", "<br>")

    unsubscribe_html = ""
    if unsubscribe_url:
        unsubscribe_html = f'<br><a href="{unsubscribe_url}">Unsubscribe</a> from these emails.'

    return f"""\
<h2>{meeting.title} — {doc_label} Summary</h2>
<p><strong>{meeting.date.strftime("%B %d, %Y")}</strong></p>

{summary_html}

<hr>
<p style="font-size: 0.85em; color: #666;">
AI-generated summary — may contain errors.
<a href="{source_url}">Read the original {doc.doc_type} PDF</a> |
<a href="{meeting_url}">View on site</a>{unsubscribe_html}
</p>
"""


def _build_email_text(meeting: Meeting, doc: Document, unsubscribe_url: str | None) -> str:
    doc_label = _doc_label(doc)
    base = settings.app_base_url.rstrip("/")

    unsubscribe_text = f"\nUnsubscribe: {unsubscribe_url}" if unsubscribe_url else ""

    return f"""\
{meeting.title} — {doc_label} Summary
{meeting.date.strftime("%B %d, %Y")}

{doc.summary or "(no summary)"}

---
AI-generated summary — may contain errors.
Original PDF: {city.base_url}{doc.source_url}
View on site: {base}/meeting/{meeting.id}{unsubscribe_text}
"""


def _email_recipients(db: Session) -> list[tuple[str, str | None]]:
    """Resolve the recipient list as ``(address, unsubscribe_token)`` pairs.

    Active subscribers get a personalized unsubscribe token. The static
    ``email_to`` env list is kept as a fallback/admin channel: those addresses
    are always included (with a ``None`` token — no unsubscribe link), except
    when an admin address is itself an active subscriber, to avoid a duplicate
    send.
    """
    recipients: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for sub in active_subscribers(db):
        recipients.append((sub.email, sub.unsubscribe_token))
        seen.add(sub.email.lower())

    for raw in settings.email_to.split(","):
        address = raw.strip()
        key = address.lower()
        if address and key not in seen:
            recipients.append((address, None))
            seen.add(key)

    return recipients


def _build_message(
    meeting: Meeting, doc: Document, subject: str, address: str, token: str | None
) -> MIMEMultipart:
    unsubscribe_url = _unsubscribe_url(token) if token else None

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = address
    if unsubscribe_url:
        # RFC 8058 one-click unsubscribe — the POST endpoint mirrors the GET link.
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.attach(MIMEText(_build_email_text(meeting, doc, unsubscribe_url), "plain"))
    msg.attach(MIMEText(_build_email_html(meeting, doc, unsubscribe_url), "html"))
    return msg


def send_email(meeting: Meeting, doc: Document, db: Session) -> bool:
    """Send per-subscriber email notifications for a new document summary.

    Returns ``True`` when the channel is satisfied (delivered to at least one
    recipient, or there were no recipients to send to), ``False`` on an SMTP
    failure that should be retried on the next pipeline run.
    """
    if not settings.email_enabled:
        return False

    recipients = _email_recipients(db)
    if not recipients:
        logger.info("No email recipients for document %d; skipping", doc.id)
        return True

    doc_label = _doc_label(doc)
    subject = (
        f"{city.name} Council: {meeting.title} — {doc_label} ({meeting.date.strftime('%m/%d/%Y')})"
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)

            sent = 0
            for address, token in recipients:
                msg = _build_message(meeting, doc, subject, address, token)
                try:
                    server.sendmail(settings.email_from, [address], msg.as_string())
                    sent += 1
                except Exception as e:
                    logger.error("Failed to send to %s for document %d: %s", address, doc.id, e)
    except Exception as e:
        logger.error("SMTP failure for document %d: %s", doc.id, e)
        return False

    logger.info("Email sent for document %d to %d/%d recipients", doc.id, sent, len(recipients))
    return sent > 0


def post_bluesky(meeting: Meeting, doc: Document) -> bool:
    """Post a Bluesky notification for a new document summary."""
    if not settings.bluesky_enabled:
        return False

    try:
        from atproto import Client, client_utils

        at_client = Client()
        at_client.login(settings.bluesky_handle, settings.bluesky_app_password)

        doc_label = _doc_label(doc)
        date_str = meeting.date.strftime("%m/%d/%Y")
        meeting_url = f"{settings.app_base_url.rstrip('/')}/meeting/{meeting.id}"

        # Build text — Bluesky has a 300-grapheme limit
        summary_snippet = (doc.summary or "")[:150]
        if len(doc.summary or "") > 150:
            summary_snippet += "…"

        tb = client_utils.TextBuilder()
        tb.text(f"{city.display_name} {doc_label} — {date_str}\n\n")
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

            if not doc.notified_email and settings.email_enabled and send_email(meeting, doc, db):
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
