"""FastAPI application — routes and startup."""

import logging
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from council_meetings import subscriptions
from council_meetings.config import city
from council_meetings.db import get_db, init_db
from council_meetings.models import Document, Meeting
from council_meetings.notifier import send_confirmation_email
from council_meetings.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Import here to avoid circular imports
    from council_meetings.scheduler import start_scheduler

    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=f"{city.display_name} Meetings", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Expose the configured city to every template (including those rendered without
# explicit context, e.g. about/subscribe/404) so branding, links, and the
# affiliation disclaimer follow CITY_* config instead of being hard-coded.
templates.env.globals["city"] = city

BASE_URL = city.base_url

# A signup can send email, so keep its allowance deliberately small. Confirmation
# only performs a token lookup and needs a larger allowance for mail scanners and
# shared networks. These counters are process-local by design: production runs one
# replica, and losing the counters during a deploy is an acceptable tradeoff here.
subscribe_limiter = RateLimiter(limit=5, window_seconds=60 * 60)
confirm_limiter = RateLimiter(limit=30, window_seconds=60)


def _client_ip(request: Request) -> str:
    """Return the normalized visitor IP supplied by the trusted Cloudflare edge."""
    # Production is proxied by Cloudflare and Traefik, so request.client is the
    # ingress pod. Cloudflare overwrites CF-Connecting-IP at the edge; local/direct
    # deployments fall back to the socket peer.
    candidate = request.headers.get("cf-connecting-ip")
    if candidate is None and request.client is not None:
        candidate = request.client.host
    if candidate is None:
        return "unknown"
    try:
        return ip_address(candidate.strip()).compressed
    except ValueError:
        return request.client.host if request.client is not None else "unknown"


def _rate_limit_error(request: Request, retry_after: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "subscribe.html",
        {"error": "Too many requests. Please try again later."},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    meetings = db.query(Meeting).order_by(Meeting.date.desc(), Meeting.id.desc()).all()

    # Attach documents to each meeting for template access
    meeting_data = []
    for m in meetings:
        agenda_doc = db.query(Document).filter_by(meeting_id=m.id, doc_type="agenda").first()
        minutes_doc = db.query(Document).filter_by(meeting_id=m.id, doc_type="minutes").first()
        meeting_data.append(
            {
                "meeting": m,
                "agenda": agenda_doc,
                "minutes": minutes_doc,
            }
        )

    return templates.TemplateResponse(
        request,
        "index.html",
        {"meeting_data": meeting_data, "base_url": BASE_URL},
    )


@app.get("/meeting/{meeting_id}", response_class=HTMLResponse)
def meeting_detail(meeting_id: int, request: Request, db: Session = Depends(get_db)):
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    if not meeting:
        return templates.TemplateResponse(request, "404.html", status_code=404)

    agenda_doc = db.query(Document).filter_by(meeting_id=meeting.id, doc_type="agenda").first()
    minutes_doc = db.query(Document).filter_by(meeting_id=meeting.id, doc_type="minutes").first()

    return templates.TemplateResponse(
        request,
        "meeting.html",
        {
            "meeting": meeting,
            "agenda": agenda_doc,
            "minutes": minutes_doc,
            "base_url": BASE_URL,
        },
    )


@app.get("/subscribe", response_class=HTMLResponse)
def subscribe_form(request: Request):
    return templates.TemplateResponse(request, "subscribe.html")


@app.post("/subscribe", response_class=HTMLResponse)
def subscribe_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    allowed, retry_after = subscribe_limiter.check(_client_ip(request))
    if not allowed:
        return _rate_limit_error(request, retry_after)

    email = email.strip()
    if not subscriptions.is_valid_email(email):
        return templates.TemplateResponse(
            request,
            "subscribe.html",
            {"error": "Please enter a valid email address.", "email": email},
            status_code=400,
        )

    subscriber, status = subscriptions.subscribe(db, email)
    if status in {"created", "reactivated"} and not send_confirmation_email(
        subscriber.email, subscriber.confirmation_token
    ):
        subscriptions.cancel_pending_confirmation(db, subscriber)
        return templates.TemplateResponse(
            request,
            "subscribe.html",
            {"error": "We couldn't send a confirmation email. Please try again later."},
            status_code=503,
        )

    if status == "pending_confirmation":
        message = "A confirmation email has already been sent. Check your inbox or spam folder."
    elif status == "already_confirmed":
        message = "You're already subscribed — no changes made."
    else:
        message = "Check your email to confirm your subscription."

    return templates.TemplateResponse(request, "subscribe.html", {"success": message})


@app.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_subscription(token: str, request: Request, db: Session = Depends(get_db)):
    allowed, retry_after = confirm_limiter.check(_client_ip(request))
    if not allowed:
        return _rate_limit_error(request, retry_after)

    subscriber = subscriptions.confirm(db, token)
    if subscriber is None:
        return templates.TemplateResponse(
            request,
            "subscribe.html",
            {"error": "That confirmation link is not valid."},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "subscribe.html",
        {"success": "Your subscription is confirmed. You'll receive new summaries by email."},
    )


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, request: Request, db: Session = Depends(get_db)):
    """Render an unsubscribe confirmation page.

    This handler is intentionally read-only: it is the link embedded in emails,
    and mail-security scanners/link-prefetchers routinely GET such URLs before a
    human ever clicks. Performing the unsubscribe here would silently drop
    legitimate subscribers. The mutation happens only on the POST below, driven
    by an explicit button press (or an RFC 8058 one-click POST).
    """
    subscriber = subscriptions.find_by_token(db, token)
    found = subscriber is not None
    return templates.TemplateResponse(
        request,
        "unsubscribe.html",
        {
            "found": found,
            "done": False,
            "token": token,
            "email": subscriber.email if subscriber else None,
        },
        status_code=200 if found else 404,
    )


@app.post("/unsubscribe/{token}")
def unsubscribe_submit(token: str, request: Request, db: Session = Depends(get_db)):
    """Perform the unsubscribe.

    Serves both the confirmation-page button and RFC 8058 one-click POSTs. Mail
    clients issuing the one-click request ignore the response body, so returning
    the HTML confirmation page here is fine for both callers.
    """
    subscriber = subscriptions.unsubscribe(db, token)
    if subscriber is None:
        return templates.TemplateResponse(
            request,
            "unsubscribe.html",
            {"found": False, "done": True, "token": token, "email": None},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "unsubscribe.html",
        {"found": True, "done": True, "token": token, "email": subscriber.email},
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(request, "about.html")


@app.get("/health")
def health():
    return {"status": "ok"}
