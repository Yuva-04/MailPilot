import csv

import io

import os

import re
import threading
import time
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile

from starlette.middleware.sessions import SessionMiddleware
from fastapi.middleware.cors import CORSMiddleware

from authlib.integrations.starlette_client import OAuth

from pydantic import BaseModel, Field

from sqlalchemy import func
from sqlalchemy import update

from sqlalchemy.orm import Session

from app.config import settings

from app.database import Base, engine, get_db

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from app.models import Campaign, CampaignRecipient, Contact, EmailTemplate, User

from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.security import encrypt_refresh_token



from fastapi.responses import StreamingResponse
from app.retry_worker import process_due_retries
from app.database import SessionLocal
from app.services.gmail_services import send_email
from app.retry_worker import (
    process_due_retries,
    recover_stuck_sending_records,
)

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from app.services.campaign_service import (
    render_campaign_text,
    get_retry_time,
    claim_pending_recipient,
)





def run_retry_job():
    db = SessionLocal()
    try:
        # Step 1: Recover stale records for manual review.
        recover_stuck_sending_records(
            db,
            older_than_minutes=15,
        )

        # Step 2: Process eligible retries.
        process_due_retries(db, limit=50)

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler(timezone="UTC")

    retry_enabled = (
        os.getenv("ENABLE_EMAIL_RETRIES", "false").strip().lower()
        == "true"
    )
    scheduled_checks_enabled = (
        os.getenv("ENABLE_SCHEDULED_CAMPAIGN_CHECKS", "false")
        .strip()
        .lower()
        == "true"
    )

    if retry_enabled:
        scheduler.add_job(
            run_retry_job,
            trigger="interval",
            minutes=1,
            id="email_retry_job",
            max_instances=1,
            coalesce=True,
        )

    if scheduled_checks_enabled:
        from app.scheduled_campaign_worker import run_scheduled_campaign_job

        scheduler.add_job(
            run_scheduled_campaign_job,
            trigger="interval",
            seconds=30,
            id="scheduled_campaign_check",
            max_instances=1,
            coalesce=True,
        )

    # Keep the scheduler stopped unless at least one worker is explicitly enabled.
    if retry_enabled or scheduled_checks_enabled:
        scheduler.start()

    app.state.retry_scheduler = scheduler
    app.state.email_retries_enabled = retry_enabled
    app.state.scheduled_campaign_checks_enabled = scheduled_checks_enabled

    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


# --------------------------------------------------

# APP INITIALIZATION

# --------------------------------------------------

app = FastAPI(

    title="Email Campaign & Mail Merge Tool",

    description="API for managing contacts, email templates, and campaigns.",

    version="3.6.0",

     lifespan=lifespan,

)

# Signed session cookies for the Google OAuth flow.

# Local HTTP only; use https_only=True in production HTTPS.

app.add_middleware(

    SessionMiddleware,

    secret_key=settings.SESSION_SECRET_KEY,

    same_site="lax",

    https_only=False,

)

# Allow the Chrome extension and local development frontend to call the API.

# Chrome extension IDs contain 32 characters from a-p.

app.add_middleware(

    CORSMiddleware,

    allow_origins=[

        "http://127.0.0.1:5500",

        "http://localhost:5500",

    ],

    allow_origin_regex=r"^chrome-extension://[a-p]{32}$",

    allow_credentials=True,

    allow_methods=["*"],

    allow_headers=["*"],

)

oauth = OAuth()

oauth.register(

    name="google",

    client_id=settings.GOOGLE_CLIENT_ID,

    client_secret=settings.GOOGLE_CLIENT_SECRET,

    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",

    client_kwargs={"scope": "openid email profile"},

)

# Separate OAuth client for Gmail sending permission. Keep this flow distinct

# from the sign-in flow so the existing Google login callback remains unchanged.

oauth.register(

    name="gmail",

    client_id=settings.GOOGLE_CLIENT_ID,

    client_secret=settings.GOOGLE_CLIENT_SECRET,

    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",

    client_kwargs={"scope": "https://www.googleapis.com/auth/gmail.send"},

)

# Do not initialize the configured database during test imports.

# Tests should create their own isolated test database.

if os.getenv("APP_ENV", "development").lower() != "test":

    Base.metadata.create_all(bind=engine)

# --------------------------------------------------

# REQUEST SCHEMAS

# --------------------------------------------------

class ContactCreate(BaseModel):

    email: str = Field(min_length=1, max_length=320)

    first_name: str | None = Field(default=None, max_length=100)

    last_name: str | None = Field(default=None, max_length=100)

    company: str | None = Field(default=None, max_length=200)

class ContactUpdate(BaseModel):

    email: str | None = None

    first_name: str | None = None

    last_name: str | None = None

    company: str | None = None

    is_unsubscribed: bool | None = None

class EmailTemplateCreate(BaseModel):

    name: str = Field(min_length=1, max_length=200)

    subject: str = Field(min_length=1)

    body: str = Field(min_length=1)

class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template_id: int = Field(gt=0)
    scheduled_at: datetime | None = None
    timezone: str = "Asia/Kolkata"
    contact_ids: list[int] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid timezone, e.g. Asia/Kolkata")
        return value

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError(
                "scheduled_at must include a timezone offset"
            )
        return value


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    template_id: int | None = Field(default=None, gt=0)
    status: str | None = None
    scheduled_at: datetime | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid timezone, e.g. Asia/Kolkata")
        return value

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError(
                "scheduled_at must include a timezone offset"
            )
        return value

class TestEmailRequest(BaseModel):

    to: str = Field(min_length=3, max_length=320)

    subject: str = Field(min_length=1, max_length=998)

    body: str = Field(min_length=1, max_length=100000)

class CampaignLaunchRequest(BaseModel):

    confirm: bool

    contact_ids: list[int] = Field(default_factory=list)

    max_recipients: int = Field(default=1, ge=1, le=50)

# --------------------------------------------------

# EMAIL VALIDATION HELPER

# --------------------------------------------------

def is_valid_email(email: str | None) -> bool:

    """Check the basic format of an email address."""

    if not email or not isinstance(email, str):

        return False

    email = email.strip()

    if len(email) > 254 or email.count("@") != 1:

        return False

    local_part, domain = email.rsplit("@", 1)

    if not local_part or len(local_part) > 64:

        return False

    if local_part.startswith(".") or local_part.endswith("."):

        return False

    if ".." in local_part or ".." in domain:

        return False

    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'\*+/=?^\_\`{|}\~-]+", local_part):

        return False

    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", domain):

        return False

    labels = domain.split(".")

    if any(

        not label or label.startswith("-") or label.endswith("-")

        for label in labels

    ):

        return False

    if len(labels[-1]) < 2:

        return False

    return True

