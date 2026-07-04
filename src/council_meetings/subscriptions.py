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
    """Create or reactivate a subscriber.

    Returns the subscriber and one of the status strings ``"created"``,
    ``"reactivated"``, or ``"already_active"`` so callers can tailor the
    confirmation message.
    """
    email = normalize_email(email)
    existing = db.query(Subscriber).filter_by(email=email).first()
    if existing is not None:
        if existing.active:
            return existing, "already_active"
        existing.active = True
        existing.unsubscribed_at = None
        db.commit()
        return existing, "reactivated"

    subscriber = Subscriber(
        email=email,
        unsubscribe_token=secrets.token_urlsafe(32),
        active=True,
    )
    db.add(subscriber)
    db.commit()
    return subscriber, "created"


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
    return db.query(Subscriber).filter_by(active=True).order_by(Subscriber.id).all()
