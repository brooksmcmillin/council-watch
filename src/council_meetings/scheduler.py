"""APScheduler pipeline: scrape -> summarize -> notify."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from council_meetings.config import settings

logger = logging.getLogger(__name__)


def run_pipeline() -> None:
    """Run the full scrape -> summarize -> notify pipeline."""
    logger.info("Pipeline starting")

    # 1. Scrape
    from council_meetings.scraper import scrape_meetings

    log = scrape_meetings()
    logger.info(
        "Scrape done: %d meetings found, %d new documents",
        log.meetings_found,
        log.new_documents,
    )

    # 2. Summarize
    from council_meetings.summarizer import summarize_unsummarized

    count = summarize_unsummarized()
    logger.info("Summarized %d documents", count)

    # 3. Notify
    from council_meetings.notifier import notify_new_summaries

    notified = notify_new_summaries()
    logger.info("Notified %d documents", notified)

    logger.info("Pipeline complete")


def start_scheduler() -> BackgroundScheduler:
    """Create and start the APScheduler background scheduler."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_pipeline,
        "interval",
        minutes=settings.scrape_interval_minutes,
        id="pipeline",
        name="Scrape/Summarize/Notify Pipeline",
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — pipeline runs every %d minutes",
        settings.scrape_interval_minutes,
    )
    return scheduler
