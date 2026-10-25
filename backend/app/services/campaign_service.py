import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.models import Campaign, CampaignRecipient, Contact


def get_due_campaign_ids(db: Session) -> list[int]:
    """Return scheduled campaigns that are due and have an eligible pending recipient."""
    now = datetime.now(timezone.utc)
    rows = (
        db.query(Campaign.id)
        .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .filter(
            Campaign.status == "scheduled",
            Campaign.scheduled_at.isnot(None),
            Campaign.scheduled_at <= now,
            CampaignRecipient.status == "pending",
            or_(
                CampaignRecipient.next_retry_at.is_(None),
                CampaignRecipient.next_retry_at <= now,
            ),
        )
        .distinct()
        .order_by(Campaign.id.asc())
        .all()
    )
    return [row[0] for row in rows]


def render_campaign_text(text: str, contact: Contact) -> str:
    """Personalize supported merge fields in an email."""
    values = {
        "first_name": (contact.first_name or "").strip(),
        "last_name": (contact.last_name or "").strip(),
        "name": " ".join(
            part
            for part in (
                (contact.first_name or "").strip(),
                (contact.last_name or "").strip(),
            )
            if part
        ),
        "email": (contact.email or "").strip(),
        "company": (contact.company or "").strip(),
    }
    pattern = r"\{\{\s*(first_name|last_name|name|email|company)\s*\}\}"
    return re.sub(
        pattern,
        lambda match: values[match.group(1).lower()],
        text or "",
        flags=re.IGNORECASE,
    )


def get_retry_time(attempts: int) -> datetime:
    """Return next retry time using capped exponential backoff."""
    delay_minutes = min(2 ** max(0, attempts - 1), 4)
    return datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)


def claim_pending_recipient(
    db: Session,
    record: CampaignRecipient,
    rendered_subject: str,
) -> bool:
    """Atomically claim a pending recipient; this function does not send email."""
    claim = db.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.id == record.id,
            CampaignRecipient.status == "pending",
            CampaignRecipient.attempts < CampaignRecipient.max_attempts,
        )
        .values(
            status="sending",
            attempts=CampaignRecipient.attempts + 1,
            last_attempt_at=datetime.now(timezone.utc),
            rendered_subject=rendered_subject,
            error_message=None,
            next_retry_at=None,
        )
    )
    if claim.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    db.refresh(record)
    return True
