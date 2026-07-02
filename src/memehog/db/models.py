from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
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
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