# --------------------------------------------------

# ROOT AND HEALTH

# --------------------------------------------------

@app.get("/")

def home():

    return {

        "message": "Email Campaign & Mail Merge Tool API",

        "version": "3.6.0",

        "docs": "/docs",

    }

@app.get("/health")

def health():

    return {"status": "healthy"}

# --------------------------------------------------

# CONTACT IMPORT

# --------------------------------------------------

@app.get("/auth/google/login")

async def google_login(request: Request):

    """Redirect the user to Google's sign-in page."""

    return await oauth.google.authorize_redirect(request, settings.GOOGLE_REDIRECT_URI)

@app.get("/auth/google/callback")

async def google_callback(request: Request, db: Session = Depends(get_db)):

    """Handle Google's response, persist the user, and establish a session."""

    token = await oauth.google.authorize_access_token(request)

    user_info = token.get("userinfo")

    if not user_info:

        user_info = await oauth.google.userinfo(token=token)

    google_sub = user_info.get("sub")

    email = user_info.get("email")

    name = user_info.get("name", "")

    if not google_sub or not email:

        raise HTTPException(

            status_code=400,

            detail="Google did not return the required user information.",

        )

    user = db.query(User).filter(User.google_sub == google_sub).first()

    if user is None:

        user = User(google_sub=google_sub, email=email, name=name)

        db.add(user)

    else:

        user.email = email

        user.name = name

    db.commit()

    db.refresh(user)

    request.session["user"] = {

        "google_sub": user.google_sub,

        "email": user.email,

        "name": user.name or "",

    }

    return {"message": "Google sign-in successful.", "user": request.session["user"]}

@app.get("/auth/me")

async def get_current_user(request: Request):

    """Return the signed-in user's identity."""

    user = request.session.get("user")

    if not user:

        raise HTTPException(status_code=401, detail="Not signed in.")

    return {"user": user}

@app.post("/auth/logout")

async def logout(request: Request):

    """Clear the current session."""

    request.session.clear()

    return {"message": "Logged out successfully."}

# Resolve the Google session to a database user for ownership checks.

def get_current_database_user(

    request: Request,

    db: Session = Depends(get_db),

):

    session_user = request.session.get("user")

    if not session_user or not session_user.get("google_sub"):

        raise HTTPException(

            status_code=401,

            detail="Please sign in with Google first.",

        )

    user = (

        db.query(User)

        .filter(User.google_sub == session_user["google_sub"])

        .first()

    )

    if user is None:

        raise HTTPException(

            status_code=404,

            detail="User account not found in the database. Sign in again.",

        )

    return {

        "id": user.id,

        "google_sub": user.google_sub,

        "email": user.email,

        "name": user.name,

        "created_at": user.created_at,

    }

@app.get("/auth/gmail/connect")

async def gmail_connect(

    request: Request,

    current_user: dict = Depends(get_current_database_user),

):

    """Start a separate OAuth flow requesting permission to send Gmail messages."""

    # Store the intended application user in the signed session before redirecting.

    request.session["gmail_connect_user_id"] = current_user["id"]

    return await oauth.gmail.authorize_redirect(

        request,

        settings.GMAIL_REDIRECT_URI,

        access_type="offline",

        prompt="consent",

        include_granted_scopes="true",

    )

@app.get("/auth/gmail/callback")

