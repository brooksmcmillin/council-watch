import datetime as dt
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from council_meetings import main as main_module
from council_meetings import scraper, subscriptions
from council_meetings.db import get_db
from council_meetings.escribe import parse_meeting
from council_meetings.main import app
from council_meetings.models import Base, Document, Meeting, Subscriber
from council_meetings.notifier import _build_email_html


@pytest.fixture(autouse=True)
def _confirmation_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "send_confirmation_email", lambda _address, _token: True)
    main_module.subscribe_limiter.reset()
    main_module.confirm_limiter.reset()


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def _override_get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    # Not entering the TestClient context manager, so the app lifespan
    # (which would start the scheduler) does not run.
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def meeting_id(client: TestClient) -> int:
    for db in app.dependency_overrides[get_db]():
        meeting = Meeting(
            date=dt.date(2026, 11, 8),
            title="Regular Council Meeting",
            civicplus_id="1234",
            url_date_slug="_11082026-1234",
        )
        db.add(meeting)
        db.commit()
        return meeting.id
    raise AssertionError("Database override did not yield a session")


@pytest.mark.parametrize("doc_type", ["agenda", "minutes"])
def test_meeting_summary_is_plain_text(client: TestClient, meeting_id: int, doc_type: str) -> None:
    summary = (
        "Budget & parks\n\n"
        '<script>alert("script")</script>\n'
        '<img src=x onerror="alert(1)">\n'
        '<svg onload="alert(2)"></svg>\n'
        "Next steps"
    )
    for db in app.dependency_overrides[get_db]():
        db.add(
            Document(
                meeting_id=meeting_id, doc_type=doc_type, source_url="/doc.pdf", summary=summary
            )
        )
        db.commit()

    response = client.get(f"/meeting/{meeting_id}")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    content = soup.select_one(".summary-content")
    assert content is not None
    assert content.find(True) is None
    assert content.get_text() == summary
    assert f"{doc_type.title()} Summary" in soup.get_text()
    stylesheet = client.get("/static/style.css")
    assert stylesheet.status_code == 200
    assert (
        "white-space: pre-line;"
        in stylesheet.text.split(".summary-content {", 1)[1].split("}", 1)[0]
    )


def test_meeting_detail_without_summaries(client: TestClient, meeting_id: int) -> None:
    response = client.get(f"/meeting/{meeting_id}")
    assert response.status_code == 200
    assert "Regular Council Meeting" in response.text
    assert "No summaries available yet" in response.text


def test_homepage_lists_meeting(client: TestClient, meeting_id: int) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Regular Council Meeting" in response.text
    assert f'href="/meeting/{meeting_id}"' in response.text


def test_unknown_meeting_returns_404(client: TestClient) -> None:
    assert client.get("/meeting/9999").status_code == 404


def _subscriber(client: TestClient, email: str) -> Subscriber:
    db = next(iter(app.dependency_overrides[get_db]()))
    return db.query(Subscriber).filter_by(email=email).first()


def test_subscribe_form_renders(client: TestClient) -> None:
    resp = client.get("/subscribe")
    assert resp.status_code == 200
    assert "Subscribe" in resp.text


def test_subscribe_submit_sends_one_confirmation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_module,
        "send_confirmation_email",
        lambda address, token: sent.append((address, token)) or True,
    )

    resp = client.post("/subscribe", data={"email": "new@example.com"})
    duplicate = client.post("/subscribe", data={"email": "new@example.com"})

    assert resp.status_code == 200
    assert "check your email" in resp.text.lower()
    assert duplicate.status_code == 200
    assert "already been sent" in duplicate.text.lower()
    subscriber = _subscriber(client, "new@example.com")
    assert subscriber.confirmed is False
    assert sent == [(subscriber.email, subscriber.confirmation_token)]


def test_subscribe_rejects_invalid_email(client: TestClient) -> None:
    resp = client.post("/subscribe", data={"email": "not-an-email"})
    assert resp.status_code == 400
    assert "valid email" in resp.text.lower()


