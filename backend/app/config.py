
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str

    GOOGLE_REDIRECT_URI: str = (
        "http://127.0.0.1:8000/auth/google/callback"
    )

    GMAIL_REDIRECT_URI: str = (
        "http://127.0.0.1:8000/auth/gmail/callback"
    )

    SESSION_SECRET_KEY: str
    GMAIL_TOKEN_ENCRYPTION_KEY: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