async def gmail_callback(

    request: Request,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    """Save an encrypted Gmail refresh token for the signed-in database user."""

    expected_user_id = request.session.get("gmail_connect_user_id")

    if expected_user_id is None or expected_user_id != current_user["id"]:

        raise HTTPException(

            status_code=400,

            detail="Gmail connection session expired or changed. Start the connection again.",

        )

    try:

        token = await oauth.gmail.authorize_access_token(request)

    except Exception as exc:

        # Avoid returning provider internals or tokens to the client.

        raise HTTPException(

            status_code=400,

            detail="Gmail authorization failed. Please try connecting Gmail again.",

        ) from exc

    user = db.query(User).filter(User.id == current_user["id"]).first()

    if user is None:

        raise HTTPException(status_code=404, detail="User account not found.")

    refresh_token = token.get("refresh_token")

    if refresh_token:

        try:

            user.gmail_refresh_token = encrypt_refresh_token(refresh_token)

        except Exception as exc:

            db.rollback()

            raise HTTPException(

                status_code=500,

                detail="Could not securely save the Gmail authorization. Check server configuration.",

            ) from exc

    elif not user.gmail_refresh_token:

        # Google may omit refresh_token if it does not issue a new offline token.

        db.rollback()

        raise HTTPException(

            status_code=400,

            detail="Google did not return a refresh token. Revoke this app's access in your Google Account and try connecting again.",

        )

    db.commit()

    request.session.pop("gmail_connect_user_id", None)

    return {

        "message": "Gmail connected successfully.",

        "gmail_connected": True,

        "next_step": "Gmail is connected. You can now check the connection status in the extension.",

    }

@app.get("/auth/gmail/status")

def gmail_status(current_user: dict = Depends(get_current_database_user), db: Session = Depends(get_db)):

    """Report whether the current application user has connected Gmail."""

    user = db.query(User).filter(User.id == current_user["id"]).first()

    if user is None:

        raise HTTPException(status_code=404, detail="User account not found.")

    connected = bool(user.gmail_refresh_token)

    return {"gmail_connected": connected, "email": user.email if connected else None}

@app.post("/gmail/test-send")

def gmail_test_send(

    payload: TestEmailRequest,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    """Send one test email through the connected user's Gmail account."""

    recipient = payload.to.strip()

    if not is_valid_email(recipient):

        raise HTTPException(status_code=422, detail="Please provide a valid recipient email address.")

    user = db.query(User).filter(User.id == current_user["id"]).first()

    if user is None:

        raise HTTPException(status_code=404, detail="User account not found.")

    if not user.gmail_refresh_token:

        raise HTTPException(

            status_code=409,

            detail="Gmail is not connected. Connect Gmail before sending a test email.",

        )

    try:

        result = send_email(

            encrypted_refresh_token=user.gmail_refresh_token,

            sender_email=user.email,

            recipient_email=recipient,

            subject=payload.subject.strip(),

            body=payload.body,

        )

    except Exception as exc:

        # Do not expose provider responses, credentials, or token details to clients.

        raise HTTPException(

            status_code=502,

            detail=(

                "Gmail could not send the test email. Check the server logs and "

                "confirm the Gmail connection and API permissions."

            ),

        ) from exc

    return {

        "message": "Test email accepted by Gmail API.",

        "to": recipient,

        "gmail_message_id": result.get("gmail_message_id"),

        "thread_id": result.get("thread_id"),

    }

@app.delete("/auth/gmail/disconnect")

def gmail_disconnect(

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    """Remove the saved Gmail authorization for the current application user."""

    user = db.query(User).filter(User.id == current_user["id"]).first()

    if user is None:

        raise HTTPException(status_code=404, detail="User account not found.")

    user.gmail_refresh_token = None

    db.commit()

    return {"message": "Gmail disconnected successfully.", "gmail_connected": False}

@app.get("/users/me")

def get_current_database_user_details(

    current_user: dict = Depends(get_current_database_user),

):

    return current_user


@app.post("/contacts/import")
def import_contacts(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_database_user),
):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail="Please upload a CSV file.",
        )

    try:
        content = file.file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))

        if not reader.fieldnames:
            raise HTTPException(
                status_code=400,
                detail="The CSV file has no header row.",
            )

        # Normalize headers while preserving their original names.
        headers = {
            header.strip().lower(): header
            for header in reader.fieldnames
            if header and header.strip()
        }

        if "email" not in headers:
            raise HTTPException(
                status_code=400,
                detail="CSV must contain an 'email' column.",
            )

        imported_count = 0
        duplicate_count = 0
        invalid_count = 0
        total_rows = 0
        seen_in_file = set()
        errors = []

        for row_number, row in enumerate(reader, start=2):
            # Ignore completely empty CSV lines.
            if not row or not any(
                str(value or "").strip() for value in row.values()
            ):
                continue

            total_rows += 1

            email = (row.get(headers["email"]) or "").strip()
            normalized_email = email.casefold()

            # Missing or invalid email.
            if not email or not is_valid_email(email):
                invalid_count += 1
                errors.append({
                    "row": row_number,
                    "email": email or None,
                    "reason": "Missing or invalid email address.",
                })
                continue

            # Duplicate email inside the uploaded CSV.
            if normalized_email in seen_in_file:
                duplicate_count += 1
                errors.append({
                    "row": row_number,
                    "email": email,
                    "reason": "Duplicate email in uploaded CSV.",
                })
                continue

            seen_in_file.add(normalized_email)

            # Duplicate email already stored for this user.
            existing = (
                db.query(Contact)
                .filter(
                    Contact.user_id == current_user["id"],
                    func.lower(Contact.email) == normalized_email,
                )
                .first()
            )

            if existing:
                duplicate_count += 1
                errors.append({
                    "row": row_number,
                    "email": email,
                    "reason": "Contact with this email already exists.",
                })
                continue

            def get_value(column_name: str):
                original_header = headers.get(column_name)
                if not original_header:
                    return None

                value = row.get(original_header)
                return value.strip() if value else None

            db.add(Contact(
                user_id=current_user["id"],
                email=email,
                first_name=get_value("first_name"),
                last_name=get_value("last_name"),
                company=get_value("company"),
                is_unsubscribed=False,
            ))

            imported_count += 1

        db.commit()

        skipped_count = duplicate_count + invalid_count

        return {
            "message": "Contact import completed.",
            "total_rows": total_rows,
            "imported": imported_count,
            "imported_count": imported_count,
            "duplicates_skipped": duplicate_count,
            "duplicate_count": duplicate_count,
            "invalid_rows": invalid_count,
            "invalid_count": invalid_count,
            "skipped": skipped_count,
            "skipped_count": skipped_count,
            "errors": errors,
        }

    except UnicodeDecodeError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Could not read the CSV file. Save it as UTF-8 and retry.",
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

# --------------------------------------------------

# CONTACT MANAGEMENT

# --------------------------------------------------

def contact_to_dict(contact: Contact) -> dict:

    return {

        "id": contact.id,

        "email": contact.email,

        "first_name": contact.first_name,

        "last_name": contact.last_name,

        "company": contact.company,

        "is_unsubscribed": contact.is_unsubscribed,

        "created_at": contact.created_at,

    }

@app.post("/contacts", status_code=201)

def create_contact(

    payload: ContactCreate,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    """Create a contact owned by the signed-in user."""

    email = payload.email.strip()

    if not is_valid_email(email):

        raise HTTPException(status_code=422, detail="Please provide a valid email address.")

    normalized_email = email.casefold()

    duplicate = (

        db.query(Contact)

        .filter(

            Contact.user_id == current_user["id"],

            func.lower(Contact.email) == normalized_email,

        )

        .first()

    )

    if duplicate:

        raise HTTPException(status_code=409, detail="A contact with this email already exists.")

    contact = Contact(

        user_id=current_user["id"],

        email=email,

        first_name=payload.first_name.strip() if payload.first_name else None,

        last_name=payload.last_name.strip() if payload.last_name else None,

        company=payload.company.strip() if payload.company else None,

        is_unsubscribed=False,

    )

    try:

        db.add(contact)

        db.commit()

        db.refresh(contact)

    except Exception:

        db.rollback()

        raise

    return {"message": "Contact created successfully.", "contact": contact_to_dict(contact)}

@app.get("/contacts")

def list_contacts(

    skip: int = Query(default=0, ge=0),

    limit: int = Query(default=100, ge=1, le=500),

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    query = db.query(Contact).filter(Contact.user_id == current_user["id"])

    total = query.count()

    contacts = query.order_by(Contact.id).offset(skip).limit(limit).all()

    return {"count": total, "contacts": [contact_to_dict(c) for c in contacts]}


@app.get("/contacts/export")
def export_contacts(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_database_user),
):
    contacts = (
        db.query(Contact)
        .filter(Contact.user_id == current_user["id"])
        .order_by(Contact.id.asc())
        .all()
    )

    output = io.StringIO()
    fieldnames = [
        "id",
        "email",
        "first_name",
        "last_name",
        "company",
        "is_unsubscribed",
        "created_at",
    ]

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for contact in contacts:
        writer.writerow({
            "id": contact.id,
            "email": contact.email,
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "company": contact.company,
            "is_unsubscribed": contact.is_unsubscribed,
            "created_at": (
                contact.created_at.isoformat()
                if contact.created_at else ""
            ),
        })

    csv_content = "\ufeff" + output.getvalue()

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="contacts_export.csv"'
        },
    )


