"""SQLAlchemy 2.0 declarative models."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Word(Base):
    __tablename__ = "word"

    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[str] = mapped_column(unique=True, index=True)
    surface: Mapped[str]
    reading: Mapped[str]
    is_kana_only: Mapped[bool]
    gloss: Mapped[str]
    pos: Mapped[str | None]
    jlpt: Mapped[str] = mapped_column(index=True)
    sort_key: Mapped[int]
    last_shown_on: Mapped[dt.date | None] = mapped_column(index=True)
    shown_count: Mapped[int] = mapped_column(default=0)

    __table_args__ = (Index("ix_word_picker", "jlpt", "last_shown_on", "sort_key"),)


class Selection(Base):
    __tablename__ = "selection"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[dt.date] = mapped_column(unique=True, index=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("word.id"))
    created_at: Mapped[dt.datetime]


class Meta(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]
