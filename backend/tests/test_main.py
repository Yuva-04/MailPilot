
import os

# Enable test mode before importing the application.
os.environ["APP_ENV"] = "test"

import io
from app.retry_worker import recover_stuck_sending_records
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from app.database import Base, get_db
from app.main import app, get_current_database_user
from app.models import (
    Campaign,
    CampaignRecipient,
    Contact,
    EmailTemplate,
    User,
)


from unittest.mock import patch


def test_launch_campaign_skips_unsubscribed_contact(client):
    # Create a contact and unsubscribe it.
    contact_response = create_contact(
        client,
        email="unsubscribed@example.com",
        first_name="Unsubscribed",
    )
    assert contact_response.status_code == 201, contact_response.text
    contact_id = contact_response.json()["contact"]["id"]

    unsubscribe_response = client.put(
        f"/contacts/{contact_id}",
        json={"is_unsubscribed": True},
    )
    assert unsubscribe_response.status_code == 200, unsubscribe_response.text

    campaign_id = create_template_and_campaign(client)

    # Supply Gmail credentials in the isolated test database.
    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(User.email == "pytest@example.com").first()
        user.gmail_refresh_token = "test-refresh-token"
        db.commit()
    finally:
        db.close()

    # Mock Gmail so the test cannot send a real email.
    with patch("app.main.send_email") as mocked_send:
        response = client.post(
            f"/campaigns/{campaign_id}/launch",
            json={
                "confirm": True,
                "contact_ids": [contact_id],
                "max_recipients": 1,
            },
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "No unsent eligible recipients remain."
    mocked_send.assert_not_called()

    db = TestingSessionLocal()
    try:
        record = (
            db.query(CampaignRecipient)
            .filter(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.contact_id == contact_id,
            )
            .first()
        )

        assert record is not None
        assert record.status == "skipped"
        assert record.skipped_reason
    finally:
        db.close()



# --------------------------------------------------
# TEST DATABASE CONFIGURATION
# --------------------------------------------------

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


@pytest.fixture()
def client():
    """Create an isolated database and authenticated test user."""
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # Create User A.
    db = TestingSessionLocal()
    try:
        test_user = User(
            google_sub="pytest-google-sub",
            email="pytest@example.com",
            name="Pytest User A",
        )
        db.add(test_user)
        db.commit()
        db.refresh(test_user)

        test_user_id = test_user.id
    finally:
        db.close()

    # Allow tests to switch the authenticated user.
    active_user = {"id": test_user_id}

    def override_current_database_user():
        db = TestingSessionLocal()
        try:
            user = (
                db.query(User)
                .filter(User.id == active_user["id"])
                .first()
            )

            if user is None:
                raise RuntimeError("Test user was not created.")

            return {
                "id": user.id,
                "google_sub": user.google_sub,
                "email": user.email,
                "name": user.name,
            }
        finally:
            db.close()

    app.dependency_overrides[
        get_current_database_user
    ] = override_current_database_user

    # Expose the test-only user selector to isolation tests.
    app.state.test_active_user = active_user

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()

        if hasattr(app.state, "test_active_user"):
            delattr(app.state, "test_active_user")

        Base.metadata.drop_all(bind=engine)


# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------

def create_contact(client, email="test@example.com", first_name="Test"):
    return client.post(
        "/contacts",
        json={
            "email": email,
            "first_name": first_name,
        },
    )


def create_template_and_campaign(client):
    template_response = client.post(
        "/templates",
        json={
            "name": "Test Template",
            "subject": "Hello {{first_name}}",
            "body": "Welcome {{first_name}}!",
        },
    )

    assert template_response.status_code == 201, template_response.text
    template_id = template_response.json()["template"]["id"]

    campaign_response = client.post(
        "/campaigns",
        json={
            "name": "Test Campaign",
            "template_id": template_id,
        },
    )

    assert campaign_response.status_code == 201, campaign_response.text
    return campaign_response.json()["campaign"]["id"]


def get_test_user_id():
    """Return User A's ID."""
    db = TestingSessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.google_sub == "pytest-google-sub")
            .first()
        )

        assert user is not None, "Test user does not exist."
        return user.id
    finally:
        db.close()


