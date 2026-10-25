# Email Campaign & Mail Merge Tool

## 1. Project Overview

The Email Campaign & Mail Merge Tool is a web application designed to manage contacts, create personalized email templates, import recipients from CSV files, and organize email campaigns through the Gmail API.

The backend is built with FastAPI and PostgreSQL. A Chrome extension provides the user interface for campaign-related workflows.

## 2. Technology Stack

- **Backend:** Python, FastAPI
- **Database:** PostgreSQL
- **ORM and migrations:** SQLAlchemy, Alembic
- **Email integration:** Gmail API
- **Authentication:** Google OAuth 2.0
- **Testing:** pytest
- **Frontend:** HTML, CSS, JavaScript, Chrome extension

## 3. Features

### Contact Management
- Create and manage contacts.
- Validate email addresses.
- Detect duplicate email addresses.
- Enforce user-level contact access.

### CSV Import
- Upload CSV files containing contact information.
- Validate uploaded file types.
- Support contact data imported from structured CSV files.

### Email Templates
- Create reusable email templates.
- Support personalized merge fields.
- Validate required template fields.

### Campaign Management
- Create campaigns associated with email templates.
- Select recipients.
- Run campaign preflight checks.
- Pause or cancel campaigns.
- Track campaign and recipient statuses.

### Gmail Integration
- Authenticate using Google OAuth 2.0.
- Send campaign emails through the Gmail API.
- Handle eligible temporary sending failures.
- Schedule retries for applicable HTTP 429 responses.

### Scheduling and Safety
- Process eligible scheduled campaigns through a background worker.
- Prevent future-dated campaigns from being launched early.
- Skip paused and cancelled campaigns.
- Suppress unsubscribed contacts.
- Keep scheduled campaign processing disabled by default.

## 4. Project Structure

```text
email-campaign-tool/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── models.py
│   │   ├── database.py
│   │   ├── scheduled_campaign_worker.py
│   │   └── services/
│   │       └── campaign_service.py
│   ├── migrations/
│   ├── tests/
│   │   ├── test_main.py
│   │   ├── test_campaign_suppression.py
│   │   └── test_scheduled_campaign_worker.py
│   ├── requirements.txt
│   └── ...
└── README.md
```

The structure above highlights the main backend components. Additional files may exist in the repository.

## 5. Prerequisites

- Python 3.11
- PostgreSQL
- Google Cloud project with Gmail API enabled
- Google OAuth 2.0 credentials
- Git
- Chrome, if using the browser extension

## 6. Installation

Open a terminal in the backend directory.

Create a virtual environment if one does not already exist:

```powershell
python -m venv .venv
```

Activate it in Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Configure the required environment variables using the project's existing environment configuration and example files. Set up the PostgreSQL database and apply the migrations provided by the project.

Do not commit environment files containing credentials or secrets.

## 7. Running the Backend

From the backend directory, activate the virtual environment and run:

```powershell
uvicorn app.main:app --reload
```

The development server should be available at:

`http://127.0.0.1:8000`

FastAPI interactive documentation:

`http://127.0.0.1:8001/docs`

The application requires the appropriate database and environment configuration to run successfully.

## 8. Running Automated Tests

Run the full test suite from the backend directory:

```powershell
python -m pytest tests\ -v
```

### Latest Local Test Results

- **Total tests passed:** 37
- **Failed tests:** 0
- **Warnings:** 3

The tests cover contact validation, campaign preflight checks, unsubscribe suppression, retry handling, campaign status updates, and scheduled worker behavior.

The scheduled worker tests mock the campaign launch function, so they do not send real campaign emails.

These results reflect the local test run and do not independently verify production deployment or live Gmail delivery.

## 9. Scheduler Configuration

Scheduled campaign processing is controlled by the environment variable:

```env
ENABLE_SCHEDULED_CAMPAIGN_CHECKS=false
```

Keep this set to `false` during development and testing.

Before enabling scheduled sending, verify the database configuration, Gmail OAuth credentials, campaign recipients, unsubscribe suppression, retry behavior, and campaign cancellation handling.

Do not test with real recipient lists without authorization.

## 10. Security Considerations

- Keep OAuth credentials, API secrets, and database passwords out of source control.
- Do not commit `.env` files, database dumps, or personal contact information.
- Use synthetic contact data for development and automated tests.
- Verify recipient consent before sending campaign emails.
- Honor unsubscribe and suppression records.
- Test email sending with a dedicated account before any production use.
- Keep scheduled sending disabled until the complete workflow has been verified.

## 11. Limitations and Further Validation

Before using the application in production, verify the complete Chrome extension workflow, Gmail OAuth setup, database migration process, scheduled campaign execution, retry limits, reporting and export functionality, and data deletion behavior against the current source code.

The local passing test suite is an important validation step, but it does not replace end-to-end testing with a dedicated Gmail account and a controlled test recipient.

## 12. Future Improvements

- Expand end-to-end tests for the Chrome extension and Gmail integration.
- Add comprehensive campaign reporting and activity-log verification.
- Improve scheduler recovery and monitoring.
- Document the complete API endpoint catalog.
- Add deployment instructions and operational monitoring.
