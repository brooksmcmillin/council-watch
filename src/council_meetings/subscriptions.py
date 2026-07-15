"""Email subscription management: signup, unsubscribe, recipient lookup."""

import datetime as dt
import re
import secrets

from sqlalchemy.orm import Session

from council_meetings.models import Subscriber

# Deliberately permissive — we only guard against obvious garbage, not RFC 5322
# edge cases. Real validation happens when SMTP accepts (or rejects) the address.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def subscribe(db: Session, email: str) -> tuple[Subscriber, str]:
    """Create an unconfirmed subscriber or start confirmation again.

    Returns the subscriber and one of the status strings ``"created"``,
    ``"reactivated"``, ``"pending_confirmation"``, or ``"already_confirmed"``.
    Only created/reactivated rows need a confirmation email; returning a
    separate pending status prevents repeated form submissions from becoming
    a confirmation-email mailbomb.
    """
    email = normalize_email(email)
    existing = db.query(Subscriber).filter_by(email=email).first()
    if existing is not None:
        if existing.active:
            status = "already_confirmed" if existing.confirmed else "pending_confirmation"
            return existing, status
        existing.active = True
        existing.confirmed = False
        existing.confirmation_token = secrets.token_urlsafe(32)
        existing.unsubscribed_at = None
        db.commit()
        return existing, "reactivated"

    subscriber = Subscriber(
        email=email,
        unsubscribe_token=secrets.token_urlsafe(32),
        confirmation_token=secrets.token_urlsafe(32),
        confirmed=False,
        active=True,
    )
    db.add(subscriber)
    db.commit()
    return subscriber, "created"


def confirm(db: Session, token: str) -> Subscriber | None:
    """Confirm the subscriber owning ``token`` and return it.

    Confirmation is idempotent. An unknown or rotated token returns ``None``.
    """
    subscriber = db.query(Subscriber).filter_by(confirmation_token=token, active=True).first()
    if subscriber is None:
        return None
    if not subscriber.confirmed:
        subscriber.confirmed = True
        db.commit()
    return subscriber


def cancel_pending_confirmation(db: Session, subscriber: Subscriber) -> None:
    """Deactivate a pending attempt whose confirmation message was not sent."""
    if subscriber.active and not subscriber.confirmed:
        subscriber.active = False
        subscriber.unsubscribed_at = dt.datetime.now(dt.UTC)
        db.commit()


def find_by_token(db: Session, token: str) -> Subscriber | None:
    """Look up a subscriber by unsubscribe token without mutating state."""
    return db.query(Subscriber).filter_by(unsubscribe_token=token).first()


def unsubscribe(db: Session, token: str) -> Subscriber | None:
    """Deactivate the subscriber owning ``token``.

    Idempotent: unsubscribing an already-inactive subscriber succeeds and
    returns the row. Returns ``None`` when no subscriber matches the token.
    """
    subscriber = db.query(Subscriber).filter_by(unsubscribe_token=token).first()
    if subscriber is None:
        return None
    if subscriber.active:
        subscriber.active = False
        subscriber.unsubscribed_at = dt.datetime.now(dt.UTC)
        db.commit()
    return subscriber


def active_subscribers(db: Session) -> list[Subscriber]:
    return db.query(Subscriber).filter_by(active=True, confirmed=True).order_by(Subscriber.id).all()
