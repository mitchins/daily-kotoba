"""The daily-selection singleton, and the recycling picker behind it."""

from __future__ import annotations

import datetime as dt
import threading

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from daily_kotoba.models import Selection, Word

_LOCK = threading.Lock()


class EmptyPoolError(Exception):
    """No words match the configured KOTOBA_LEVELS pool."""

    def __init__(self, levels: list[str]) -> None:
        self.levels = levels
        super().__init__(f"no words available for levels: {', '.join(levels)}")


def today() -> dt.date:
    """The process-local "today", honouring TZ. Tests monkeypatch this."""
    return dt.datetime.now().astimezone().date()


def pick_next(session: Session, levels: list[str]) -> Word | None:
    stmt = (
        select(Word)
        .where(Word.jlpt.in_(levels))
        .order_by(Word.last_shown_on.asc().nulls_first(), Word.sort_key.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def get_or_create_selection(session: Session, day: dt.date, levels: list[str]) -> Selection:
    sel = session.scalar(select(Selection).where(Selection.day == day))
    if sel:
        return sel

    with _LOCK:  # avoids an in-process herd; the real guarantee is UNIQUE(day) below
        sel = session.scalar(select(Selection).where(Selection.day == day))
        if sel:
            return sel

        word = pick_next(session, levels)
        if word is None:
            raise EmptyPoolError(levels)

        session.execute(
            sqlite_insert(Selection)
            .values(day=day, word_id=word.id, created_at=dt.datetime.now(dt.UTC))
            .on_conflict_do_nothing(index_elements=["day"])
        )
        session.commit()

        sel = session.scalar(select(Selection).where(Selection.day == day))
        # Only the insert winner bumps last_shown_on — a lost race must not burn a
        # word that never actually got displayed to the day it "almost" won.
        if sel.word_id == word.id:
            word.last_shown_on = day
            word.shown_count += 1
            session.commit()
        return sel
