"""FastAPI application — routes and startup."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from council_meetings import subscriptions
from council_meetings.db import get_db, init_db
from council_meetings.models import Document, Meeting

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


app = FastAPI(title="Campbell Council Meetings", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

BASE_URL = "https://www.campbellca.gov"


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

    _, status = subscriptions.subscribe(db, email)
    if status == "already_active":
        message = "You're already subscribed — no changes made."
    else:
        message = "You're subscribed! You'll get an email when new summaries are posted."

    return templates.TemplateResponse(
        request,
        "subscribe.html",
        {"success": message},
    )


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_get(token: str, request: Request, db: Session = Depends(get_db)):
    subscriber = subscriptions.unsubscribe(db, token)
    found = subscriber is not None
    return templates.TemplateResponse(
        request,
        "unsubscribe.html",
        {"found": found, "email": subscriber.email if subscriber else None},
        status_code=200 if found else 404,
    )


@app.post("/unsubscribe/{token}", response_class=PlainTextResponse)
def unsubscribe_post(token: str, db: Session = Depends(get_db)):
    """RFC 8058 one-click unsubscribe endpoint (used by mail clients)."""
    subscriber = subscriptions.unsubscribe(db, token)
    if subscriber is None:
        return PlainTextResponse("Unknown unsubscribe token.", status_code=404)
    return PlainTextResponse("You have been unsubscribed.")


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(request, "about.html")


@app.get("/health")
def health():
    return {"status": "ok"}
