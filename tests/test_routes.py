from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from council_meetings import main as main_module
from council_meetings.db import get_db
from council_meetings.main import app
from council_meetings.models import Base, Subscriber


@pytest.fixture(autouse=True)
def _confirmation_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "send_confirmation_email", lambda _address, _token: True)


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


def test_confirm_route_activates_subscription(client: TestClient) -> None:
    client.post("/subscribe", data={"email": "confirm@example.com"})
    token = _subscriber(client, "confirm@example.com").confirmation_token

    resp = client.get(f"/confirm/{token}")

    assert resp.status_code == 200
    assert "confirmed" in resp.text.lower()
    assert _subscriber(client, "confirm@example.com").confirmed is True

    duplicate = client.post("/subscribe", data={"email": "confirm@example.com"})
    assert "already subscribed" in duplicate.text.lower()


def test_confirm_route_unknown_token_404(client: TestClient) -> None:
    resp = client.get("/confirm/bogus-token")
    assert resp.status_code == 404


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
