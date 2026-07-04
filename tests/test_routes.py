from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from council_meetings.db import get_db
from council_meetings.main import app
from council_meetings.models import Base, Subscriber


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


def test_subscribe_submit_creates_subscriber(client: TestClient) -> None:
    resp = client.post("/subscribe", data={"email": "new@example.com"})
    assert resp.status_code == 200
    assert "subscribed" in resp.text.lower()
    assert _subscriber(client, "new@example.com") is not None


def test_subscribe_rejects_invalid_email(client: TestClient) -> None:
    resp = client.post("/subscribe", data={"email": "not-an-email"})
    assert resp.status_code == 400
    assert "valid email" in resp.text.lower()


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
