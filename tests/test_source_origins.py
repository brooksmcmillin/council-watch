import httpx
import pytest

from council_meetings.config import city
from council_meetings.scraper import _client


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/private",
        "https://foreign.example/document",
        "//foreign.example/document",
        "https://www.campbellca.gov.foreign.example/document",
        "https://www.campbellca.gov:444/document",
        "http://www.campbellca.gov/document",
        "https://user:password@www.campbellca.gov/document",  # pragma: allowlist secret
    ],
)
def test_initial_foreign_origin_never_dispatches(target: str) -> None:
    requests = []
    with (
        _client(
            httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))
        ) as client,
        pytest.raises((httpx.RequestError, httpx.UnsupportedProtocol)),
    ):
        client.get(target)
    assert requests == []


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
@pytest.mark.parametrize("foreign", [False, True])
def test_production_client_checks_every_redirect(method: str, foreign: bool) -> None:
    requests = []
    destination = "https://foreign.example/document" if foreign else f"{city.escribe_url}/document"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(307, headers={"location": destination})
        assert request.url.host == httpx.URL(city.escribe_url).host
        return httpx.Response(200, content=b"%PDF-test")

    with _client(httpx.MockTransport(respond)) as client:
        if foreign:
            with pytest.raises(httpx.RequestError, match="outside configured"):
                client.request(method, f"{city.base_url}/start")
            assert len(requests) == 1
        else:
            assert client.request(method, f"{city.base_url}/start").status_code == 200
            assert len(requests) == 2


def test_disabled_portal_is_not_an_allowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(city, "escribe_base_url", "")
    requests = []
    with (
        _client(
            httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))
        ) as client,
        pytest.raises(httpx.RequestError),
    ):
        client.get("https://pub-campbell.escribemeetings.com/document")
    assert requests == []
