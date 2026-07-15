"""FastAPI application — routes and startup."""

import logging
from contextlib import asynccontextmanager
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

    return templates.TemplateResponse(
        request,
        "subscribe.html",
        {"success": "Check your email to confirm your subscription."},
    )


@app.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_subscription(token: str, request: Request, db: Session = Depends(get_db)):
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
