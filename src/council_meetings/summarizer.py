"""Summarize council meeting PDFs using Claude API with native PDF support."""

import base64
import logging
from datetime import UTC, datetime
from pathlib import Path

import anthropic
from anthropic.types import TextBlock

from council_meetings.config import settings
from council_meetings.db import SessionLocal
from council_meetings.models import Document

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"

AGENDA_PROMPT = """\
You are summarizing a city council meeting agenda for Campbell, California.
Write a clear, accessible summary for residents. Include:

1. A brief overview of the meeting (1-2 sentences)
2. A numbered list of key agenda items
3. A "Resident Highlights" section noting items that directly affect residents
   (e.g., public hearings, zoning changes, fee increases, road closures)

Keep the summary under 800 words. Use plain language, not bureaucratic jargon.
If the agenda is mostly procedural (closed session, adjournment), note that briefly."""

MINUTES_PROMPT = """\
You are summarizing city council meeting minutes for Campbell, California.
Write a clear, accessible summary for residents. Include:

1. A brief overview of the meeting (1-2 sentences)
2. Key decisions and votes (include vote counts if available)
3. Notable public comments or testimony
4. Items continued or referred to future meetings

Keep the summary under 1000 words. Use plain language, not bureaucratic jargon.
If the minutes are mostly procedural (closed session), note that briefly."""


def summarize_pdf(pdf_path: str, doc_type: str) -> str:
    """Send a PDF to Claude and get a summary back."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pdf_bytes = path.read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

    prompt = AGENDA_PROMPT if doc_type == "agenda" else MINUTES_PROMPT

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    block = message.content[0]
    if not isinstance(block, TextBlock):
        raise TypeError(f"Expected TextBlock, got {type(block).__name__}")
    return block.text


def summarize_unsummarized() -> int:
    """Find and summarize all documents that don't have summaries yet.

    Returns the number of documents summarized.
    """
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping summarization")
        return 0

    db = SessionLocal()
    try:
        docs = (
            db.query(Document)
            .filter(
                Document.summary.is_(None),
                Document.pdf_path.isnot(None),
            )
            .all()
        )

        if not docs:
            logger.info("No documents to summarize")
            return 0

        logger.info("Summarizing %d documents", len(docs))
        count = 0
        for doc in docs:
            try:
                if not doc.pdf_path:
                    continue
                logger.info("Summarizing %s (id=%d, type=%s)", doc.pdf_path, doc.id, doc.doc_type)
                summary = summarize_pdf(doc.pdf_path, doc.doc_type)
                doc.summary = summary
                doc.summary_model = MODEL
                doc.summarized_at = datetime.now(UTC)
                db.commit()
                count += 1
                logger.info("Summarized document %d", doc.id)
            except Exception as e:
                db.rollback()
                logger.error("Failed to summarize document %d: %s", doc.id, e)

        return count
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = summarize_unsummarized()
    print(f"Summarized {count} documents")
