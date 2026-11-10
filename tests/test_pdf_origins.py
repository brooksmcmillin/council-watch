"""Exercise the production PDF helpers and request hook without network access."""

import hashlib
from pathlib import Path

import httpx
import pytest

from council_meetings import scraper
from council_meetings.config import CityConfig

PDF = b"%PDF-1.4 fixture agenda/minutes"
AGENDA = "/AgendaCenter/ViewFile/Agenda/_09162025-3145"
MINUTES = "/AgendaCenter/ViewFile/Minutes/_09162025-3145"  # pragma: allowlist secret
BASE = "https://www.campbellca.gov"
PORTAL = "https://pub-campbell.escribemeetings.com"


@pytest.fixture(autouse=True)
def _city(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scraper, "city", CityConfig(base_url=BASE, escribe_base_url=PORTAL))


def _fetch(client: httpx.Client, method: str, target: str, destination: Path) -> None:
    if method == "HEAD":
        assert scraper.head_content_length(client, target) == len(PDF)
    else:
        assert scraper.download_pdf(client, target, destination) == (
            hashlib.sha256(PDF).hexdigest(),
            len(PDF),
        )
        assert destination.read_bytes() == PDF


@pytest.mark.parametrize("method", ["HEAD", "GET"])
@pytest.mark.parametrize("redirect_status", [None, 301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "target",
    [
        "https://www.campbellca.gov@169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/private",
        "https://[::1]/private",
        "https://internal.example/private",
        "//169.254.169.254/private",
        "http://www.campbellca.gov/document",
        "ftp://www.campbellca.gov/document",
        "https://user@www.campbellca.gov/document",
        "https://www.campbellca.gov.evil.example/document",
        "https://www.campbellca.gov:444/document",
    ],
)
def test_pdf_hostile_destination_never_dispatches(
    method: str, redirect_status: int | None, target: str, tmp_path: Path
) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert redirect_status is not None
        assert str(request.url) == BASE + AGENDA
        return httpx.Response(redirect_status, headers={"location": target})

    destination = tmp_path / "document.pdf"
    destination.write_bytes(b"previous PDF")
    with scraper._client(httpx.MockTransport(respond)) as client:
        source = target if redirect_status is None else AGENDA
        if method == "HEAD":
            assert scraper.head_content_length(client, source) is None
        else:
            with pytest.raises(httpx.RequestError):
                scraper.download_pdf(client, source, destination)

    assert requests == ([] if redirect_status is None else [BASE + AGENDA])
    assert destination.read_bytes() == b"previous PDF"
    assert not destination.with_suffix(".pdf.part").exists()


@pytest.mark.parametrize("method", ["HEAD", "GET"])
@pytest.mark.parametrize("redirect", [False, True])
def test_observed_civicplus_links_remain_downloadable(
    method: str, redirect: bool, tmp_path: Path
) -> None:
    html = (Path(__file__).parent / "fixtures/civicplus-pdf-links.html").read_text()
    meeting = scraper.parse_meetings_html(html)[0]
    assert meeting["agenda_url"] == AGENDA
    assert meeting["minutes_url"] == MINUTES
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if redirect and request.url.path == "/download":
            # Model a relative Location using a real published PDF path.
            return httpx.Response(302, headers={"location": MINUTES})
        assert request.url.path in {AGENDA, MINUTES}
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    with scraper._client(httpx.MockTransport(respond)) as client:
        for link in (meeting["agenda_url"], meeting["minutes_url"], BASE + MINUTES):
            _fetch(client, method, link, tmp_path / "document.pdf")
        if redirect:
            _fetch(client, method, "/download", tmp_path / "document.pdf")
    assert requests == [BASE + AGENDA, BASE + MINUTES, BASE + MINUTES] + (
        [BASE + "/download", BASE + MINUTES] if redirect else []
    )


@pytest.mark.parametrize("method", ["HEAD", "GET"])
def test_pdf_redirect_rechecks_every_hop_and_allows_configured_portal(
    method: str, tmp_path: Path
) -> None:
    portal_pdf = PORTAL + "/FileStream.ashx?DocumentId=2377"
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(307, headers={"location": portal_pdf})
        if len(requests) == 2:
            return httpx.Response(302, headers={"location": "?DocumentId=2373"})
        return httpx.Response(200, content=PDF)

    with scraper._client(httpx.MockTransport(respond)) as client:
        _fetch(client, method, AGENDA, tmp_path / "document.pdf")
    assert requests == [BASE + AGENDA, portal_pdf, PORTAL + "/FileStream.ashx?DocumentId=2373"]


@pytest.mark.parametrize("method", ["HEAD", "GET"])
@pytest.mark.parametrize(
    "target",
    [
        "https://www.campbellca.gov@169.254.169.254/latest/meta-data/",
        "https://internal.example/private",
        "http://pub-campbell.escribemeetings.com/document",
    ],
)
def test_pdf_hostile_destination_after_allowed_portal_never_dispatches(
    method: str, target: str, tmp_path: Path
) -> None:
    portal_pdf = PORTAL + "/FileStream.ashx?DocumentId=2377"
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert len(requests) <= 2
        return httpx.Response(
            302, headers={"location": portal_pdf if len(requests) == 1 else target}
        )

    destination = tmp_path / "document.pdf"
    destination.write_bytes(b"previous PDF")
    with scraper._client(httpx.MockTransport(respond)) as client:
        if method == "HEAD":
            assert scraper.head_content_length(client, AGENDA) is None
        else:
            with pytest.raises(httpx.RequestError):
                scraper.download_pdf(client, AGENDA, destination)

    assert requests == [BASE + AGENDA, portal_pdf]
    assert destination.read_bytes() == b"previous PDF"
    assert not destination.with_suffix(".pdf.part").exists()


@pytest.mark.parametrize("method", ["HEAD", "GET"])
def test_http_source_config_does_not_admit_plaintext_pdf(
    method: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scraper.city, "base_url", "http://www.campbellca.gov")
    requests: list[httpx.Request] = []
    with scraper._client(
        httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))
    ) as client:
        if method == "HEAD":
            assert scraper.head_content_length(client, AGENDA) is None
        else:
            with pytest.raises(httpx.RequestError):
                scraper.download_pdf(client, AGENDA, tmp_path / "document.pdf")
    assert requests == []
