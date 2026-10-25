from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import or_

from app.database import SessionLocal
from app.models import Campaign, CampaignRecipient
from app.services.campaign_service import get_due_campaign_ids


def run_scheduled_campaign_job():
    """Execute one eligible recipient per due campaign per scheduler run."""
    db = SessionLocal()

    try:
        due_campaign_ids = get_due_campaign_ids(db)

        for campaign_id in due_campaign_ids:
            try:
                now = datetime.now(timezone.utc)

                campaign = (
                    db.query(Campaign)
                    .filter(
                        Campaign.id == campaign_id,
                        Campaign.status == "scheduled",
                    )
                    .first()
                )

                if campaign is None or campaign.scheduled_at is None:
                    continue

                scheduled_at = campaign.scheduled_at
                if scheduled_at.tzinfo is None:
                    scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

                if scheduled_at > now:
                    continue

                recipient = (
                    db.query(CampaignRecipient)
                    .filter(
                        CampaignRecipient.campaign_id == campaign_id,
                        CampaignRecipient.status == "pending",
                        CampaignRecipient.contact_id.isnot(None),
                        or_(
                            CampaignRecipient.next_retry_at.is_(None),
                            CampaignRecipient.next_retry_at <= now,
                        ),
                    )
                    .order_by(CampaignRecipient.id.asc())
                    .first()
                )

                if recipient is None:
                    continue

                from app.main import CampaignLaunchRequest, _launch_campaign

                payload = CampaignLaunchRequest(
                    confirm=False,
                    contact_ids=[recipient.contact_id],
                    max_recipients=1,
                )

                result = _launch_campaign(
                    campaign_id,
                    payload,
                    db,
                    {"id": campaign.user_id},
                    scheduled=True,
                )

                print(
                    f"Campaign {campaign_id}: "
                    f"status={result.get('campaign_status')}, "
                    f"sent={result.get('sent_count', 0)}, "
                    f"retries={result.get('retry_scheduled_count', 0)}"
                )

            except HTTPException as exc:
                db.rollback()
                print(
                    f"Campaign {campaign_id} was not processed: "
                    f"HTTP {exc.status_code}"
                )

            except Exception as exc:
                db.rollback()
                print(
                    f"Campaign {campaign_id} encountered "
                    f"{type(exc).__name__}; check application logs."
                )

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
