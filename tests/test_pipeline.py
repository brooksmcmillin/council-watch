from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from council_meetings import notifier, scheduler, scraper, summarizer
from council_meetings.config import GatewayConfigurationError, Settings, city
from council_meetings.models import ScrapeLog


@pytest.mark.parametrize("failed", [False, True])
def test_scrape_result_readable_after_session_closes(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    monkeypatch.setattr(scraper, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(city, "escribe_base_url", "")

    def client() -> httpx.Client:
        if failed:
            raise RuntimeError("fixture scrape failure")
        return httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="")))

    monkeypatch.setattr(scraper, "_client", client)
    result = scraper.scrape_meetings()

    assert inspect(result).detached
    assert result.id is not None
    assert result.started_at is not None
    assert result.finished_at is not None
    assert (result.meetings_found, result.new_documents, result.revised_documents) == (0, 0, 0)
    assert result.errors == ("fixture scrape failure" if failed else None)
    assert db_session.query(ScrapeLog).count() == 1

    summarize = Mock(return_value=0)
    notify = Mock(return_value=0)
    monkeypatch.setattr(summarizer, "summarize_unsummarized", summarize)
    monkeypatch.setattr(notifier, "notify_new_summaries", notify)
    monkeypatch.setattr(scraper, "scrape_meetings", lambda **_: result)
    scheduler.run_pipeline()
    summarize.assert_called_once_with()
    notify.assert_called_once_with()


@pytest.mark.parametrize("mode", ["gateway", "", "gatway"])
def test_invalid_gateway_does_not_block_existing_notifications(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    monkeypatch.setenv("LLM_MODE", mode)
    monkeypatch.setenv("LLM_GATEWAY_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "direct-fixture")
    monkeypatch.setattr(summarizer, "settings", Settings(_env_file=None))
    monkeypatch.setattr(
        scraper,
        "scrape_meetings",
        Mock(return_value=ScrapeLog(meetings_found=0, new_documents=0, revised_documents=0)),
    )
    notify = Mock(return_value=1)
    client = Mock()
    monkeypatch.setattr(notifier, "notify_new_summaries", notify)
    monkeypatch.setattr(summarizer.anthropic, "Anthropic", client)

    scheduler.run_pipeline()

    assert "Gateway summarization skipped" in caplog.text
    notify.assert_called_once_with()
    with pytest.raises(GatewayConfigurationError):
        summarizer.summarize_unsummarized()
    client.assert_not_called()