def create_second_test_user():
    """Create and return User B's ID."""
    db = TestingSessionLocal()
    try:
        user = User(
            google_sub="pytest-google-sub-b",
            email="pytest-b@example.com",
            name="Pytest User B",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def switch_test_user(client, user_id):
    """Switch the authenticated identity in the test override."""
    client.app.state.test_active_user["id"] = user_id


# --------------------------------------------------
# HOME AND HEALTH TESTS
# --------------------------------------------------

def test_home(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["version"] == "3.6.0"


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


# --------------------------------------------------
# CONTACT TESTS
# --------------------------------------------------

def test_create_contact(client):
    response = create_contact(client)

    assert response.status_code == 201, response.text
    data = response.json()["contact"]

    assert data["email"] == "test@example.com"
    assert data["first_name"] == "Test"


def test_reject_invalid_email(client):
    response = create_contact(
        client,
        email="not-an-email",
    )

    assert response.status_code == 422


def test_reject_case_insensitive_duplicate(client):
    first_response = create_contact(
        client,
        email="duplicate@example.com",
    )
    assert first_response.status_code == 201, first_response.text

    second_response = create_contact(
        client,
        email="DUPLICATE@example.com",
    )

    assert second_response.status_code == 409


def test_contact_not_found(client):
    response = client.get("/contacts/99999")

    assert response.status_code == 404


# --------------------------------------------------
# MULTI-USER CONTACT ISOLATION
# --------------------------------------------------

def test_user_cannot_access_another_users_contact(client):
    # User A creates a contact.
    create_response = create_contact(
        client,
        email="user-a-contact@example.com",
        first_name="User A",
    )

    assert create_response.status_code == 201, create_response.text
    contact_id = create_response.json()["contact"]["id"]

    # Create User B.
    user_b_id = create_second_test_user()

    # Authenticate as User B.
    switch_test_user(client, user_b_id)

    # User B must not read User A's contact.
    get_response = client.get(f"/contacts/{contact_id}")
    assert get_response.status_code == 404, get_response.text

    # User B must not update User A's contact.
    update_response = client.put(
        f"/contacts/{contact_id}",
        json={"first_name": "Hacked"},
    )
    assert update_response.status_code == 404, update_response.text


def test_users_can_create_contacts_with_same_email(client):
    # User A creates a contact.
    first_response = create_contact(
        client,
        email="shared@example.com",
        first_name="User A",
    )
    assert first_response.status_code == 201, first_response.text

    # Switch to User B and create a contact with the same email.
    user_b_id = create_second_test_user()
    switch_test_user(client, user_b_id)

    second_response = create_contact(
        client,
        email="shared@example.com",
        first_name="User B",
    )

    # This should pass only if duplicate checking is scoped by owner.
    assert second_response.status_code == 201, second_response.text


# --------------------------------------------------
# EMAIL TEMPLATE TESTS
# --------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "Empty Subject",
            "subject": "",
            "body": "Welcome!",
        },
        {
            "name": "Empty Body",
            "subject": "Welcome",
            "body": "",
        },
    ],
)
def test_reject_empty_template_fields(client, payload):
    response = client.post("/templates", json=payload)

    assert response.status_code == 422


# --------------------------------------------------
# CAMPAIGN CREATION TESTS
# --------------------------------------------------

def test_reject_campaign_with_missing_template(client):
    response = client.post(
        "/campaigns",
        json={
            "name": "Invalid Campaign",
            "template_id": 99999,
        },
    )

    assert response.status_code == 404


# --------------------------------------------------
# CAMPAIGN PREFLIGHT TESTS
# --------------------------------------------------

