"""FastAPI app: healthz, daily word JSON, daily 1-bit PNG, and selection history."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from daily_kotoba import cache, db, selection
from daily_kotoba.config import Settings, get_settings
from daily_kotoba.models import Meta, Selection, Word
from daily_kotoba.render import TitleStyle

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 760
DEFAULT_HEIGHT = 300


class WordOut(BaseModel):
    id: int
    seq: str
    surface: str
    reading: str
    gloss: str
    pos: str | None
    jlpt: str


class ImageOut(BaseModel):
    url: str
    width: int
    height: int


class DailyOut(BaseModel):
    date: str
    word: WordOut
    image: ImageOut


class HistoryItemOut(BaseModel):
    date: str
    word: WordOut


class HealthOut(BaseModel):
    status: str
    words: int
    levels: list[str]
    tz: str | None
    today: str
    selected_word_id: int | None
    jmdict_tag: str | None


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    Path(settings.cache_dir).mkdir(parents=True, exist_ok=True)
    db.ensure_db_ready(settings)

    with db.get_sessionmaker(settings)() as session:
        word_count = session.scalar(select(func.count()).select_from(Word)) or 0

    tz_name = dt.datetime.now().astimezone().tzname()
    logger.info(
        "startup: tz=%s today=%s words=%d levels=%s",
        tz_name,
        selection.today(),
        word_count,
        settings.levels_list,
    )
    yield


app = FastAPI(title="daily-kotoba", lifespan=lifespan)


def get_session() -> Iterator[Session]:
    settings = get_settings()
    session_factory = db.get_sessionmaker(settings)
    with session_factory() as session:
        yield session


def _word_out(word: Word) -> WordOut:
    return WordOut(
        id=word.id,
        seq=word.seq,
        surface=word.surface,
        reading=word.reading,
        gloss=word.gloss,
        pos=word.pos,
        jlpt=word.jlpt,
    )


# Both word endpoints surface EmptyPoolError as a 503; declared here so it shows up
# in the generated OpenAPI schema rather than only in the code path.
_EMPTY_POOL_RESPONSE = {"description": "No words match the configured JLPT levels."}


def _get_today_word(session: Session, settings: Settings) -> tuple[dt.date, Word]:
    day = selection.today()
    try:
        sel = selection.get_or_create_selection(session, day, settings.levels_list)
    except selection.EmptyPoolError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    word = session.get(Word, sel.word_id)
    assert word is not None  # FK guarantees this
    return day, word


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)) -> HealthOut:
    """Read-only: must never mint the day's selection, so an orchestrator probe is inert."""
    settings = get_settings()
    day = selection.today()
    words = session.scalar(select(func.count()).select_from(Word)) or 0
    sel = session.scalar(select(Selection).where(Selection.day == day))
    jmdict_tag = session.scalar(select(Meta.value).where(Meta.key == "jmdict_tag"))
    tz_name = dt.datetime.now().astimezone().tzname()
    return HealthOut(
        status="ok",
        words=words,
        levels=settings.levels_list,
        tz=tz_name,
        today=day.isoformat(),
        selected_word_id=sel.word_id if sel else None,
        jmdict_tag=jmdict_tag,
    )


@app.get("/v1/daily.json", responses={503: _EMPTY_POOL_RESPONSE})
def get_daily_json(session: Session = Depends(get_session)) -> DailyOut:
    settings = get_settings()
    day, word = _get_today_word(session, settings)
    return DailyOut(
        date=day.isoformat(),
        word=_word_out(word),
        image=ImageOut(
            url=f"/v1/daily.png?w={DEFAULT_WIDTH}&h={DEFAULT_HEIGHT}",
            width=DEFAULT_WIDTH,
            height=DEFAULT_HEIGHT,
        ),
    )


@app.get("/v1/daily.png", responses={503: _EMPTY_POOL_RESPONSE})
def get_daily_png(
    request: Request,
    w: int = Query(DEFAULT_WIDTH, ge=96, le=800),
    h: int = Query(DEFAULT_HEIGHT, ge=48, le=480),
    title: TitleStyle = Query(
        TitleStyle.NONE,
        description="Heading drawn top-left, opposite the JLPT badge. 'ja' renders "
        "日本語 — server-side, so it works despite the firmware having no CJK glyphs.",
    ),
    session: Session = Depends(get_session),
) -> Response:
    settings = get_settings()
    day, word = _get_today_word(session, settings)
    cached = cache.get_or_render(
        Path(settings.cache_dir), day, w, h, word, settings.cache_keep_days, title
    )
    headers = {
        "ETag": cached.etag,
        "Last-Modified": cached.last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "Cache-Control": "public, max-age=300",
    }
    if request.headers.get("if-none-match") == cached.etag:
        return Response(status_code=304, headers=headers)
    return Response(content=cached.data, media_type="image/png", headers=headers)


@app.get("/v1/history")
def get_history(
    limit: int = Query(30, ge=1, le=100), session: Session = Depends(get_session)
) -> list[HistoryItemOut]:
    rows = session.execute(
        select(Selection, Word)
        .join(Word, Selection.word_id == Word.id)
        .order_by(Selection.day.desc())
        .limit(limit)
    ).all()
    return [HistoryItemOut(date=sel.day.isoformat(), word=_word_out(word)) for sel, word in rows]
