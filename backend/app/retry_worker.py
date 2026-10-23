
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    CampaignRecipient,
    Contact,
    EmailTemplate,
    User,
)


def get_due_retries(db: Session, limit: int = 50):
    """Fetch one due retry at a time while locking its database row."""

    now = datetime.now(timezone.utc)

    return (
        db.query(CampaignRecipient)
        .filter(
            CampaignRecipient.status == "pending",
            CampaignRecipient.next_retry_at.isnot(None),
            CampaignRecipient.next_retry_at <= now,
            CampaignRecipient.attempts < CampaignRecipient.max_attempts,
        )
        .order_by(CampaignRecipient.next_retry_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def validate_retry(db: Session, record: CampaignRecipient):
    """Check whether a recipient is eligible for retry."""

    if record.status != "pending":
        return False, "Recipient is not pending."

    if record.attempts >= record.max_attempts:
        record.status = "failed"
        record.next_retry_at = None
        record.error_message = "Maximum send attempts reached."
        return False, record.error_message

    contact = (
        db.query(Contact)
        .filter(Contact.id == record.contact_id)
        .first()
    )

    campaign = (
        db.query(Campaign)
        .filter(Campaign.id == record.campaign_id)
        .first()
    )

    if not contact or not campaign:
        record.status = "skipped"
        record.next_retry_at = None
        record.skipped_reason = (
            "Contact or campaign no longer exists."
        )
        return False, record.skipped_reason

    if contact.user_id != campaign.user_id:
        record.status = "skipped"
        record.next_retry_at = None
        record.skipped_reason = "Contact ownership mismatch."
        return False, record.skipped_reason

    if campaign.status not in {"sending", "paused"}:
        record.status = "skipped"
        record.next_retry_at = None
        record.skipped_reason = (
            "Campaign is not eligible for retry."
        )
        return False, record.skipped_reason

    recipient_email = (record.email or "").strip().casefold()

    if not recipient_email:
        record.status = "failed"
        record.next_retry_at = None
        record.error_message = "Recipient email is missing."
        return False, record.error_message

    # Suppress this address if any matching contact has unsubscribed.
    matching_contacts = (
        db.query(Contact)
        .filter(Contact.user_id == campaign.user_id)
        .all()
    )

    if any(
        (contact_item.email or "").strip().casefold()
        == recipient_email
        and contact_item.is_unsubscribed
        for contact_item in matching_contacts
    ):
        record.status = "skipped"
        record.next_retry_at = None
        record.skipped_reason = "Recipient has unsubscribed."
        record.error_message = record.skipped_reason
        return False, record.skipped_reason

    return True, "Retry is eligible."


def recover_stuck_sending_records(
    db: Session,
    older_than_minutes: int = 15,
):
    """
    Mark old 'sending' records as failed for manual review.
    Never automatically resend because Gmail may have accepted
    the message before the application crashed.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=older_than_minutes
    )

    records = (
        db.query(CampaignRecipient)
        .filter(
            CampaignRecipient.status == "sending",
            CampaignRecipient.last_attempt_at.isnot(None),
            CampaignRecipient.last_attempt_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )

    recovered_count = 0

    for record in records:
        record.status = "failed"
        record.next_retry_at = None
        record.error_message = (
            "Stale sending record requires manual review. "
            "Check Gmail before retrying; delivery may have occurred."
        )
        recovered_count += 1

    db.commit()
    return recovered_count



def process_due_retries(db: Session, limit: int = 50):
    """
    Process due retries one recipient at a time.

    A recipient is marked as sending and committed before the
    external Gmail API call. Row locking prevents concurrent
    workers from claiming the same pending recipient.
    """

    from app.main import (
        get_retry_time,
        render_campaign_text,
        send_email,
    )

    results = []

    for _ in range(limit):
        # Claim one eligible record in its own transaction.
        try:
            record = get_due_retries(db)

            if record is None:
                db.rollback()
                break

            eligible, reason = validate_retry(db, record)

            if not eligible:
                results.append(
                    {
                        "recipient_id": record.id,
                        "status": record.status,
                        "reason": reason,
                    }
                )
                db.commit()
                continue

            campaign = (
                db.query(Campaign)
                .filter(Campaign.id == record.campaign_id)
                .first()
            )

            contact = (
                db.query(Contact)
                .filter(Contact.id == record.contact_id)
                .first()
            )

            if not campaign or not contact:
                record.status = "skipped"
                record.next_retry_at = None
                record.skipped_reason = (
                    "Contact or campaign no longer exists."
                )
                db.commit()

                results.append(
                    {
                        "recipient_id": record.id,
                        "status": "skipped",
                        "reason": record.skipped_reason,
                    }
                )
                continue

            template = (
                db.query(EmailTemplate)
                .filter(
                    EmailTemplate.id == campaign.template_id,
                    EmailTemplate.user_id == campaign.user_id,
                )
                .first()
            )

            user = (
                db.query(User)
                .filter(User.id == campaign.user_id)
                .first()
            )

            if (
                not template
                or not (template.subject or "").strip()
                or not (template.body or "").strip()
                or not user
                or not user.gmail_refresh_token
            ):
                record.status = "failed"
                record.next_retry_at = None
                record.error_message = (
                    "Retry stopped: template or Gmail connection "
                    "is unavailable."
                )
                db.commit()

                results.append(
                    {
                        "recipient_id": record.id,
                        "status": "failed",
                        "reason": record.error_message,
                    }
                )
                continue

            # Recheck suppression immediately before claiming.
            eligible, reason = validate_retry(db, record)

            if not eligible:
                results.append(
                    {
                        "recipient_id": record.id,
                        "status": record.status,
                        "reason": reason,
                    }
                )
                db.commit()
                continue

            # Save all values needed after the database commit.
            recipient_id = record.id
            refresh_token = user.gmail_refresh_token
            sender_email = user.email
            recipient_email = record.email.strip()
            rendered_subject = render_campaign_text(
                template.subject,
                contact,
            )
            rendered_body = render_campaign_text(
                template.body,
                contact,
            )

            # Claim only this recipient and release its row lock.
            record.status = "sending"
            record.attempts += 1
            record.last_attempt_at = datetime.now(timezone.utc)
            record.next_retry_at = None
            record.rendered_subject = rendered_subject
            record.error_message = None

            db.commit()

        except Exception:
            db.rollback()
            raise

        # Call Gmail outside the database transaction.
        try:
            gmail_result = send_email(
                encrypted_refresh_token=refresh_token,
                sender_email=sender_email,
                recipient_email=recipient_email,
                subject=rendered_subject,
                body=rendered_body,
            )

        except Exception as exc:
            response = getattr(exc, "resp", None)
            status_code = getattr(response, "status", None)

            try:
                record = (
                    db.query(CampaignRecipient)
                    .filter(
                        CampaignRecipient.id == recipient_id
                    )
                    .with_for_update()
                    .first()
                )

                if record is None:
                    db.rollback()
                    results.append(
                        {
                            "recipient_id": recipient_id,
                            "status": "uncertain",
                            "reason": (
                                "Recipient record could not be "
                                "reloaded after the Gmail error."
                            ),
                        }
                    )
                    continue

                if (
                    status_code == 429
                    and record.attempts < record.max_attempts
                ):
                    record.status = "pending"
                    record.next_retry_at = get_retry_time(
                        record.attempts
                    )
                    record.error_message = (
                        "Gmail returned HTTP 429. Retry scheduled "
                        f"for {record.next_retry_at.isoformat()}."
                    )

                    retry_at = record.next_retry_at.isoformat()

                    db.commit()

                    results.append(
                        {
                            "recipient_id": recipient_id,
                            "status": "retry_scheduled",
                            "retry_at": retry_at,
                        }
                    )

                else:
                    # Never automatically resend an uncertain result.
                    record.status = "failed"
                    record.next_retry_at = None
                    record.error_message = (
                        "Retry failed or outcome is uncertain. "
                        "Check Gmail before manually retrying. "
                        f"Error type: {type(exc).__name__}"
                    )

                    reason = record.error_message
                    db.commit()

                    results.append(
                        {
                            "recipient_id": recipient_id,
                            "status": "failed",
                            "reason": reason,
                        }
                    )

            except Exception:
                db.rollback()
                raise

            continue

        # Gmail returned successfully. Save the delivery result.
        try:
            record = (
                db.query(CampaignRecipient)
                .filter(CampaignRecipient.id == recipient_id)
                .with_for_update()
                .first()
            )

            if record is None:
                db.rollback()
                results.append(
                    {
                        "recipient_id": recipient_id,
                        "status": "uncertain",
                        "reason": (
                            "Gmail accepted the request, but the "
                            "recipient record could not be found."
                        ),
                    }
                )
                continue

            record.status = "sent"
            record.provider_message_id = gmail_result.get(
                "gmail_message_id"
            )
            record.sent_at = datetime.now(timezone.utc)
            record.next_retry_at = None
            record.error_message = None

            message_id = record.provider_message_id
            db.commit()

            results.append(
                {
                    "recipient_id": recipient_id,
                    "status": "sent",
                    "gmail_message_id": message_id,
                }
            )

        except Exception:
            # Gmail may already have accepted the message.
            # Do not automatically resend after a database error.
            db.rollback()
            raise

    return {
        "processed_count": len(results),
        "results": results,
    }
