from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Item(Base):
    """A single media file in the library."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Path relative to the library dir, e.g. "2026/07/ab12cd34ef56ab78.jpg"
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(16))  # image | video | animation
    mime: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(Text, default=None)
    origin: Mapped[str] = mapped_column(String(16))  # telegram | web | api
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    uploader: Mapped[str | None] = mapped_column(String(128), default=None)
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    duration: Mapped[float | None] = mapped_column(Float, default=None)
    thumb_filename: Mapped[str | None] = mapped_column(String(255), default=None)
    # pending → the nightly indexer (OCR / embeddings, future) should process it
    index_status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tags: Mapped[list[Tag]] = relationship(
        secondary="item_tags", back_populates="items", lazy="selectin"
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    items: Mapped[list[Item]] = relationship(
        secondary="item_tags", back_populates="tags", lazy="selectin"
    )


class ItemTag(Base):
    __tablename__ = "item_tags"

    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    # Who attached the tag: "user" (bot caption, web UI) or "ai" (the indexer).
    source: Mapped[str] = mapped_column(String(8), default="user")


class Embedding(Base):
    """Vector embeddings for semantic search (populated by a future indexer)."""

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(64))
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VlmProfile(Base):
    """A saved vision-model connection (endpoint + model + API key).

    Every profile with `active` set is run by the nightly indexer, each
    keeping its own copy of OCR/description text (`vlm_texts` + `vlm_fts`) —
    search can then use one model's data or all of them, and one provider
    having a bad night doesn't leave memes unindexed. The benchmark runs
    across all saved profiles regardless of `active`."""

    __tablename__ = "vlm_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(Text)
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VlmText(Base):
    """One model's OCR+description blob for one item.

    The searchable copy lives in the `vlm_fts` FTS5 table; this row is the
    source of truth — its presence means the profile has processed the item,
    which is how the indexer builds its per-profile work queue."""

    __tablename__ = "vlm_texts"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("vlm_profiles.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VlmSample(Base):
    """One vision model's take on one meme, produced by the settings-page
    benchmark — stored so different models can be compared side by side."""

    __tablename__ = "vlm_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    model_label: Mapped[str] = mapped_column(String(128))
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """Runtime-editable settings (web UI) that override .env defaults."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class TelegramClient(Base):
    """Additional Telegram users allowed to use the bot (besides the owner
    IDs from ALLOWED_TELEGRAM_IDS). Rows are created either manually in the
    web settings (status=approved) or via /register (status=pending)."""

    __tablename__ = "telegram_clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), default=None)
    note: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Submission(Base):
    """A meme sent by a non-whitelisted Telegram user, quarantined in the
    pending dir until the owner votes 👍/👎. Only on approval does the file
    get ingested into the library proper."""

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # Path relative to the pending dir (quarantine, not the library).
    filename: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    submitter_id: Mapped[int] = mapped_column(index=True)
    submitter_name: Mapped[str | None] = mapped_column(String(128), default=None)
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending | approved | rejected
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL"), default=None
    )
    # JSON list of [chat_id, message_id] pairs of the owner vote messages,
    # so they can be edited once a decision is made.
    vote_msgs: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    """A queued URL download request."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending | running | done | duplicate | error
    error: Mapped[str | None] = mapped_column(Text, default=None)
    origin: Mapped[str] = mapped_column(String(16))  # telegram | web | api
    requested_by: Mapped[str | None] = mapped_column(String(128), default=None)
    # Ingest the downloaded files straight into the spicy stash.
    spicy: Mapped[bool] = mapped_column(Boolean, default=False)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