@app.get("/contacts/{contact_id}")

def get_contact(

    contact_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    contact = db.query(Contact).filter(

        Contact.id == contact_id,

        Contact.user_id == current_user["id"],

    ).first()

    if not contact:

        raise HTTPException(status_code=404, detail="Contact not found.")

    return contact_to_dict(contact)

@app.put("/contacts/{contact_id}")

def update_contact(

    contact_id: int,

    payload: ContactUpdate,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    contact = db.query(Contact).filter(

        Contact.id == contact_id,

        Contact.user_id == current_user["id"],

    ).first()

    if not contact:

        raise HTTPException(status_code=404, detail="Contact not found.")

    updates = payload.model_dump(exclude_unset=True)

    if "email" in updates:

        email = updates["email"]

        if not email or not email.strip():

            raise HTTPException(status_code=400, detail="Email cannot be empty.")

        email = email.strip()

        if not is_valid_email(email):

            raise HTTPException(status_code=400, detail="Please provide a valid email address.")

        normalized_email = email.casefold()

        duplicate = db.query(Contact).filter(

            Contact.user_id == current_user["id"],

            func.lower(Contact.email) == normalized_email,

            Contact.id != contact_id,

        ).first()

        if duplicate:

            raise HTTPException(status_code=409, detail="Another contact already uses this email.")

        updates["email"] = email

    for field, value in updates.items():

        setattr(contact, field, value)

    db.commit()

    db.refresh(contact)

    return {"message": "Contact updated successfully.", "contact": contact_to_dict(contact)}

@app.delete("/contacts/{contact_id}")

def delete_contact(

    contact_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    contact = db.query(Contact).filter(

        Contact.id == contact_id,

        Contact.user_id == current_user["id"],

    ).first()

    if not contact:

        raise HTTPException(status_code=404, detail="Contact not found.")

    db.delete(contact)

    db.commit()

    return {"message": "Contact deleted successfully."}

# --------------------------------------------------

# EMAIL TEMPLATE MANAGEMENT

# --------------------------------------------------

@app.post("/templates", status_code=201)

def create_template(

    payload: EmailTemplateCreate,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    """Create a template owned by the signed-in user."""

    name = payload.name.strip()

    subject = payload.subject.strip()

    body = payload.body.strip()

    if not name or not subject or not body:

        raise HTTPException(

            status_code=400,

            detail="Template name, subject, and body cannot be empty.",

        )

    template = EmailTemplate(

        user_id=current_user["id"],

        name=name,

        subject=subject,

        body=body,

    )

    db.add(template)

    db.commit()

    db.refresh(template)

    return {

        "message": "Email template created successfully.",

        "template": {

            "id": template.id,

            "name": template.name,

            "subject": template.subject,

            "body": template.body,

            "created_at": template.created_at,

        },

    }

@app.get("/templates")

def list_templates(

    skip: int = Query(default=0, ge=0),

    limit: int = Query(default=100, ge=1, le=500),

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    query = db.query(EmailTemplate).filter(

        EmailTemplate.user_id == current_user["id"]

    )

    total = query.count()

    templates = query.order_by(EmailTemplate.id).offset(skip).limit(limit).all()

    return {

        "count": total,

        "templates": [

            {

                "id": template.id,

                "name": template.name,

                "subject": template.subject,

                "body": template.body,

                "created_at": template.created_at,

            }

            for template in templates

        ],

    }

@app.get("/templates/{template_id}/preview/{contact_id}")

def preview_template(

    template_id: int,

    contact_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    template = db.query(EmailTemplate).filter(

        EmailTemplate.id == template_id,

        EmailTemplate.user_id == current_user["id"],

    ).first()

    if not template:

        raise HTTPException(

            status_code=404,

            detail="Email template not found.",

        )

    contact = db.query(Contact).filter(

        Contact.id == contact_id,

        Contact.user_id == current_user["id"],

    ).first()

    if not contact:

        raise HTTPException(

            status_code=404,

            detail="Contact not found.",

        )

    values = {

        "first_name": (contact.first_name or "").strip(),

        "last_name": (contact.last_name or "").strip(),

        "name": " ".join(

            part for part in [

                (contact.first_name or "").strip(),

                (contact.last_name or "").strip(),

            ]

            if part

        ),

        "email": (contact.email or "").strip(),

        "company": (contact.company or "").strip(),

    }

    def personalize(text: str) -> str:

        return re.sub(

            r"\{\{\s*(first_name|last_name|name|email|company)\s*\}\}",

            lambda match: values[match.group(1).lower()],

            text or "",

            flags=re.IGNORECASE,

        )

    return {

        "template_id": template.id,

        "contact_id": contact.id,

        "to": contact.email,

        "is_unsubscribed": contact.is_unsubscribed,

        "subject": personalize(template.subject),

        "body": personalize(template.body),

    }

# --------------------------------------------------

# CAMPAIGN MANAGEMENT

# --------------------------------------------------

def campaign_to_dict(campaign: Campaign) -> dict:
    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "template_id": campaign.template_id,
        "scheduled_at": (
            campaign.scheduled_at.isoformat()
            if campaign.scheduled_at
            else None
        ),
        "timezone": campaign.timezone,
        "created_at": (
            campaign.created_at.isoformat()
            if campaign.created_at
            else None
        ),
    }

@app.post("/campaigns", status_code=201)
def create_campaign(
    payload: CampaignCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_database_user),
):
    template = db.query(EmailTemplate).filter(
        EmailTemplate.id == payload.template_id,
        EmailTemplate.user_id == current_user["id"],
    ).first()

    if not template:
        raise HTTPException(
            status_code=404,
            detail="Email template not found.",
        )

    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail="Campaign name cannot be empty.",
        )

    # Remove duplicate contact IDs while preserving their order.
    contact_ids = list(dict.fromkeys(payload.contact_ids))

    # Validate contact ownership.
    contacts = []
    if contact_ids:
        contacts = db.query(Contact).filter(
            Contact.id.in_(contact_ids),
            Contact.user_id == current_user["id"],
        ).all()

        if len(contacts) != len(contact_ids):
            raise HTTPException(
                status_code=400,
                detail="One or more contacts are invalid or do not belong to you.",
            )

    campaign = Campaign(
        user_id=current_user["id"],
        name=name,
        template_id=template.id,
        status="scheduled" if payload.scheduled_at else "draft",
        scheduled_at=payload.scheduled_at,
        timezone=payload.timezone,
    )

    try:
        db.add(campaign)
        db.flush()

        # Save recipients without sending emails.
        for contact in contacts:
            db.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    contact_id=contact.id,
                    email=contact.email,
                    status="pending",
                )
            )

        db.commit()
        db.refresh(campaign)

    except Exception:
        db.rollback()
        raise

    return {
        "message": "Campaign created successfully.",
        "campaign": campaign_to_dict(campaign),
        "recipients_saved": len(contacts),
    }

@app.get("/campaigns")

def list_campaigns(

    skip: int = Query(default=0, ge=0),

    limit: int = Query(default=100, ge=1, le=500),

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    query = db.query(Campaign).filter(Campaign.user_id == current_user["id"])

    total = query.count()

    campaigns = query.order_by(Campaign.id).offset(skip).limit(limit).all()

    return {

        "count": total,

        "campaigns": [campaign_to_dict(campaign) for campaign in campaigns],

    }

@app.get("/campaigns/{campaign_id}")

def get_campaign(

    campaign_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    campaign = (
        db.query(Campaign)
        .filter(
            Campaign.id == campaign_id,
            Campaign.user_id == current_user["id"],
        )
        .with_for_update()
        .first()
    )
    if not campaign:

        raise HTTPException(status_code=404, detail="Campaign not found.")

    return campaign_to_dict(campaign)

@app.get("/campaigns/{campaign_id}/report")

def get_campaign_report(

    campaign_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    # Verify campaign ownership

    # Serialize launch attempts for this campaign. Keep this row lock until
    # the transition to "sending" is committed below.
    campaign = (
        db.query(Campaign)
        .filter(
            Campaign.id == campaign_id,
            Campaign.user_id == current_user["id"],
        )
        .with_for_update()
        .first()
    )

    if not campaign:

        raise HTTPException(

            status_code=404,

            detail="Campaign not found.",

        )

    # Fetch recipient outcomes

    recipients = (

        db.query(CampaignRecipient)

        .filter(CampaignRecipient.campaign_id == campaign_id)

        .all()

    )

    # Count recipient statuses

    status_counts = {}

    for recipient in recipients:

        status = (recipient.status or "pending").lower()

        status_counts[status] = status_counts.get(status, 0) + 1

    # Return individual recipient outcomes

    recipient_details = []

    for recipient in recipients:

        recipient_details.append({

            "recipient_id": recipient.id,

            "contact_id": recipient.contact_id,

            "email": recipient.contact.email if recipient.contact else None,

            "status": recipient.status,

            "attempts": recipient.attempts,

            "provider_message_id": recipient.provider_message_id,

            "error_message": recipient.error_message,

            "skipped_reason": recipient.skipped_reason,

            "sent_at": (

                recipient.sent_at.isoformat()

                if recipient.sent_at else None

            ),

        })

    return {

        "campaign_id": campaign.id,

        "campaign_name": campaign.name,

        "campaign_status": campaign.status,

        "total_recipients": len(recipients),

        "sent_count": status_counts.get("sent", 0),

        "failed_count": status_counts.get("failed", 0),

        "pending_count": status_counts.get("pending", 0),

        "skipped_count": status_counts.get("skipped", 0),

        "status_counts": status_counts,

        "recipients": recipient_details,

        "note": (

            "A sent status indicates Gmail accepted the send request; "

            "it does not guarantee inbox delivery."

        ),

    }

@app.put("/campaigns/{campaign_id}")

def update_campaign(

    campaign_id: int,

    payload: CampaignUpdate,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    campaign = db.query(Campaign).filter(

        Campaign.id == campaign_id,

        Campaign.user_id == current_user["id"],

    ).first()

    if not campaign:

        raise HTTPException(status_code=404, detail="Campaign not found.")

    updates = payload.model_dump(exclude_unset=True)

    if "name" in updates:

        name = updates["name"]

        if name is None or not name.strip():

            raise HTTPException(status_code=400, detail="Campaign name cannot be empty.")

        campaign.name = name.strip()

    if "template_id" in updates:

        template_id = updates["template_id"]

        if template_id is None:

            raise HTTPException(status_code=400, detail="Template ID cannot be empty.")

        template = db.query(EmailTemplate).filter(

            EmailTemplate.id == template_id,

            EmailTemplate.user_id == current_user["id"],

        ).first()

        if not template:

            raise HTTPException(status_code=404, detail="Email template not found.")

        campaign.template_id = template.id


    if "scheduled_at" in updates:
        campaign.scheduled_at = updates["scheduled_at"]

    if "timezone" in updates:
        campaign.timezone = updates["timezone"]

    if "status" in updates:

        allowed_statuses = {"draft", "scheduled", "paused", "cancelled"}

        status = updates["status"]

        if status not in allowed_statuses:

            raise HTTPException(

                status_code=400,

                detail="Status must be one of: draft, scheduled, paused, cancelled.",

            )

        campaign.status = status

    db.commit()

    db.refresh(campaign)

    return {

        "message": "Campaign updated successfully.",

        "campaign": campaign_to_dict(campaign),

    }

@app.delete("/campaigns/{campaign_id}")

def delete_campaign(

    campaign_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    campaign = db.query(Campaign).filter(

        Campaign.id == campaign_id,

        Campaign.user_id == current_user["id"],

    ).first()

    if not campaign:

        raise HTTPException(status_code=404, detail="Campaign not found.")

    db.delete(campaign)

    db.commit()

    return {"message": "Campaign deleted successfully."}

# --------------------------------------------------

# CAMPAIGN PREFLIGHT VALIDATION

# --------------------------------------------------

@app.post("/campaigns/{campaign_id}/preflight")

def preflight_campaign(

    campaign_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    errors = []

    warnings = []

    campaign = db.query(Campaign).filter(

        Campaign.id == campaign_id,

        Campaign.user_id == current_user["id"],

    ).first()

    if not campaign:

        raise HTTPException(status_code=404, detail="Campaign not found.")

    template = db.query(EmailTemplate).filter(

        EmailTemplate.id == campaign.template_id,

        EmailTemplate.user_id == current_user["id"],

    ).first()

    if not template:

        errors.append("Email template not found.")

    else:

        if not template.subject or not template.subject.strip():

            errors.append("Email template subject is empty.")

        if not template.body or not template.body.strip():

            errors.append("Email template body is empty.")

    contacts = (

        db.query(Contact)

        .filter(Contact.user_id == current_user["id"])

        .order_by(Contact.id)

        .all()

    )

    total_contacts = len(contacts)

    unsubscribed_contacts = [c for c in contacts if c.is_unsubscribed]

    subscribed_contacts = [c for c in contacts if not c.is_unsubscribed]

    if total_contacts == 0:

        errors.append("No contacts found. Import contacts first.")

    # Group contacts by normalized address to detect duplicate recipients.

    contacts_by_email = {}

    for contact in contacts:

        normalized_email = (contact.email or "").strip().casefold()

        if normalized_email:

            contacts_by_email.setdefault(normalized_email, []).append(contact)

    duplicate_emails = []

    for normalized_email, matching_contacts in contacts_by_email.items():

        if len(matching_contacts) > 1:

            duplicate_emails.append({

                "email": normalized_email,

                "contact_ids": [c.id for c in matching_contacts],

                "contact_count": len(matching_contacts),

                "has_unsubscribed_contact": any(

                    c.is_unsubscribed for c in matching_contacts

                ),

            })

    valid_contacts = [

        c for c in subscribed_contacts if is_valid_email(c.email)

    ]

    invalid_contacts = [

        {

            "contact_id": c.id,

            "email": c.email,

            "reason": "Invalid email format",

        }

        for c in subscribed_contacts

        if not is_valid_email(c.email)

    ]

    if invalid_contacts:

        errors.append(

            f"{len(invalid_contacts)} subscribed contact(s) have invalid email addresses."

        )

    # Suppress an address if any matching record has unsubscribed.

    eligible_emails = set()

    for contact in valid_contacts:

        normalized_email = (contact.email or "").strip().casefold()

        matching_contacts = contacts_by_email.get(normalized_email, [])

        if not any(c.is_unsubscribed for c in matching_contacts):

            eligible_emails.add(normalized_email)

    eligible_count = len(eligible_emails)

    if eligible_count == 0:

        errors.append(

            "No eligible recipients. Check email formats, duplicate addresses, "

            "and unsubscribe status."

        )

    if eligible_count:

        warnings.append(

            f"{eligible_count} unique recipient email address(es) are eligible "

            "based on format and unsubscribe status. Mailbox existence has not been verified."

        )

    if unsubscribed_contacts:

        warnings.append(

            f"{len(unsubscribed_contacts)} unsubscribed contact(s) will be excluded."

        )

    if duplicate_emails:

        warnings.append(

            f"{len(duplicate_emails)} duplicate email address group(s) detected. "

            "Each address is counted at most once; any address with an unsubscribed "

            "matching contact is suppressed."

        )

    warnings.append(

        "Mailbox deliverability and sending limits have not been checked yet."

    )

    return {

        "campaign_id": campaign.id,

        "campaign_name": campaign.name,

        "ready": len(errors) == 0,

        "total_contacts": total_contacts,

        "unsubscribed_count": len(unsubscribed_contacts),

        "subscribed_count": len(subscribed_contacts),

        "valid_email_count": len(valid_contacts),

        "unique_valid_email_count": len({

            (c.email or "").strip().casefold() for c in valid_contacts

        }),

        "invalid_email_count": len(invalid_contacts),

        "eligible_recipient_count": eligible_count,

        "duplicate_email_count": len(duplicate_emails),

        "duplicate_emails": duplicate_emails,

        "invalid_contacts": invalid_contacts,

        "errors": errors,

        "warnings": warnings,

    }

class TemplateCreate(BaseModel):

    name: str = Field(min_length=1, max_length=200)

    subject: str = Field(min_length=1)

    body: str = Field(min_length=1)

@app.put("/templates/{template_id}")

def update_template(

    template_id: int,

    template_data: TemplateCreate,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    template = (

        db.query(EmailTemplate)

        .filter(

            EmailTemplate.id == template_id,

            EmailTemplate.user_id == current_user["id"],

        )

        .first()

    )

    if not template:

        raise HTTPException(status_code=404, detail="Template not found.")

    name = template_data.name.strip()

    subject = template_data.subject.strip()

    body = template_data.body.strip()

    if not name or not subject or not body:

        raise HTTPException(

            status_code=400,

            detail="Template name, subject, and body cannot be empty.",

        )

    template.name = name

    template.subject = subject

    template.body = body

    db.commit()

    db.refresh(template)

    return {

        "message": "Template updated successfully.",

        "template": {

            "id": template.id,

            "name": template.name,

            "subject": template.subject,

            "body": template.body,

            "created_at": template.created_at,

        },

    }

@app.delete("/templates/{template_id}")

def delete_template(

    template_id: int,

    db: Session = Depends(get_db),

    current_user: dict = Depends(get_current_database_user),

):

    template = (

        db.query(EmailTemplate)

        .filter(

            EmailTemplate.id == template_id,

            EmailTemplate.user_id == current_user["id"],

        )

        .first()

    )

    if not template:

        raise HTTPException(status_code=404, detail="Template not found.")

    campaign = (

        db.query(Campaign)

        .filter(

            Campaign.template_id == template.id,

            Campaign.user_id == current_user["id"],

        )

        .first()

    )

    if campaign:

        raise HTTPException(

            status_code=409,

            detail="Cannot delete this template because it is used by a campaign.",

        )

    try:

        db.delete(template)

        db.commit()

    except Exception as exc:

        db.rollback()

        raise HTTPException(

            status_code=500,

            detail="Could not delete template.",

        ) from exc

    return {

        "message": "Template deleted successfully.",

        "template_id": template_id,

    }

# --------------------------------------------------

# RESET APPLICATION DATA

# --------------------------------------------------


@app.post("/admin/reset-data")
@app.delete("/admin/reset-data")
def reset_application_data(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_database_user),
):
    """Delete application data belonging only to the signed-in user."""
    user_id = current_user["id"]

    try:
        # Find only this user's campaign IDs.
        campaign_ids = [
            row[0]
            for row in db.query(Campaign.id)
            .filter(Campaign.user_id == user_id)
            .all()
        ]

        # Delete recipient records before their referenced contacts.
        recipients_deleted = 0
        if campaign_ids:
            recipients_deleted = (
                db.query(CampaignRecipient)
                .filter(CampaignRecipient.campaign_id.in_(campaign_ids))
                .delete(synchronize_session=False)
            )

        campaigns_deleted = (
            db.query(Campaign)
            .filter(Campaign.user_id == user_id)
            .delete(synchronize_session=False)
        )

        templates_deleted = (
            db.query(EmailTemplate)
            .filter(EmailTemplate.user_id == user_id)
            .delete(synchronize_session=False)
        )

        contacts_deleted = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .delete(synchronize_session=False)
        )

        db.commit()

        return {
            "message": "Application data reset successfully.",
            "deleted": {
                "recipients": recipients_deleted,
                "campaigns": campaigns_deleted,
                "templates": templates_deleted,
                "contacts": contacts_deleted,
            },
        }

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not reset application data.",
        ) from exc

# --------------------------------------------------

# CAMPAIGN LAUNCH

# --------------------------------------------------
def _launch_campaign(
    campaign_id: int,
    payload: CampaignLaunchRequest,
    db: Session,
    current_user: dict,
    *,
    scheduled: bool = False,
):
    """Shared campaign execution logic for manual and scheduled launches.

    The scheduled=True path is called only by the internal worker after it
    independently verifies that the campaign is due and still scheduled.
    """
    if not scheduled and not payload.confirm:

        raise HTTPException(

            status_code=400,

            detail="Explicit confirmation is required before sending.",

        )

    campaign = db.query(Campaign).filter(

        Campaign.id == campaign_id,

        Campaign.user_id == current_user["id"],

    ).first()

    if not campaign:

        raise HTTPException(status_code=404, detail="Campaign not found.")

    
    if scheduled:
        scheduled_at = campaign.scheduled_at
        if scheduled_at is not None and scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        if (
            campaign.status != "scheduled"
            or scheduled_at is None
            or scheduled_at > datetime.now(timezone.utc)
        ):
            raise HTTPException(
                status_code=409,
                detail="Campaign is no longer due and scheduled.",
            )
    else:
        allowed_launch_states = {"draft", "paused"}
        if campaign.status not in allowed_launch_states:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Campaign cannot be launched with status "
                    f"'{campaign.status}'. Only draft or paused "
                    "campaigns can be launched."
                ),
            )

    user = db.query(User).filter(

        User.id == current_user["id"]

    ).first()

    if not user or not user.gmail_refresh_token:

        raise HTTPException(

            status_code=409,

            detail="Connect Gmail before launching a campaign.",

        )

    template = db.query(EmailTemplate).filter(

        EmailTemplate.id == campaign.template_id,

        EmailTemplate.user_id == current_user["id"],

    ).first()

    if not template or not template.subject.strip() or not template.body.strip():

        raise HTTPException(

            status_code=400,

            detail="The campaign email template is missing or incomplete.",

        )

    if not payload.contact_ids:
        raise HTTPException(
            status_code=400,
            detail="Select at least one contact before launching.",
        )

    if len(payload.contact_ids) != len(set(payload.contact_ids)):
        raise HTTPException(
            status_code=400,
            detail="Duplicate contact IDs are not allowed.",
        )

    contacts = (
        db.query(Contact)
        .filter(
            Contact.user_id == current_user["id"],
            Contact.id.in_(payload.contact_ids),
        )
        .order_by(Contact.id)
        .all()
    )

    if len(contacts) != len(payload.contact_ids):
        raise HTTPException(
            status_code=400,
            detail="One or more selected contacts were not found.",
        )

    # Group addresses case-insensitively. If any record has unsubscribed,

    # suppress that address completely.

    contacts_by_email = {}

    for contact in contacts:

        normalized = (contact.email or "").strip().casefold()

        if normalized:

            contacts_by_email.setdefault(normalized, []).append(contact)

    eligible_contacts = []

    invalid_count = 0

    suppressed_count = 0

    for normalized_email, matching in contacts_by_email.items():

        if any(c.is_unsubscribed for c in matching):

            suppressed_count += 1

            continue

        canonical = matching[0]

        if not is_valid_email(canonical.email):

            invalid_count += 1

            continue

        eligible_contacts.append(canonical)

    if invalid_count:

        raise HTTPException(

            status_code=400,

            detail=(

                f"{invalid_count} subscribed address(es) are invalid. "

                "Correct the contact list before launching."

            ),

        )

        # Select only new recipients or retries whose scheduled time has arrived.
    pending_contacts = []
    already_sent_count = 0
    now = datetime.now(timezone.utc)

    for contact in eligible_contacts:
        record = db.query(CampaignRecipient).filter(
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.contact_id == contact.id,
        ).first()

        if record and record.status == "sent":
            already_sent_count += 1
            continue

        # Never automatically resend an ambiguous failure.
        if record and record.status == "failed":
            continue

        # A pending retry must wait until its scheduled time.
        if record and record.next_retry_at is not None:
            retry_at = record.next_retry_at
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)

            if retry_at > now:
                continue

        pending_contacts.append(contact)

    batch = pending_contacts[:payload.max_recipients]
    
    
    # Record selected unsubscribed contacts as skipped.
    pending_ids = {contact.id for contact in pending_contacts}

    matching_contacts = (
        db.query(Contact)
        .filter(Contact.user_id == current_user["id"])
        .all()
    )

    for contact in contacts:
        if contact.id in pending_ids:
            continue

        normalized_email = (contact.email or "").strip().casefold()
        is_suppressed = any(
            (item.email or "").strip().casefold() == normalized_email
            and item.is_unsubscribed
            for item in matching_contacts
        )

        if is_suppressed:
            record = (
                db.query(CampaignRecipient)
                .filter(
                    CampaignRecipient.campaign_id == campaign.id,
                    CampaignRecipient.contact_id == contact.id,
                )
                .first()
            )

            if record is None:
                record = CampaignRecipient(
                    campaign_id=campaign.id,
                    contact_id=contact.id,
                    email=contact.email,
                    status="skipped",
                )
                db.add(record)

            record.email = contact.email
            record.status = "skipped"
            record.next_retry_at = None
            record.skipped_reason = "Address is unsubscribed/suppressed"
            record.error_message = "Address is unsubscribed/suppressed"

    db.commit()


    if not batch:

        raise HTTPException(

            status_code=400,

            detail="No unsent eligible recipients remain.",

        )

    sent_count = 0
    failed_count = 0
    retry_scheduled_count = 0
    results = []

    # Persist recipient rows before marking the campaign as sending. This keeps
    # the report useful even if an unexpected error occurs during processing.
    try:
        for contact in batch:
            record = db.query(CampaignRecipient).filter(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.contact_id == contact.id,
            ).first()

            if record is None:
                db.add(
    CampaignRecipient(
        campaign_id=campaign.id,
        contact_id=contact.id,
        email=contact.email,
        status="pending",
        attempts=0,
    )
)
                

        db.flush()
        campaign.status = "sending"
        db.commit()
    except Exception:
        db.rollback()
        raise

    for contact in batch:

        # Refresh the contact and query all matching address records immediately
        # before sending. The earlier contacts_by_email snapshot may be stale if
        # another matching contact unsubscribed while this campaign was running.
        db.refresh(contact)
        normalized_email = (contact.email or "").strip().casefold()
        live_matching_contacts = (
            db.query(Contact)
            .filter(
                Contact.user_id == current_user["id"],
                func.lower(func.trim(Contact.email)) == normalized_email,
            )
            .all()
        ) if normalized_email else []

        is_suppressed_now = any(
            matching_contact.is_unsubscribed
            for matching_contact in live_matching_contacts
        )

        if is_suppressed_now:
            record = db.query(CampaignRecipient).filter(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.contact_id == contact.id,
            ).first()

            if record:
                record.status = "skipped"
                record.next_retry_at = None
                record.skipped_reason = "Recipient has unsubscribed."
                record.error_message = "Recipient has unsubscribed."

            db.commit()

            results.append({
                "contact_id": contact.id,
                "status": "skipped",
                "reason": "Recipient has unsubscribed.",
            })
            continue

        
        

        record = db.query(CampaignRecipient).filter(

            CampaignRecipient.campaign_id == campaign.id,

            CampaignRecipient.contact_id == contact.id,

        ).first()

        if record and record.status in {"sent", "sending"}:

            results.append({

                "contact_id": contact.id,

                "status": "skipped",

                "reason": "Recipient already sent or currently being processed",

            })

            continue

        if record is None:

            record = CampaignRecipient(

                campaign_id=campaign.id,

                contact_id=contact.id,

                status="pending",

                attempts=0,

            )

            db.add(record)

            db.flush()

        if record.attempts >= record.max_attempts:

            record.status = "failed"

            record.error_message = "Maximum send attempts reached."

            db.commit()

            failed_count += 1

            results.append({

                "contact_id": contact.id,

                "status": "failed",

                "reason": "Maximum send attempts reached",

            })

            continue

        rendered_subject = render_campaign_text(template.subject, contact)

        # Atomic compare-and-set: only a pending recipient below the attempt
        # limit can be claimed by this request. Competing workers/requests that
        # changed the status first will get rowcount == 0 and will not send.
        claimed = claim_pending_recipient(
            db=db,
            record=record,
            rendered_subject=rendered_subject,
        )
        if not claimed:
            results.append({
                "contact_id": contact.id,
                "status": "skipped",
                "reason": "Another process claimed this recipient",
            })
            continue

        try:

            gmail_result = send_email(

                encrypted_refresh_token=user.gmail_refresh_token,

                sender_email=user.email,

                recipient_email=contact.email.strip(),

                subject=record.rendered_subject,

                body=render_campaign_text(template.body, contact),

            )

            record.status = "sent"

            record.provider_message_id = gmail_result.get("gmail_message_id")

            record.sent_at = datetime.now(timezone.utc)

            record.error_message = None

            sent_count += 1

            results.append({

                "contact_id": contact.id,

                "email": contact.email,

                "status": "sent",

                "gmail_message_id": record.provider_message_id,

            })

        except Exception as exc:
            # Retry only a confirmed HTTP 429 temporary rejection.
            response = getattr(exc, "resp", None)
            status_code = getattr(response, "status", None)

            if status_code == 429 and record.attempts < record.max_attempts:
                record.status = "pending"
                record.next_retry_at = get_retry_time(record.attempts)
                record.error_message = (
                    "Gmail temporarily rejected the request (HTTP 429). "
                    f"Retry scheduled for {record.next_retry_at.isoformat()}."
                )

                retry_scheduled_count += 1
                results.append({
                    "contact_id": contact.id,
                    "email": contact.email,
                    "status": "retry_scheduled",
                    "retry_at": record.next_retry_at.isoformat(),
                })

            else:
                # Do not retry ambiguous outcomes or other errors automatically.
                record.status = "failed"
                record.next_retry_at = None
                record.error_message = (
                    "Gmail send failed or its outcome could not be confirmed. "
                    "Check Gmail before retrying. "
                    f"Error type: {type(exc).__name__}"
                )

                failed_count += 1
                results.append({
                    "contact_id": contact.id,
                    "email": contact.email,
                    "status": "failed",
                    "reason": record.error_message,
                })

        db.commit()

    remaining_count = max(0, len(pending_contacts) - len(batch))

    if scheduled:
        # Keep scheduled campaigns eligible for the next scheduler tick when
        # more pending recipients remain, including retries not yet due.
        pending_remaining = (
            db.query(CampaignRecipient.id)
            .filter(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status == "pending",
            )
            .count()
        )
        if pending_remaining:
            campaign.status = "scheduled"
        elif failed_count:
            campaign.status = "completed_with_errors"
        else:
            campaign.status = "completed"
    elif remaining_count or retry_scheduled_count:
        campaign.status = "paused"
    elif failed_count:
        campaign.status = "completed_with_errors"
    else:
        campaign.status = "completed"

    db.commit()

    return {

        "campaign_id": campaign.id,

        "campaign_status": campaign.status,

        "sent_count": sent_count,

        "retry_scheduled_count": retry_scheduled_count,

        "suppressed_address_count": suppressed_count,

        "already_sent_count": already_sent_count,

        "remaining_count": remaining_count,

        "results": results,

        "note": (

            "Sent means the Gmail API accepted the request. "

            "It does not confirm inbox delivery."

        ),

    }


@app.post("/campaigns/{campaign_id}/launch")
def launch_campaign(
    campaign_id: int,
    payload: CampaignLaunchRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_database_user),
):
    """Explicitly confirmed manual launch endpoint."""
    return _launch_campaign(
        campaign_id=campaign_id,
        payload=payload,
        db=db,
        current_user=current_user,
        scheduled=False,
    )
