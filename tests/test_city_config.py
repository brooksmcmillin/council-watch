"""Tests for per-city configuration threaded through the pipeline modules."""

import pytest

from council_meetings import notifier, scraper, summarizer
from council_meetings.config import CityConfig, city


def test_defaults_describe_campbell() -> None:
    cfg = CityConfig()
    assert cfg.name == "Campbell"
    assert cfg.location == "Campbell, California"
    assert cfg.base_url == "https://www.campbellca.gov"
    assert cfg.category_id == "10"


def test_derived_urls_compose_from_base_url_and_path() -> None:
    cfg = CityConfig(base_url="https://example.gov", agenda_path="City-Council-7")
    assert cfg.agenda_center_url == "https://example.gov/AgendaCenter/City-Council-7"
    assert cfg.backfill_url == "https://example.gov/AgendaCenter/UpdateCategoryList"


def test_city_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITY_NAME", "Springfield")
    monkeypatch.setenv("CITY_BASE_URL", "https://springfield.example.gov")
    monkeypatch.setenv("CITY_CATEGORY_ID", "42")
    cfg = CityConfig()
    assert cfg.name == "Springfield"
    assert cfg.base_url == "https://springfield.example.gov"
    assert cfg.category_id == "42"


def test_summarizer_prompt_uses_configured_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(city, "location", "Springfield, Illinois")
    for doc_type in ("agenda", "minutes"):
        prompt = summarizer._prompt_for(doc_type)
        assert "Springfield, Illinois" in prompt
        assert "Campbell" not in prompt


def test_fetch_year_posts_configured_category_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(city, "base_url", "https://springfield.example.gov")
    monkeypatch.setattr(city, "category_id", "42")

    captured: dict[str, object] = {}

    class _Resp:
        text = "<html></html>"

        def raise_for_status(self) -> None:
            pass

    class _Client:
        def post(self, url: str, *, data: dict[str, str], headers: dict[str, str]) -> _Resp:
            captured["url"] = url
            captured["data"] = data
            return _Resp()

    scraper.fetch_year(_Client(), 2024)  # type: ignore[arg-type]

    assert captured["url"] == "https://springfield.example.gov/AgendaCenter/UpdateCategoryList"
    assert captured["data"] == {"year": "2024", "catID": "42"}


def test_email_subject_uses_configured_city_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date
    from types import SimpleNamespace

    monkeypatch.setattr(city, "name", "Springfield")

    meeting = SimpleNamespace(title="Regular Meeting", date=date(2025, 1, 2))
    doc = SimpleNamespace(doc_type="agenda", revised_at=None)

    label = notifier._doc_label(doc)  # type: ignore[arg-type]
    subject = (
        f"{city.name} Council: {meeting.title} — {label} ({meeting.date.strftime('%m/%d/%Y')})"
    )
    assert subject.startswith("Springfield Council:")
