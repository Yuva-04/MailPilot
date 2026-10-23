
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    google_sub = Column(String(255), unique=True, nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    name = Column(String(200), nullable=True)
    picture = Column(String(500), nullable=True)

    gmail_refresh_token = Column(Text, nullable=True)
    gmail_refresh_token_encrypted = Column(Text, nullable=True)
    gmail_connected = Column(Boolean, default=False, nullable=False)

    created_at = Column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    contacts = relationship(
        "Contact",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    templates = relationship(
        "EmailTemplate",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    campaigns = relationship(
        "Campaign",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    email = Column(String(320), nullable=False, index=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    company = Column(String(200), nullable=True)

    is_unsubscribed = Column(Boolean, default=False, nullable=False)
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)

    email_validation_status = Column(
        String(30), default="unknown", nullable=False
    )
    email_validation_reason = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    user = relationship("User", back_populates="contacts")
    campaign_recipients = relationship(
        "CampaignRecipient",
        back_populates="contact",
    )


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    name = Column(String(200), nullable=False)
    subject = Column(String(300), nullable=False)
    body = Column(Text, nullable=False)

    created_at = Column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=True,
    )

    user = relationship("User", back_populates="templates")
    campaigns = relationship("Campaign", back_populates="template")


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    template_id = Column(
        Integer,
        ForeignKey("email_templates.id"),
        nullable=False,
    )

    name = Column(String(200), nullable=False)
    status = Column(String(30), default="draft", nullable=False)

    created_at = Column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    user = relationship("User", back_populates="campaigns")
    template = relationship("EmailTemplate", back_populates="campaigns")
    recipients = relationship(
        "CampaignRecipient",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"

    id = Column(Integer, primary_key=True, index=True)

    campaign_id = Column(
        Integer,
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id = Column(
        Integer,
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    email = Column(String(320), nullable=False, index=True)
    status = Column(
        String(30), default="pending", nullable=False, index=True
    )
    attempts = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=3, nullable=False)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)

    rendered_subject = Column(Text, nullable=True)
    provider_message_id = Column(String(255), nullable=True, index=True)
    error_code = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    skipped_reason = Column(Text, nullable=True)

    email_validation_status = Column(
        String(30), default="unknown", nullable=False
    )
    email_validation_reason = Column(Text, nullable=True)

    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    campaign = relationship("Campaign", back_populates="recipients")
    contact = relationship(
        "Contact",
        back_populates="campaign_recipients",
    )
