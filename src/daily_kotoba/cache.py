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


def _read_cached(path: Path) -> tuple[bytes, dt.datetime] | None:
    """Read a cache entry and its mtime, or None if it is not there.

    Both operations must tolerate the file vanishing mid-flight: the per-day quota
    is enforced by whichever request happens to be writing, so a concurrent worker
    can unlink an entry another request is in the middle of serving.
    """
    try:
        data = path.read_bytes()
        return data, dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    except (FileNotFoundError, NotADirectoryError):
        return None


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

    # No exists() check: it would be a TOCTOU gap. A concurrent request can trip the
    # per-day quota and evict this very file between the check and the read, so the
    # only safe test is the read itself. Same for the stat() that follows.
    hit = _read_cached(path)
    if hit is not None:
        data, mtime = hit
    else:
        data = render_card(word, width, height, title)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic within the same directory
        _enforce_day_quota(path.parent, path)
        _prune(cache_dir, keep_days, day)
        # Re-stat rather than assume the file survived; falling back to "now" is
        # accurate anyway, since we just rendered these bytes.
        written = _read_cached(path)
        mtime = written[1] if written else dt.datetime.now(dt.UTC)

    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    return CachedImage(data=data, etag=etag, last_modified=mtime)
