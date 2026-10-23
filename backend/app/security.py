
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _get_fernet() -> Fernet:
    """Create a Fernet instance using the configured encryption key."""
    return Fernet(settings.GMAIL_TOKEN_ENCRYPTION_KEY.encode("utf-8"))


def encrypt_refresh_token(refresh_token: str) -> str:
    """Encrypt a Gmail OAuth refresh token before database storage."""
    if not refresh_token:
        raise ValueError("Refresh token cannot be empty.")

    return _get_fernet().encrypt(
        refresh_token.encode("utf-8")
    ).decode("utf-8")


def decrypt_refresh_token(encrypted_token: str) -> str:
    """Decrypt a stored Gmail OAuth refresh token."""
    if not encrypted_token:
        raise ValueError("Encrypted refresh token cannot be empty.")

    try:
        return _get_fernet().decrypt(
            encrypted_token.encode("utf-8")
        ).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Unable to decrypt the Gmail refresh token. "
            "Check the encryption key."
        ) from exc
