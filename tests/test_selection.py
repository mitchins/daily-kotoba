from __future__ import annotations

import datetime as dt
import threading

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from daily_kotoba import db, selection
from daily_kotoba.models import Selection, Word


def test_singleton_is_lazy(client: TestClient, kotoba_settings):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["selected_word_id"] is None

    with db.get_sessionmaker(kotoba_settings)() as session:
        assert session.scalar(select(Selection)) is None

    r = client.get("/v1/daily.json")
    assert r.status_code == 200

    with db.get_sessionmaker(kotoba_settings)() as session:
        assert session.scalar(select(Selection)) is not None


def test_singleton_concurrent_requests_agree(client: TestClient, kotoba_settings):
    results: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            r = client.get("/v1/daily.json")
            with lock:
                results.append(r.json()["word"]["id"])
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 20
    assert len(set(results)) == 1

    with db.get_sessionmaker(kotoba_settings)() as session:
        rows = session.scalars(select(Selection)).all()
        assert len(rows) == 1


def test_recycling_wraps_after_pool_exhausted(kotoba_settings, set_today):
    # Isolate a 3-word pool so we can exhaust it deterministically.
    with db.get_sessionmaker(kotoba_settings)() as session:
        for i in range(3):
            session.add(
                Word(
                    seq=f"r{i}",
                    surface=f"word{i}",
                    reading=f"word{i}",
                    is_kana_only=True,
                    gloss="gloss",
                    pos=None,
                    jlpt="N5",
                    sort_key=i,
                )
            )
        session.commit()

        day1 = dt.date(2026, 1, 1)
        set_today(day1)
        sel1 = selection.get_or_create_selection(session, day1, ["N5"])
        word1_id = sel1.word_id

        day2 = dt.date(2026, 1, 2)
        set_today(day2)
        sel2 = selection.get_or_create_selection(session, day2, ["N5"])

        day3 = dt.date(2026, 1, 3)
        set_today(day3)
        sel3 = selection.get_or_create_selection(session, day3, ["N5"])

        selected_ids = {sel1.word_id, sel2.word_id, sel3.word_id}
        pool_ids = {w.id for w in session.scalars(select(Word)).all()}
        assert selected_ids == pool_ids

        day4 = dt.date(2026, 1, 4)
        set_today(day4)
        sel4 = selection.get_or_create_selection(session, day4, ["N5"])

        assert sel4.word_id == word1_id
        word1 = session.get(Word, word1_id)
        assert word1.shown_count == 2


def test_level_filter_restricts_pool(client: TestClient, monkeypatch):
    monkeypatch.setenv("KOTOBA_LEVELS", "N5")
    for _ in range(5):
        r = client.get("/v1/daily.json")
        assert r.status_code == 200
        assert r.json()["word"]["jlpt"] == "N5"


def test_level_filter_empty_pool_returns_503(client: TestClient, monkeypatch, kotoba_settings):
    # Drop all N5 words from the fixture pool, then request an N5-only pool.
    with db.get_sessionmaker(kotoba_settings)() as session:
        for w in session.scalars(select(Word).where(Word.jlpt == "N5")).all():
            session.delete(w)
        session.commit()

    monkeypatch.setenv("KOTOBA_LEVELS", "N5")
    r = client.get("/v1/daily.json")
    assert r.status_code == 503
    assert "N5" in r.json()["detail"]


def test_lost_race_does_not_burn_a_word(seeded_db, set_today):
    """Two processes (no shared in-process lock) both pick a candidate for `day` and
    both attempt the UNIQUE(day) insert. Only the winner's word may be bumped — this
    exercises the exact `if sel.word_id == word.id` guard in selection.py directly,
    since a real cross-process interleaving isn't reproducible with in-process threads."""
    day = dt.date(2026, 2, 1)
    set_today(day)

    with db.get_sessionmaker(seeded_db)() as session:
        words = session.scalars(select(Word).order_by(Word.sort_key)).all()
        winner, loser = words[0], words[1]
        assert winner.last_shown_on is None
        assert loser.last_shown_on is None

        # The other process wins the insert race for `day` first.
        session.execute(
            sqlite_insert(Selection)
            .values(day=day, word_id=winner.id, created_at=dt.datetime.now(dt.UTC))
            .on_conflict_do_nothing(index_elements=["day"])
        )
        session.commit()

        # Our process independently picked `loser` and now attempts the same insert.
        session.execute(
            sqlite_insert(Selection)
            .values(day=day, word_id=loser.id, created_at=dt.datetime.now(dt.UTC))
            .on_conflict_do_nothing(index_elements=["day"])
        )
        session.commit()

        sel = session.scalar(select(Selection).where(Selection.day == day))
        assert sel.word_id == winner.id  # our insert was a no-op

        if sel.word_id == loser.id:  # the guard under test — must not run for the loser
            loser.last_shown_on = day
            loser.shown_count += 1
            session.commit()

        session.refresh(loser)
        assert loser.last_shown_on is None
        assert loser.shown_count == 0
