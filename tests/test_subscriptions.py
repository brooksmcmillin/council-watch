from sqlalchemy.orm import Session

from council_meetings import subscriptions
from council_meetings.models import Subscriber


def test_is_valid_email() -> None:
    assert subscriptions.is_valid_email("a@b.com")
    assert not subscriptions.is_valid_email("nope")
    assert not subscriptions.is_valid_email("a@b")
    assert not subscriptions.is_valid_email("a b@c.com")


def test_subscribe_creates_active_subscriber(db_session: Session) -> None:
    sub, status = subscriptions.subscribe(db_session, "Person@Example.com")
    assert status == "created"
    assert sub.email == "person@example.com"  # normalized
    assert sub.active is True
    assert sub.unsubscribe_token  # populated


def test_subscribe_twice_is_idempotent(db_session: Session) -> None:
    first, _ = subscriptions.subscribe(db_session, "dup@example.com")
    second, status = subscriptions.subscribe(db_session, "dup@example.com")
    assert status == "already_active"
    assert second.id == first.id
    assert db_session.query(Subscriber).count() == 1


def test_unsubscribe_then_resubscribe_reactivates(db_session: Session) -> None:
    sub, _ = subscriptions.subscribe(db_session, "cycle@example.com")
    token = sub.unsubscribe_token

    removed = subscriptions.unsubscribe(db_session, token)
    assert removed is not None
    assert removed.active is False
    assert removed.unsubscribed_at is not None

    again, status = subscriptions.subscribe(db_session, "cycle@example.com")
    assert status == "reactivated"
    assert again.active is True
    assert again.unsubscribed_at is None
    assert again.unsubscribe_token == token  # token preserved across cycle


def test_unsubscribe_unknown_token_returns_none(db_session: Session) -> None:
    assert subscriptions.unsubscribe(db_session, "does-not-exist") is None


def test_active_subscribers_excludes_inactive(db_session: Session) -> None:
    a, _ = subscriptions.subscribe(db_session, "a@example.com")
    subscriptions.subscribe(db_session, "b@example.com")
    subscriptions.unsubscribe(db_session, a.unsubscribe_token)

    active = subscriptions.active_subscribers(db_session)
    assert [s.email for s in active] == ["b@example.com"]
