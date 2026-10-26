from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.scheduled_campaign_worker import run_scheduled_campaign_job


def setup_worker(campaign, recipient=None):
    db = MagicMock()
    campaign_query = MagicMock()
    campaign_query.filter.return_value.first.return_value = campaign

    recipient_query = MagicMock()
    recipient_query.filter.return_value.order_by.return_value.first.return_value = recipient

    db.query.side_effect = [campaign_query, recipient_query]
    return db


@patch("app.scheduled_campaign_worker.SessionLocal")
@patch("app.scheduled_campaign_worker.get_due_campaign_ids")
def test_worker_does_nothing_when_no_campaigns_are_due(
    mock_get_due_campaign_ids, mock_session_local
):
    db = MagicMock()
    mock_session_local.return_value = db
    mock_get_due_campaign_ids.return_value = []

    run_scheduled_campaign_job()

    db.close.assert_called_once()


@patch("app.main._launch_campaign")
@patch("app.main.CampaignLaunchRequest")
@patch("app.scheduled_campaign_worker.SessionLocal")
@patch("app.scheduled_campaign_worker.get_due_campaign_ids")
def test_worker_executes_due_campaign(
    mock_get_due_campaign_ids,
    mock_session_local,
    mock_payload_class,
    mock_launch,
):
    campaign = MagicMock()
    campaign.id = 123
    campaign.user_id = 7
    campaign.status = "scheduled"
    campaign.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    recipient = MagicMock()
    recipient.contact_id = 456

    db = setup_worker(campaign, recipient)
    mock_session_local.return_value = db
    mock_get_due_campaign_ids.return_value = [123]
    mock_launch.return_value = {
        "campaign_status": "scheduled",
        "sent_count": 1,
        "retry_scheduled_count": 0,
    }

    run_scheduled_campaign_job()

    mock_payload_class.assert_called_once_with(
        confirm=False, contact_ids=[456], max_recipients=1
    )
    mock_launch.assert_called_once_with(
        123,
        mock_payload_class.return_value,
        db,
        {"id": 7},
        scheduled=True,
    )
    db.close.assert_called_once()


@patch("app.scheduled_campaign_worker.SessionLocal")
@patch("app.scheduled_campaign_worker.get_due_campaign_ids")
def test_worker_skips_future_campaign(
    mock_get_due_campaign_ids, mock_session_local
):
    campaign = MagicMock()
    campaign.id = 123
    campaign.status = "scheduled"
    campaign.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)

    db = MagicMock()
    campaign_query = MagicMock()
    campaign_query.filter.return_value.first.return_value = campaign
    db.query.return_value = campaign_query

    mock_session_local.return_value = db
    mock_get_due_campaign_ids.return_value = [123]

    with patch("app.main._launch_campaign") as mock_launch:
        run_scheduled_campaign_job()

    mock_launch.assert_not_called()
    db.close.assert_called_once()


@patch("app.main._launch_campaign", side_effect=HTTPException(status_code=429))
@patch("app.main.CampaignLaunchRequest")
@patch("app.scheduled_campaign_worker.SessionLocal")
@patch("app.scheduled_campaign_worker.get_due_campaign_ids")
def test_worker_handles_launch_http_error(
    mock_get_due_campaign_ids,
    mock_session_local,
    mock_payload_class,
    mock_launch,
):
    campaign = MagicMock()
    campaign.id = 123
    campaign.user_id = 7
    campaign.status = "scheduled"
    campaign.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    recipient = MagicMock()
    recipient.contact_id = 456

    db = setup_worker(campaign, recipient)
    mock_session_local.return_value = db
    mock_get_due_campaign_ids.return_value = [123]

    run_scheduled_campaign_job()

    mock_launch.assert_called_once()
    db.rollback.assert_called()
    db.close.assert_called_once()

@patch("app.scheduled_campaign_worker.SessionLocal")
@patch("app.scheduled_campaign_worker.get_due_campaign_ids")
def test_worker_skips_cancelled_campaign(
    mock_get_due_campaign_ids,
    mock_session_local,
):
    campaign = MagicMock()
    campaign.id = 123
    campaign.status = "cancelled"
    campaign.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    db = MagicMock()
    campaign_query = MagicMock()
    campaign_query.filter.return_value.first.return_value = None
    db.query.return_value = campaign_query

    mock_session_local.return_value = db
    mock_get_due_campaign_ids.return_value = [123]

    with patch("app.main._launch_campaign") as mock_launch:
        run_scheduled_campaign_job()

    mock_launch.assert_not_called()
    db.close.assert_called_once()


@patch("app.scheduled_campaign_worker.SessionLocal")
@patch("app.scheduled_campaign_worker.get_due_campaign_ids")
def test_worker_skips_paused_campaign(
    mock_get_due_campaign_ids,
    mock_session_local,
):
    campaign = MagicMock()
    campaign.id = 123
    campaign.status = "paused"
    campaign.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    db = MagicMock()
    campaign_query = MagicMock()
    campaign_query.filter.return_value.first.return_value = None
    db.query.return_value = campaign_query

    mock_session_local.return_value = db
    mock_get_due_campaign_ids.return_value = [123]

    with patch("app.main._launch_campaign") as mock_launch:
        run_scheduled_campaign_job()

    mock_launch.assert_not_called()
    db.close.assert_called_once()


@patch("app.main._launch_campaign")
@patch("app.main.CampaignLaunchRequest")
@patch("app.scheduled_campaign_worker.SessionLocal")
@patch("app.scheduled_campaign_worker.get_due_campaign_ids")
def test_worker_passes_due_recipient_to_launch_suppression_guard(
    mock_get_due_campaign_ids,
    mock_session_local,
    mock_payload_class,
    mock_launch,
):
    campaign = MagicMock()
    campaign.id = 123
    campaign.user_id = 7
    campaign.status = "scheduled"
    campaign.scheduled_at = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    )

    recipient = MagicMock()
    recipient.contact_id = 456

    db = setup_worker(campaign, recipient)
    mock_session_local.return_value = db
    mock_get_due_campaign_ids.return_value = [123]

    mock_launch.return_value = {
        "campaign_status": "scheduled",
        "sent_count": 0,
        "skipped_count": 1,
        "retry_scheduled_count": 0,
    }

    run_scheduled_campaign_job()

    mock_launch.assert_called_once_with(
        123,
        mock_payload_class.return_value,
        db,
        {"id": 7},
        scheduled=True,
    )
    mock_payload_class.assert_called_once_with(
        confirm=False,
        contact_ids=[456],
        max_recipients=1,
    )
    db.close.assert_called_once()
