"""Tests for per-city configuration threaded through the pipeline modules."""

import datetime as dt
from email import message_from_string
from email.header import decode_header, make_header

import pytest
from sqlalchemy.orm import Session

from council_meetings import notifier, scraper, subscriptions, summarizer
from council_meetings.config import CityConfig, city, settings
from council_meetings.models import Document, Meeting


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


def test_ignores_non_city_keys_in_shared_env_file(tmp_path) -> None:
    """The shared .env holds Settings keys (ANTHROPIC_API_KEY, SMTP_*, ...);
    CityConfig reads the same file and must ignore them rather than raise on
    extras (regression: previously crashed app startup with a .env present)."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=sk-test\n"
        "SMTP_PORT=587\n"
        "DATABASE_URL=sqlite:///data/council.db\n"
        "CITY_NAME=Springfield\n"
    )
    cfg = CityConfig(_env_file=str(env_file))  # type: ignore[call-arg]
    assert cfg.name == "Springfield"


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


class _FakeSMTP:
    """Records sendmail calls; usable as a context manager like smtplib.SMTP."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int) -> None:
        self.sent: list[tuple[str, list[str], str]] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: str) -> None:
        self.sent.append((from_addr, to_addrs, msg))


def test_email_subject_uses_configured_city_name(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Subject header sent by send_email reflects the configured city."""
    monkeypatch.setattr(city, "name", "Springfield")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "email_from", "bot@example.com")
    monkeypatch.setattr(settings, "email_to", "admin@example.com")
    monkeypatch.setattr(settings, "app_base_url", "https://site.example")
    _FakeSMTP.instances = []
    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)

    meeting = Meeting(
        date=dt.date(2025, 9, 16),
        title="Regular Meeting",
        civicplus_id="3145",
        url_date_slug="_09162025-3145",
    )
    db_session.add(meeting)
    db_session.flush()
    doc = Document(
        meeting_id=meeting.id,
        doc_type="agenda",
        source_url="/AgendaCenter/ViewFile/Agenda/_09162025-3145",
        summary="A summary of the meeting.",
    )
    db_session.add(doc)
    db_session.flush()
    subscriptions.subscribe(db_session, "sub@example.com")

    assert notifier.send_email(meeting, doc, db_session) is True

    _, _, body = _FakeSMTP.instances[-1].sent[0]
    # The Subject is MIME encoded-word wrapped (contains an em-dash), so decode
    # it before asserting rather than substring-matching the raw header.
    msg = message_from_string(body)
    subject = str(make_header(decode_header(msg["Subject"])))
    assert subject.startswith("Springfield Council: Regular Meeting")