def test_subscribe_burst_from_one_ip_is_throttled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"CF-Connecting-IP": "203.0.113.10"}
    sent: list[str] = []
    monkeypatch.setattr(
        main_module,
        "send_confirmation_email",
        lambda address, _token: sent.append(address) or True,
    )

    for request_number in range(main_module.subscribe_limiter.limit):
        response = client.post(
            "/subscribe",
            data={"email": f"burst-{request_number}@example.com"},
            headers=headers,
        )
        assert response.status_code == 200

    throttled = client.post(
        "/subscribe",
        data={"email": "one-too-many@example.com"},
        headers=headers,
    )

    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "3600"
    assert "too many requests" in throttled.text.lower()
    assert _subscriber(client, "one-too-many@example.com") is None
    assert len(sent) == main_module.subscribe_limiter.limit


def test_subscribe_rate_limit_is_per_ip(client: TestClient) -> None:
    for request_number in range(main_module.subscribe_limiter.limit):
        client.post(
            "/subscribe",
            data={"email": f"first-ip-{request_number}@example.com"},
            headers={"CF-Connecting-IP": "203.0.113.20"},
        )

    response = client.post(
        "/subscribe",
        data={"email": "second-ip@example.com"},
        headers={"CF-Connecting-IP": "203.0.113.21"},
    )

    assert response.status_code == 200


def test_failed_confirmation_send_can_be_retried(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "send_confirmation_email", lambda _address, _token: False)

    failed = client.post("/subscribe", data={"email": "retry@example.com"})
    first_token = _subscriber(client, "retry@example.com").confirmation_token

    assert failed.status_code == 503
    assert _subscriber(client, "retry@example.com").active is False

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_module,
        "send_confirmation_email",
        lambda address, token: sent.append((address, token)) or True,
    )
    retried = client.post("/subscribe", data={"email": "retry@example.com"})
    subscriber = _subscriber(client, "retry@example.com")

    assert retried.status_code == 200
    assert subscriber.active is True
    assert subscriber.confirmation_token != first_token
    assert sent == [(subscriber.email, subscriber.confirmation_token)]


def test_confirm_requires_form_submission(client: TestClient) -> None:
    client.post("/subscribe", data={"email": "confirm@example.com"})
    token = _subscriber(client, "confirm@example.com").confirmation_token
    path = f"/confirm/{token}"

    response = client.get(path)
    assert response.status_code == 200
    assert client.get(path).status_code == 200
    assert _subscriber(client, "confirm@example.com").confirmed is False
    # HEAD is either unsupported or read-only; it must never opt in a mailbox.
    assert client.head(path).status_code in {200, 405}
    assert _subscriber(client, "confirm@example.com").confirmed is False
    for db in app.dependency_overrides[get_db]():
        assert subscriptions.active_subscribers(db) == []

    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.select_one("form.subscribe-form")
    assert form is not None
    assert form.get("method") == "post"
    assert form.get("action") == path
    assert form.select_one('button[type="submit"]') is not None
    assert "confirm@example.com" in soup.get_text()
    confirmed = client.post(str(form["action"]))
    assert confirmed.status_code == 200
    assert "subscription is confirmed" in confirmed.text.lower()
    assert _subscriber(client, "confirm@example.com").confirmed is True
    for db in app.dependency_overrides[get_db]():
        assert [sub.email for sub in subscriptions.active_subscribers(db)] == [
            "confirm@example.com"
        ]

    again = client.post(path)
    assert again.status_code == 200
    assert "subscription is confirmed" in again.text.lower()
    revisited = client.get(path)
    assert "already confirmed" in revisited.text.lower()
    assert BeautifulSoup(revisited.text, "html.parser").select_one("form.subscribe-form") is None
    duplicate = client.post("/subscribe", data={"email": "confirm@example.com"})
    assert "already subscribed" in duplicate.text.lower()


