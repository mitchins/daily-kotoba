from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daily_kotoba import db, selection
from daily_kotoba.api import app
from daily_kotoba.config import Settings, get_settings
from daily_kotoba.models import Word

# (seq, surface, reading, is_kana_only, gloss, pos, jlpt, sort_key)
FIXTURE_WORDS = [
    ("1", "楽しい", "たのしい", False, "fun, enjoyable, pleasant", "i-adjective", "N4", 10),
    ("2", "食べる", "たべる", False, "to eat", "verb", "N5", 20),
    ("3", "ありがとう", "ありがとう", True, "thank you", "expression", "N5", 30),
    ("4", "静か", "しずか", False, "quiet, still, calm", "na-adjective", "N4", 40),
    ("5", "難しい", "むずかしい", False, "difficult, hard, troublesome", "i-adjective", "N3", 50),
    ("6", "経済", "けいざい", False, "economy, economics", "noun", "N2", 60),
    ("7", "曖昧", "あいまい", False, "vague, ambiguous, obscure", "na-adjective", "N1", 70),
    ("8", "犬", "いぬ", False, "dog", "noun", "N5", 80),
    ("9", "美しい", "うつくしい", False, "beautiful, lovely", "i-adjective", "N3", 90),
    ("10", "こんにちは", "こんにちは", True, "hello, good afternoon", None, "N5", 100),
]


def _find_font_dir() -> Path | None:
    env = os.environ.get("KOTOBA_FONT_DIR")
    if not env:
        return None
    p = Path(env)
    if (p / "NotoSansJP-Regular.otf").exists() and (p / "NotoSansJP-Bold.otf").exists():
        return p
    return None


@pytest.fixture(scope="session")
def font_dir() -> Path:
    d = _find_font_dir()
    if d is None:
        pytest.skip(
            "KOTOBA_FONT_DIR must point at a directory containing the Noto Sans JP "
            "fonts to run render/API tests (fetched in CI before pytest runs)."
        )
    return d


@pytest.fixture
def kotoba_settings(tmp_path, monkeypatch, font_dir) -> Settings:
    db_path = tmp_path / "kotoba.db"
    cache_dir = tmp_path / "cache"
    seed_db = tmp_path / "no-such-seed.db"  # deliberately absent

    monkeypatch.setenv("KOTOBA_DB_PATH", str(db_path))
    monkeypatch.setenv("KOTOBA_SEED_DB", str(seed_db))
    monkeypatch.setenv("KOTOBA_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("KOTOBA_FONT_DIR", str(font_dir))
    monkeypatch.setenv("KOTOBA_CACHE_KEEP_DAYS", "7")
    monkeypatch.delenv("KOTOBA_LEVELS", raising=False)

    db.reset_engine_cache()
    settings = get_settings()
    db.ensure_db_ready(settings)

    yield settings

    db.reset_engine_cache()


@pytest.fixture
def seeded_db(kotoba_settings: Settings) -> Settings:
    session_factory = db.get_sessionmaker(kotoba_settings)
    with session_factory() as session:
        for seq, surface, reading, kana_only, gloss, pos, jlpt, sort_key in FIXTURE_WORDS:
            session.add(
                Word(
                    seq=seq,
                    surface=surface,
                    reading=reading,
                    is_kana_only=kana_only,
                    gloss=gloss,
                    pos=pos,
                    jlpt=jlpt,
                    sort_key=sort_key,
                )
            )
        session.commit()
    return kotoba_settings


@pytest.fixture
def client(seeded_db: Settings) -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def set_today(monkeypatch):
    """set_today(date) monkeypatches selection.today() for both selection.py and api.py
    (api.py calls it as `selection.today()`, so patching the module attribute suffices)."""

    def _set(day: dt.date) -> None:
        monkeypatch.setattr(selection, "today", lambda: day)

    return _set
