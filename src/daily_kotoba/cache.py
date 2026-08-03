"""On-disk PNG cache for rendered word cards, keyed by (day, size, RENDER_VERSION)."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from daily_kotoba.render import RENDER_VERSION, render_card

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedImage:
    data: bytes
    etag: str
    last_modified: dt.datetime


def _day_dir(cache_dir: Path, day: dt.date) -> Path:
    return cache_dir / day.strftime("%Y-%m-%d")


def _cache_path(cache_dir: Path, day: dt.date, width: int, height: int) -> Path:
    return _day_dir(cache_dir, day) / f"{width}x{height}-v{RENDER_VERSION}.png"


def _prune(cache_dir: Path, keep_days: int, today: dt.date) -> None:
    if not cache_dir.exists():
        return
    cutoff = today - dt.timedelta(days=keep_days)
    for entry in cache_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            day = dt.datetime.strptime(entry.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            for f in entry.iterdir():
                f.unlink(missing_ok=True)
            entry.rmdir()
            logger.info("pruned cache dir %s", entry)


def get_or_render(
    cache_dir: Path, day: dt.date, width: int, height: int, word, keep_days: int
) -> CachedImage:
    path = _cache_path(cache_dir, day, width, height)

    if path.exists():
        data = path.read_bytes()
    else:
        data = render_card(word, width, height)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic within the same directory
        _prune(cache_dir, keep_days, day)

    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    return CachedImage(data=data, etag=etag, last_modified=mtime)
