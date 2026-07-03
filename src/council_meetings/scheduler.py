"""APScheduler pipeline: scrape -> summarize -> notify."""

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from council_meetings.config import settings

logger = logging.getLogger(__name__)


def run_pipeline(years: list[int] | None = None) -> None:
    """Run the full scrape -> summarize -> notify pipeline.

    When ``years`` is given, those prior years are re-scraped via the AJAX
    backfill endpoint in addition to the current year.
    """
    logger.info("Pipeline starting%s", f" (backfill years={years})" if years else "")

    # 1. Scrape
    from council_meetings.scraper import scrape_meetings

    log = scrape_meetings(years=years)
    logger.info(
        "Scrape done: %d meetings found, %d new documents, %d revised",
        log.meetings_found,
        log.new_documents,
        log.revised_documents,
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


def run_backfill() -> None:
    """Re-scrape the last ``settings.backfill_years`` prior years.

    Runs the full pipeline so late-posted minutes and revised documents from
    older meetings are summarized and notified like any other change. The
    current year is always scraped by the regular pipeline, so only strictly
    prior years are requested here.
    """
    if settings.backfill_years <= 0:
        logger.info("Backfill disabled (backfill_years=%d)", settings.backfill_years)
        return

    current_year = datetime.now(UTC).year
    years = [current_year - n for n in range(1, settings.backfill_years + 1)]
    logger.info("Backfill starting for years %s", years)
    run_pipeline(years=years)


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
    if settings.backfill_years > 0:
        scheduler.add_job(
            run_backfill,
            "interval",
            hours=settings.backfill_interval_hours,
            id="backfill",
            name="Historical-Year Backfill",
            misfire_grace_time=3600,
        )
    scheduler.start()
    logger.info(
        "Scheduler started — pipeline runs every %d minutes; "
        "backfill of %d prior year(s) runs every %d hours",
        settings.scrape_interval_minutes,
        settings.backfill_years,
        settings.backfill_interval_hours,
    )
    return scheduler