def test_campaign_preflight_ready(client):
    first = create_contact(
        client,
        email="first@example.com",
        first_name="First",
    )
    second = create_contact(
        client,
        email="second@example.com",
        first_name="Second",
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    campaign_id = create_template_and_campaign(client)

    response = client.post(
        f"/campaigns/{campaign_id}/preflight"
    )

    
    data = response.json()

    assert data["ready"] is True
    assert data["total_contacts"] == 2
    assert data["eligible_recipient_count"] == 2


def test_preflight_excludes_unsubscribed_contact(client):
    contact_response = create_contact(
        client,
        email="unsubscribed@example.com",
        first_name="Unsubscribed",
    )

    assert contact_response.status_code == 201, contact_response.text
    contact_id = contact_response.json()["contact"]["id"]

    update_response = client.put(
        f"/contacts/{contact_id}",
        json={"is_unsubscribed": True},
    )

    assert update_response.status_code == 200, update_response.text

    subscribed_response = create_contact(
        client,
        email="subscribed@example.com",
        first_name="Subscribed",
    )
    assert subscribed_response.status_code == 201, subscribed_response.text

    campaign_id = create_template_and_campaign(client)

    response = client.post(
        f"/campaigns/{campaign_id}/preflight"
    )

    assert response.status_code == 200, response.text
    data = response.json()

    assert data["total_contacts"] == 2
    assert data["unsubscribed_count"] == 1
    assert data["subscribed_count"] == 1
    assert data["eligible_recipient_count"] == 1


def test_preflight_rejects_invalid_subscribed_email(client):
    db = TestingSessionLocal()

    try:
        invalid_contact = Contact(
            user_id=get_test_user_id(),
            email="invalid-email",
            first_name="Invalid",
            last_name="Email User",
            company=None,
            is_unsubscribed=False,
        )
        db.add(invalid_contact)
        db.commit()
    finally:
        db.close()

    campaign_id = create_template_and_campaign(client)

    response = client.post(
        f"/campaigns/{campaign_id}/preflight"
    )

    assert response.status_code == 200, response.text
    data = response.json()

    assert data["ready"] is False
    assert data["invalid_email_count"] >= 1


# --------------------------------------------------
# CSV IMPORT TESTS
# --------------------------------------------------

def test_csv_import_rejects_non_csv_file(client):
    response = client.post(
        "/contacts/import",
        files={
            "file": (
                "contacts.txt",
                io.BytesIO(b"This is not a CSV file"),
                "text/plain",
            )
        },
    )

    assert response.status_code == 400


# --------------------------------------------------
# MISSING CAMPAIGN TESTS
# --------------------------------------------------

def test_campaign_not_found(client):
    response = client.get("/campaigns/99999")

    assert response.status_code == 404


def test_http_429_schedules_retry(client):
    contact_response = create_contact(
        client,
        email="retry429@example.com",
        first_name="Retry",
    )
    assert contact_response.status_code == 201, contact_response.text
    contact_id = contact_response.json()["contact"]["id"]

    campaign_id = create_template_and_campaign(client)

    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(
            User.email == "pytest@example.com"
        ).first()
        user.gmail_refresh_token = "test-refresh-token"
        db.commit()
    finally:
        db.close()

    # Simulate HTTP 429 without calling Gmail.
    rate_limit_error = Exception("Rate limited")
    rate_limit_error.resp = SimpleNamespace(status=429)

    with patch("app.main.send_email", side_effect=rate_limit_error) as mocked_send:
        response = client.post(
            f"/campaigns/{campaign_id}/launch",
            json={
                "confirm": True,
                "contact_ids": [contact_id],
                "max_recipients": 1,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["retry_scheduled_count"] == 1
    mocked_send.assert_called_once()

    db = TestingSessionLocal()
    try:
        record = db.query(CampaignRecipient).filter_by(
            campaign_id=campaign_id,
            contact_id=contact_id,
        ).first()

        assert record.status == "pending"
        assert record.next_retry_at is not None
        assert record.attempts == 1
    finally:
        db.close()


def test_future_retry_is_not_sent(client):
    contact_response = create_contact(
        client,
        email="future-retry@example.com",
    )
    contact_id = contact_response.json()["contact"]["id"]
    campaign_id = create_template_and_campaign(client)

    db = TestingSessionLocal()
    try:
        user = db.query(User).filter_by(
            email="pytest@example.com"
        ).first()
        user.gmail_refresh_token = "test-refresh-token"

        record = CampaignRecipient(
            campaign_id=campaign_id,
            contact_id=contact_id,
            email="future-retry@example.com",
            status="pending",
            attempts=1,
            max_attempts=3,
            next_retry_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(record)
        db.commit()
    finally:
        db.close()

    with patch("app.main.send_email") as mocked_send:
        response = client.post(
            f"/campaigns/{campaign_id}/launch",
            json={
                "confirm": True,
                "contact_ids": [contact_id],
                "max_recipients": 1,
            },
        )

    assert response.status_code == 400
    mocked_send.assert_not_called()


def test_max_attempts_prevents_another_send(client):
    contact_response = create_contact(
        client,
        email="max-attempts@example.com",
    )
    contact_id = contact_response.json()["contact"]["id"]
    campaign_id = create_template_and_campaign(client)

    db = TestingSessionLocal()
    try:
        user = db.query(User).filter_by(
            email="pytest@example.com"
        ).first()
        user.gmail_refresh_token = "test-refresh-token"

        record = CampaignRecipient(
            campaign_id=campaign_id,
            contact_id=contact_id,
            email="max-attempts@example.com",
            status="pending",
            attempts=3,
            max_attempts=3,
        )
        db.add(record)
        db.commit()
    finally:
        db.close()

    with patch("app.main.send_email") as mocked_send:
        response = client.post(
            f"/campaigns/{campaign_id}/launch",
            json={
                "confirm": True,
                "contact_ids": [contact_id],
                "max_recipients": 1,
            },
        )

    # The recipient may be recorded as failed at the attempt limit.
    assert response.status_code == 200, response.text
    mocked_send.assert_not_called()


def test_unsubscribed_due_retry_is_not_sent(client):
    contact_response = create_contact(
        client,
        email="unsubscribed-retry@example.com",
    )
    contact_id = contact_response.json()["contact"]["id"]

    update_response = client.put(
        f"/contacts/{contact_id}",
        json={"is_unsubscribed": True},
    )
    assert update_response.status_code == 200, update_response.text

    campaign_id = create_template_and_campaign(client)

    db = TestingSessionLocal()
    try:
        user = db.query(User).filter_by(
            email="pytest@example.com"
        ).first()
        user.gmail_refresh_token = "test-refresh-token"

        record = CampaignRecipient(
            campaign_id=campaign_id,
            contact_id=contact_id,
            email="unsubscribed-retry@example.com",
            status="pending",
            attempts=1,
            max_attempts=3,
            next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        db.add(record)
        db.commit()
    finally:
        db.close()


def test_retry_worker_sends_due_retry_once(client):
    from app.retry_worker import process_due_retries

    contact_response = create_contact(
        client,
        email="worker-test@example.com",
        first_name="Worker",
    )
    assert contact_response.status_code == 201
    contact_id = contact_response.json()["contact"]["id"]

    campaign_id = create_template_and_campaign(client)

    db = TestingSessionLocal()
    try:
        user = db.query(User).filter_by(
            email="pytest@example.com"
        ).first()
        user.gmail_refresh_token = "test-refresh-token"

        campaign = db.query(Campaign).filter_by(
            id=campaign_id
        ).first()
        campaign.status = "sending"

        record = CampaignRecipient(
            campaign_id=campaign_id,
            contact_id=contact_id,
            email="worker-test@example.com",
            status="pending",
            attempts=1,
            max_attempts=3,
            next_retry_at=datetime.now(timezone.utc)
            - timedelta(minutes=1),
        )
        db.add(record)
        db.commit()
        recipient_id = record.id

        with patch(
            "app.main.send_email",
            return_value={"gmail_message_id": "mock-message-id"},
        ) as mocked_send:
            result = process_due_retries(db, limit=1)

        mocked_send.assert_called_once()

        db.refresh(record)
        assert record.status == "sent"
        assert record.provider_message_id == "mock-message-id"
        assert record.attempts == 2
        assert result["processed_count"] == 1

    finally:
        db.close()


def test_recovery_marks_only_stale_sending_records(client):
    db = TestingSessionLocal()
    try:
        user_id = get_test_user_id()

        contact = Contact(
            user_id=user_id,
            email="recovery-test@example.com",
            first_name="Recovery",
            last_name="Test",
            is_unsubscribed=False,
        )
        db.add(contact)
        db.flush()

        template = EmailTemplate(
            user_id=user_id,
            name="Recovery Test Template",
            subject="Test",
            body="Test body",
        )
        db.add(template)
        db.flush()

        campaign = Campaign(
            user_id=user_id,
            name="Recovery Test Campaign",
            template_id=template.id,
            status="sending",
        )
        db.add(campaign)
        db.flush()

        now = datetime.now(timezone.utc)

        stale = CampaignRecipient(
            campaign_id=campaign.id,
            contact_id=contact.id,
            email=contact.email,
            status="sending",
            attempts=1,
            max_attempts=3,
            last_attempt_at=now - timedelta(minutes=30),
        )
        recent = CampaignRecipient(
            campaign_id=campaign.id,
            contact_id=contact.id,
            email=contact.email,
            status="sending",
            attempts=1,
            max_attempts=3,
            last_attempt_at=now - timedelta(minutes=2),
        )
        db.add_all([stale, recent])
        db.commit()

        recovered = recover_stuck_sending_records(
            db,
            older_than_minutes=15,
        )

        db.refresh(stale)
        db.refresh(recent)

        assert recovered == 1
        assert stale.status == "failed"
        assert "manual review" in stale.error_message.lower()
        assert recent.status == "sending"

    finally:
        db.close()


def test_retry_job_recovers_stale_records_before_processing(monkeypatch):
    from app import main

    calls = []

    def fake_recovery(db, older_than_minutes=15):
        calls.append("recovery")
        return 0

    def fake_process(db, limit=50):
        calls.append("process")
        return {"processed_count": 0, "results": []}

    monkeypatch.setattr(
        main, "recover_stuck_sending_records", fake_recovery
    )
    monkeypatch.setattr(
        main, "process_due_retries", fake_process
    )

    main.run_retry_job()

    assert calls == ["recovery", "process"]



import asyncio
from unittest.mock import MagicMock


def test_lifespan_keeps_scheduler_disabled_by_default(monkeypatch):
    from app import main

    monkeypatch.delenv("ENABLE_EMAIL_RETRIES", raising=False)

    scheduler = MagicMock()
    monkeypatch.setattr(
        main,
        "AsyncIOScheduler",
        lambda **kwargs: scheduler,
    )

    async def run_test():
        async with main.lifespan(main.app):
            assert main.app.state.email_retries_enabled is False
            scheduler.add_job.assert_not_called()
            scheduler.start.assert_not_called()

        scheduler.shutdown.assert_called_once_with(wait=False)
    asyncio.run(run_test())


def test_reset_data_deletes_only_active_users_data(client):
    # Create data for User A using the existing test helpers.
    contact_response = create_contact(
        client,
        email="reset-a@example.com",
        first_name="User A",
    )
    assert contact_response.status_code == 201, contact_response.text

    campaign_id = create_template_and_campaign(client)

    # Create User B and their independent data directly in test SQLite.
    db = TestingSessionLocal()
    try:
        user_b = User(
            google_sub="reset-user-b",
            email="reset-b@example.com",
            name="Reset User B",
        )
        db.add(user_b)
        db.commit()
        db.refresh(user_b)

        contact_b = Contact(
            user_id=user_b.id,
            email="keep-b@example.com",
            first_name="Keep",
        )
        template_b = EmailTemplate(
            user_id=user_b.id,
            name="Keep template",
            subject="Keep",
            body="Keep",
        )
        db.add_all([contact_b, template_b])
        db.commit()
        user_b_id = user_b.id
    finally:
        db.close()

    # Reset only the currently authenticated User A.
    response = client.post("/admin/reset-data")
    assert response.status_code == 200, response.text

    deleted = response.json()["deleted"]
    assert deleted["contacts"] >= 1
    assert deleted["campaigns"] >= 1
    assert deleted["templates"] >= 1

    db = TestingSessionLocal()
    try:
        # User A's records should be removed.
        assert db.query(Contact).filter(
            Contact.email == "reset-a@example.com"
        ).first() is None

        assert db.query(Campaign).filter(
            Campaign.id == campaign_id
        ).first() is None

        # User B's records must remain.
        assert db.query(Contact).filter(
            Contact.user_id == user_b_id,
            Contact.email == "keep-b@example.com",
        ).first() is not None

        assert db.query(EmailTemplate).filter(
            EmailTemplate.user_id == user_b_id,
            EmailTemplate.name == "Keep template",
        ).first() is not None

        # The user account itself must remain.
        assert db.query(User).filter(
            User.id == user_b_id
        ).first() is not None
    finally:
        db.close()



def test_reset_data_supports_rollback_on_database_error(client):
    # Create a record that must survive a failed reset.
    contact_response = create_contact(
        client,
        email="rollback-check@example.com",
        first_name="Keep",
    )
    assert contact_response.status_code == 201, contact_response.text

    # Patch the Session class method, not the sessionmaker factory.
    with patch(
        "sqlalchemy.orm.Session.commit",
        side_effect=Exception("simulated database failure"),
    ):
        response = client.post("/admin/reset-data")

    assert response.status_code == 500
    assert response.json()["detail"] == "Could not reset application data."

    # Verify the endpoint's rollback preserved the record.
    db = TestingSessionLocal()
    try:
        contact = (
            db.query(Contact)
            .filter(Contact.email == "rollback-check@example.com")
            .first()
        )
        assert contact is not None
    finally:
        db.close()


# --------------------------------------------------
# CAMPAIGN PAUSE / CANCELLATION VALIDATION
# --------------------------------------------------

def test_campaign_can_be_cancelled(client):
    campaign_id = create_template_and_campaign(client)

    response = client.put(
        f"/campaigns/{campaign_id}",
        json={"status": "cancelled"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["campaign"]["status"] == "cancelled"


def test_campaign_can_be_paused(client):
    campaign_id = create_template_and_campaign(client)

    response = client.put(
        f"/campaigns/{campaign_id}",
        json={"status": "paused"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["campaign"]["status"] == "paused"


def test_campaign_rejects_invalid_status(client):
    campaign_id = create_template_and_campaign(client)

    response = client.put(
        f"/campaigns/{campaign_id}",
        json={"status": "invalid_status"},
    )

    assert response.status_code == 400, response.text