@pytest.mark.parametrize("method", ["get", "post"])
def test_confirm_route_unknown_token_404(client: TestClient, method: str) -> None:
    resp = client.request(method, "/confirm/bogus-token")
    assert resp.status_code == 404
    assert "not valid" in resp.text.lower()


def test_confirm_rejects_inactive_and_rotated_tokens(client: TestClient) -> None:
    client.post("/subscribe", data={"email": "cycle@example.com"})
    subscriber = _subscriber(client, "cycle@example.com")
    path = f"/confirm/{subscriber.confirmation_token}"
    client.get(path)  # A form opened before unsubscribing must not remain valid.
    client.post(f"/unsubscribe/{subscriber.unsubscribe_token}")
    for method in ["get", "post"]:
        assert client.request(method, path).status_code == 404
    inactive = _subscriber(client, "cycle@example.com")
    assert inactive.active is False
    assert inactive.confirmed is False

    client.post("/subscribe", data={"email": "cycle@example.com"})
    subscriber = _subscriber(client, "cycle@example.com")
    assert f"/confirm/{subscriber.confirmation_token}" != path
    for method in ["get", "post"]:
        assert client.request(method, path).status_code == 404
    assert _subscriber(client, "cycle@example.com").confirmed is False
    assert client.get(f"/confirm/{subscriber.confirmation_token}").status_code == 200
    assert client.post(f"/confirm/{subscriber.confirmation_token}").status_code == 200
    assert _subscriber(client, "cycle@example.com").confirmed is True


@pytest.mark.parametrize("method", ["get", "post"])
def test_confirm_burst_from_one_ip_is_throttled(client: TestClient, method: str) -> None:
    headers = {"CF-Connecting-IP": "2001:db8::1"}
    client.post("/subscribe", data={"email": "limited@example.com"})
    token = _subscriber(client, "limited@example.com").confirmation_token
    path = f"/confirm/{token}"

    for request_number in range(main_module.confirm_limiter.limit):
        # GET and POST draw on the same existing allowance.
        request_method = "get" if request_number % 2 == 0 else "post"
        response = client.request(request_method, "/confirm/bogus-token", headers=headers)
        assert response.status_code == 404

    throttled = client.request(method, path, headers=headers)
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "60"
    assert _subscriber(client, "limited@example.com").confirmed is False
    other_ip = client.post(path, headers={"CF-Connecting-IP": "2001:db8::2"})
    assert other_ip.status_code == 200
    assert _subscriber(client, "limited@example.com").confirmed is True


def test_unsubscribe_get_is_read_only(client: TestClient) -> None:
    # GET renders a confirmation page; it must NOT unsubscribe (mail scanners
    # prefetch this link). The subscriber stays active until an explicit POST.
    client.post("/subscribe", data={"email": "bye@example.com"})
    token = _subscriber(client, "bye@example.com").unsubscribe_token

    resp = client.get(f"/unsubscribe/{token}")
    assert resp.status_code == 200
    assert "confirm" in resp.text.lower()
    assert _subscriber(client, "bye@example.com").active is True


def test_unsubscribe_get_unknown_token_404(client: TestClient) -> None:
    resp = client.get("/unsubscribe/bogus-token")
    assert resp.status_code == 404


def test_unsubscribe_post_deactivates(client: TestClient) -> None:
    client.post("/subscribe", data={"email": "click@example.com"})
    token = _subscriber(client, "click@example.com").unsubscribe_token

    resp = client.post(f"/unsubscribe/{token}")
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()
    assert _subscriber(client, "click@example.com").active is False


def test_unsubscribe_post_unknown_token_404(client: TestClient) -> None:
    resp = client.post("/unsubscribe/bogus-token")
    assert resp.status_code == 404


