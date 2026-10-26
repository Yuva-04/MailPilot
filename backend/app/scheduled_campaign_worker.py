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

def test_scheduled_campaign_skips_unsubscribed_contact(client, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import scheduled_campaign_worker

    # Create a test contact.
    response = create_contact(
        client,
        email="scheduled-unsubscribed@example.com",
        first_name="Scheduled",
    )
    assert response.status_code == 201, response.text
    contact_id = response.json()["contact"]["id"]

    # Unsubscribe the contact.
    response = client.put(
        f"/contacts/{contact_id}",
        json={"is_unsubscribed": True},
    )
    assert response.status_code == 200, response.text

    # Create a campaign using the existing test helper.
    campaign_id = create_template_and_campaign(client)

    # Put the campaign and recipient into a due scheduled state.
    db = TestingSessionLocal()
    try:
        campaign = db.query(Campaign).filter(
            Campaign.id == campaign_id
        ).first()
        contact = db.query(Contact).filter(
            Contact.id == contact_id
        ).first()

        assert campaign is not None
        assert contact is not None

        campaign.status = "scheduled"
        campaign.scheduled_at = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )

        recipient = CampaignRecipient(
            campaign_id=campaign.id,
            contact_id=contact.id,
            email=contact.email,
            status="pending",
            attempts=0,
            max_attempts=3,
        )
        db.add(recipient)
        db.commit()

    finally:
        db.close()

    # Use the isolated SQLite test database.
    monkeypatch.setattr(
        scheduled_campaign_worker,
        "SessionLocal",
        TestingSessionLocal,
    )

    # Mock email sending: Gmail cannot be contacted by this test.
    with patch("app.main.send_email") as mocked_send:
        scheduled_campaign_worker.run_scheduled_campaign_job()

    mocked_send.assert_not_called()

    # Verify that the recipient was skipped.
    db = TestingSessionLocal()
    try:
        record = db.query(CampaignRecipient).filter(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.contact_id == contact_id,
        ).first()

        assert record is not None
        assert record.status == "skipped"
        assert record.skipped_reason == "Recipient has unsubscribed."
    finally:
        db.close()
