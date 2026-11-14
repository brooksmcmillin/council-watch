import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from council_meetings import diagnostics, notifier, scheduler, scraper, summarizer
from council_meetings.config import settings


def test_memory_log_allowlists_numeric_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        Path, "read_text", lambda _: "Name:\tprivate-content\nVmRSS:\t123 kB\nVmHWM:\t456 kB\n"
    )
    with caplog.at_level(logging.INFO, logger="council_meetings.diagnostics"):
        diagnostics.log_memory("document.start", 42)
    assert "stage=document.start document_id=42 rss_kib=123 peak_rss_kib=456" in caplog.text
    assert "private-content" not in caplog.text


@pytest.mark.parametrize("malformed", [False, True])
def test_unavailable_memory_does_not_stop_pipeline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, malformed: bool
) -> None:
    def read_status(_path: Path) -> str:
        if malformed:
            return "VmRSS:\tunknown kB\n"
        raise OSError("procfs unavailable")

    monkeypatch.setattr(Path, "read_text", read_status)
    with caplog.at_level(logging.INFO, logger="council_meetings.diagnostics"):
        diagnostics.log_memory("scrape.start")
    assert "rss_kib=None peak_rss_kib=None" in caplog.text
    assert "procfs unavailable" not in caplog.text


@pytest.mark.parametrize("failed", [False, True])
def test_pipeline_memory_stage_order(monkeypatch: pytest.MonkeyPatch, failed: bool) -> None:
    stages: list[str] = []
    monkeypatch.setattr(scheduler, "log_memory", stages.append)
    monkeypatch.setattr(
        scraper,
        "scrape_meetings",
        Mock(return_value=SimpleNamespace(meetings_found=0, new_documents=0, revised_documents=0)),
    )
    summarize = (
        Mock(side_effect=RuntimeError("offline failure")) if failed else Mock(return_value=1)
    )
    notify = Mock(return_value=0)
    monkeypatch.setattr(summarizer, "summarize_unsummarized", summarize)
    monkeypatch.setattr(notifier, "notify_new_summaries", notify)
    if failed:
        with pytest.raises(RuntimeError, match="offline failure"):
            scheduler.run_pipeline()
        assert stages == ["scrape.start", "scrape.complete", "summarize.start"]
        notify.assert_not_called()
    else:
        scheduler.run_pipeline()
        assert stages == [
            "scrape.start",
            "scrape.complete",
            "summarize.start",
            "summarize.complete",
            "notify.start",
            "notify.complete",
        ]


@pytest.mark.parametrize("failed", [False, True])
def test_document_memory_boundaries(monkeypatch: pytest.MonkeyPatch, failed: bool) -> None:
    document = SimpleNamespace(
        id=42, meeting_id=7, doc_type="agenda", pdf_path=None, text_content="source data"
    )
    db = Mock()
    db.query.return_value.filter.return_value.all.return_value = [document]
    monkeypatch.setattr(summarizer, "SessionLocal", lambda: db)
    monkeypatch.setattr(settings, "anthropic_api_key", "offline")
    text = (
        Mock(side_effect=RuntimeError("offline failure"))
        if failed
        else Mock(return_value="summary")
    )
    monkeypatch.setattr(summarizer, "summarize_text", text)
    memory = Mock()
    monkeypatch.setattr(summarizer, "log_memory", memory)
    assert summarizer.summarize_unsummarized() == (0 if failed else 1)
    assert memory.call_args_list[0].args == ("document.start", 42)
    assert memory.call_args_list[1].args == (
        "document.failed" if failed else "document.complete",
        42,
    )
    db.close.assert_called_once_with()
    if failed:
        db.rollback.assert_called_once_with()
    else:
        db.commit.assert_called_once_with()


@pytest.mark.asyncio
async def test_startup_enables_application_info_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from council_meetings import main

    monkeypatch.setattr(logging.getLogger("council_meetings"), "level", logging.WARNING)
    monkeypatch.setattr(main, "init_db", Mock())
    job = Mock()
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: job)
    async with main.lifespan(main.app):
        diagnostics.log_memory("startup.test")
    assert "Memory stage=startup.test" in caplog.text
    job.shutdown.assert_called_once_with(wait=False)
