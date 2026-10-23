
import base64
from email.message import EmailMessage

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings
from app.security import decrypt_refresh_token


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def send_email(
    *,
    encrypted_refresh_token: str,
    sender_email: str,
    recipient_email: str,
    subject: str,
    body: str,
) -> dict:
    """
    Send one email using the user's encrypted Gmail refresh token.

    Returns Gmail's message ID and thread ID.
    Raises exceptions to the calling API endpoint for safe handling.
    """
    refresh_token = decrypt_refresh_token(encrypted_refresh_token)

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=[GMAIL_SEND_SCOPE],
    )

    gmail = build(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )

    message = EmailMessage()
    message["To"] = recipient_email
    message["From"] = sender_email
    message["Subject"] = subject
    message.set_content(body)

    raw_message = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode("utf-8")

    result = (
        gmail.users()
        .messages()
        .send(
            userId="me",
            body={"raw": raw_message},
        )
        .execute()
    )

    return {
        "gmail_message_id": result.get("id"),
        "thread_id": result.get("threadId"),
    }
