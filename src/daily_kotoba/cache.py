"""On-disk PNG cache for rendered word cards, keyed by (day, size, RENDER_VERSION)."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from daily_kotoba.render import RENDER_VERSION, TitleStyle, render_card

logger = logging.getLogger(__name__)

# Ceiling on cache files per day. The display asks for one size; a handful more
# covers manual poking and the /docs preview. Anything beyond that is a client in
# a loop, and evicting is cheaper than filling the volume.
MAX_ENTRIES_PER_DAY = 64


@dataclass(frozen=True)
class CachedImage:
    data: bytes
    etag: str
    last_modified: dt.datetime


def _day_dir(cache_dir: Path, day: dt.date) -> Path:
    return cache_dir / day.strftime("%Y-%m-%d")


def _cache_path(cache_dir: Path, day: dt.date, width: int, height: int, title: TitleStyle) -> Path:
    # TitleStyle is a closed enum, so its value is filename-safe and — more to the
    # point — bounded: entries per day stay a small multiple of the size count,
    # rather than growing with however many distinct titles a caller asks for.
    suffix = "" if title is TitleStyle.NONE else f"-{title.value}"
    return _day_dir(cache_dir, day) / f"{width}x{height}{suffix}-v{RENDER_VERSION}.png"


def _enforce_day_quota(day_dir: Path, keep: Path) -> None:
    """Cap the number of entries in a single day's directory, evicting oldest-first.

    _prune only drops whole *expired* days, which leaves today unbounded: `w` and `h`
    span 705 x 433 permitted values, so a client looping over sizes could mint ~900k
    entries before the day even rolls over. The device only ever asks for one size,
    so a small cap costs nothing legitimate and turns disk use into a hard ceiling.
    """
    entries = []
    for f in day_dir.glob("*.png"):
        if f == keep:  # never evict the entry we just wrote and are about to stat()
            continue
        try:
            entries.append((f.stat().st_mtime, f))
        except OSError:  # raced with another worker's eviction
            continue
    excess = len(entries) - (MAX_ENTRIES_PER_DAY - 1)  # -1: `keep` is not counted
    if excess <= 0:
        return
    entries.sort()
    for _mtime, f in entries[:excess]:
        f.unlink(missing_ok=True)
    logger.info("evicted %d cache entries from %s", excess, day_dir)


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
    cache_dir: Path,
    day: dt.date,
    width: int,
    height: int,
    word,
    keep_days: int,
    title: TitleStyle = TitleStyle.NONE,
) -> CachedImage:
    path = _cache_path(cache_dir, day, width, height, title)

    if path.exists():
        data = path.read_bytes()
    else:
        data = render_card(word, width, height, title)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic within the same directory
        _enforce_day_quota(path.parent, path)
        _prune(cache_dir, keep_days, day)

    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    return CachedImage(data=data, etag=etag, last_modified=mtime)