@pytest.mark.parametrize("doc_type", ["agenda", "minutes"])
def test_html_source_links_and_limitations(
    client: TestClient, meeting_id: int, doc_type: str
) -> None:
    source = "https://pub-campbell.escribemeetings.com/Meeting.aspx?Id=fixture&Agenda=Agenda"
    for db in app.dependency_overrides[get_db]():
        db.add(
            Document(
                meeting_id=meeting_id,
                doc_type=doc_type,
                source_url=source,
                text_content="Public HTML",
                summary="Plain <b>summary</b>",
            )
        )
        db.commit()
    for path in ["/", f"/meeting/{meeting_id}"]:
        response = client.get(path)
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        links = soup.select(".meeting-links a")
        assert any(link.get("href") == source for link in links)
        assert "campbellca.govhttps" not in response.text
        assert "PDF" not in " ".join(link.get_text() for link in links)
        if path != "/" or doc_type == "agenda":
            assert "linked attachments and staff reports are not included" in soup.get_text()
        assert soup.select_one(".meeting-card b, .meeting-detail b") is None


@pytest.mark.parametrize(
    "unsafe",
    [
        "javascript:alert(1)",
        " JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,<svg/onload=alert(1)>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "https://[",
    ],
)
def test_calendar_and_saved_source_links_are_inert(client: TestClient, unsafe: str) -> None:
    record = {
        "MeetingType": "City Council Regular Meeting",
        "StartDate": "2026/11/08 19:00:00",
        "ID": "fdd357dd-6794-4d51-8709-ff788aeb167e",
        "MeetingDocumentLink": [
            {"Type": kind, "Format": fmt, "Url": unsafe}
            for kind, fmt in [
                ("Agenda", ".pdf"),
                ("Agenda", "html"),
                ("PostMinutes", ".pdf"),
                ("Video", "video"),
            ]
        ],
    }
    data = parse_meeting(record, "https://pub-campbell.escribemeetings.com")
    assert data is not None
    assert all(
        data[key] is None for key in ("agenda_url", "agenda_html_url", "minutes_url", "video_url")
    )
    public_id = None
    for db in app.dependency_overrides[get_db]():
        meeting, _ = scraper.upsert_meeting(db, data)
        db.commit()
        public_id = meeting.id
    assert public_id is not None
    # Calendar producer rejects unsafe links without dropping the meeting.
    for path in ["/", f"/meeting/{public_id}"]:
        response = client.get(path)
        assert response.status_code == 200 and "City Council Regular Meeting" in response.text
        soup = BeautifulSoup(response.text, "html.parser")
        assert all(
            urlsplit(str(link.get("href", ""))).scheme in {"", "http", "https"}
            for link in soup.select("a[href]")
        )
    # Consumer protection also covers unsafe data already saved by an older scraper.
    for db in app.dependency_overrides[get_db]():
        meeting = db.get(Meeting, public_id)
        assert meeting is not None
        meeting.agenda_url = meeting.minutes_url = meeting.video_url = unsafe
        doc = Document(
            meeting_id=public_id, doc_type="agenda", source_url=unsafe, summary="Saved summary"
        )
        db.add(doc)
        db.commit()
        email = BeautifulSoup(_build_email_html(meeting, doc, None), "html.parser")
        assert all(
            urlsplit(str(link.get("href", ""))).scheme in {"", "http", "https"}
            for link in email.select("a[href]")
        )
    for path in ["/", f"/meeting/{public_id}"]:
        response = client.get(path)
        soup = BeautifulSoup(response.text, "html.parser")
        assert "Saved summary" in soup.get_text()
        assert all(
            urlsplit(str(link.get("href", ""))).scheme in {"", "http", "https"}
            for link in soup.select("a[href]")
        )


def test_legacy_source_links_remain_absolute(client: TestClient, meeting_id: int) -> None:
    for db in app.dependency_overrides[get_db]():
        meeting = db.get(Meeting, meeting_id)
        assert meeting is not None
        meeting.agenda_url = "/AgendaCenter/ViewFile/Agenda/_10072025-3159"
        db.commit()
    response = client.get(f"/meeting/{meeting_id}")
    assert (
        'href="https://www.campbellca.gov/AgendaCenter/ViewFile/Agenda/_10072025-3159"'
        in response.text
    )
