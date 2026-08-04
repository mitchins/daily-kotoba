"""Build-time seed script: `python -m daily_kotoba.seed --out <path>`.

Fetches JMdict (words + glosses) and a JLPT vocabulary list, joins them on
(surface, reading), and writes a self-contained SQLite DB. Runs once, at Docker
build time — the runtime container never needs network access for data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from daily_kotoba.models import Base, Meta, Word

logger = logging.getLogger(__name__)

JMDICT_REPO = "scriptin/jmdict-simplified"
JMDICT_ASSET_PATTERN = re.compile(r"^jmdict-eng-common-.*\.json\.zip$")

JLPT_REPO = "stephenmk/yomitan-jlpt-vocab"
JLPT_ASSET_PATTERN = re.compile(r"^jlpt\.zip$")

# Release assets are served from these hosts; anything else is a misconfigured or
# hostile --jmdict-url / --jlpt-url override.
_ALLOWED_SOURCE_HOSTS = frozenset({"github.com", "api.github.com", "objects.githubusercontent.com"})

# The JLPT has not published official vocabulary lists since 2010; "level" here is a
# well-regarded community reconstruction (Jonathan Waller's list), not authoritative.
LEVELS = ("N5", "N4", "N3", "N2", "N1")
# "Easiest wins" == largest digit: N5 (5) beats N1 (1).
_LEVEL_RANK = {level: int(level[1]) for level in LEVELS}

_ARCHAIC_MISC = {"arch", "obs", "obsc", "rare", "vulg", "sl", "derog", "X"}

_POS_MAP = {
    "n": "noun",
    "adj-i": "i-adjective",
    "adj-na": "na-adjective",
    "adv": "adverb",
    "exp": "expression",
    "int": "interjection",
    "pn": "pronoun",
    "prt": "particle",
    "conj": "conjunction",
    "suf": "suffix",
    "pref": "prefix",
}

MIN_WORDS_PER_LEVEL = 100

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ZIP_ENTRIES = 100


def resolve_latest_asset(
    repo: str, pattern: re.Pattern[str], client: httpx.Client
) -> tuple[str, str]:
    """Return (download_url, release_tag) for the newest release asset matching `pattern`."""
    resp = client.get(f"https://api.github.com/repos/{repo}/releases/latest")
    resp.raise_for_status()
    data = resp.json()
    for asset in data["assets"]:
        if pattern.match(asset["name"]):
            return asset["browser_download_url"], data["tag_name"]
    raise RuntimeError(f"no asset matching {pattern.pattern!r} in {repo}'s latest release")


def validate_source_url(url: str) -> str:
    """Confine the `--jmdict-url` / `--jlpt-url` overrides to HTTPS on GitHub.

    These flags exist to pin or sideload a release asset, not to point the seeder at
    an arbitrary host: whatever they fetch is parsed and baked into the shipped image.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"source URL must be https, got {parsed.scheme or 'no scheme'}: {url}")
    if parsed.hostname not in _ALLOWED_SOURCE_HOSTS:
        raise ValueError(
            f"source host {parsed.hostname!r} is not allowed "
            f"(expected one of {sorted(_ALLOWED_SOURCE_HOSTS)})"
        )
    return url


def validate_out_path(path: Path) -> Path:
    """Resolve `--out` to an absolute .db path.

    Resolving first means the value handed to create_engine() is a plain filesystem
    path, with no room for relative-traversal or connection-string trickery.
    """
    resolved = path.expanduser().resolve()
    if resolved.suffix != ".db":
        raise ValueError(f"--out must end in .db, got {resolved}")
    return resolved


def _download_zip(url: str, client: httpx.Client) -> zipfile.ZipFile:
    """Stream the archive under a size cap and reject implausible ZIPs.

    The real assets are ~1 MB compressed / ~50 MB expanded. The caps are generous
    multiples of that, so they only fire on a corrupt release or a decompression
    bomb — either of which would otherwise OOM the Docker build.
    """
    buf = io.BytesIO()
    with client.stream("GET", validate_source_url(url)) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            buf.write(chunk)
            if buf.tell() > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"{url} exceeds the {MAX_DOWNLOAD_BYTES} byte download limit")

    archive = zipfile.ZipFile(buf)
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise RuntimeError(f"{url} has {len(infos)} entries, over the {MAX_ZIP_ENTRIES} limit")
    total = sum(info.file_size for info in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise RuntimeError(
            f"{url} expands to {total} bytes, over the {MAX_UNCOMPRESSED_BYTES} limit"
        )
    return archive


def _easier(a: str, b: str) -> str:
    return a if _LEVEL_RANK[a] > _LEVEL_RANK[b] else b


def _bank_entry(entry: list[Any]) -> tuple[str, str | None, str] | None:
    """Unpack one yomitan term_meta_bank row into (surface, reading, level),
    or None if it is not a usable JLPT frequency entry."""
    surface, meta = entry[0], entry[2]
    freq = meta.get("frequency") if isinstance(meta, dict) else None
    if not isinstance(freq, dict):
        return None
    level = freq.get("displayValue")
    if level not in _LEVEL_RANK:
        return None
    return surface, meta.get("reading"), level


def _merge_easiest(target: dict[Any, str], key: Any, level: str) -> None:
    target[key] = _easier(level, target[key]) if key in target else level


def parse_jlpt_banks(
    banks: Iterable[list[Any]],
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """Fold yomitan term_meta_bank entries into (surface, reading) and surface lookups,
    keeping the easiest level whenever a key is seen more than once."""
    by_pair: dict[tuple[str, str], str] = {}
    by_surface: dict[str, str] = {}

    for bank in banks:
        for entry in bank:
            parsed = _bank_entry(entry)
            if parsed is None:
                continue
            surface, reading, level = parsed
            _merge_easiest(by_surface, surface, level)
            if reading:
                _merge_easiest(by_pair, (surface, reading), level)

    return by_pair, by_surface


def _map_pos(tag: str) -> str:
    if tag == "n":
        return "noun"
    if tag == "v1" or tag.startswith("v5"):
        return "verb"
    return _POS_MAP.get(tag, tag)


def _truncate_gloss(text: str, limit: int = 100) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;") + "…"


def _sort_key(seq: str) -> int:
    """Deterministic pseudo-random ordering key. blake2b gives an unsigned 64-bit
    spread; SQLite INTEGER is signed 64-bit, so reinterpret the top half as negative
    (two's complement) rather than losing entries to an OverflowError. Only the
    shuffle order matters here, not the sign."""
    raw = int.from_bytes(hashlib.blake2b(seq.encode(), digest_size=8).digest(), "big")
    return raw - 2**64 if raw >= 2**63 else raw


def _headwords(entry: dict[str, Any]) -> tuple[str, str, bool] | None:
    """Pick (surface, reading, is_kana_only), preferring entries flagged common."""
    kana = entry.get("kana") or []
    if not kana:
        return None
    kanji = entry.get("kanji") or []
    reading = next((k["text"] for k in kana if k.get("common")), kana[0]["text"])
    if not kanji:
        return reading, reading, True
    surface = next((k["text"] for k in kanji if k.get("common")), kanji[0]["text"])
    return surface, reading, False


def _first_sense(entry: dict[str, Any]) -> tuple[str, str | None] | None:
    """Shape the first sense into (gloss, pos), or None if it should be dropped."""
    senses = entry.get("sense") or []
    if not senses:
        return None
    sense0 = senses[0]
    if set(sense0.get("misc") or []) & _ARCHAIC_MISC:
        return None

    gloss_texts = [g["text"] for g in (sense0.get("gloss") or []) if g.get("lang", "eng") == "eng"][
        :3
    ]
    if not gloss_texts:
        return None

    pos_tags = sense0.get("partOfSpeech") or []
    return _truncate_gloss(", ".join(gloss_texts)), (_map_pos(pos_tags[0]) if pos_tags else None)


def _build_row(
    entry: dict[str, Any],
    by_pair: dict[tuple[str, str], str],
    by_surface: dict[str, str],
) -> dict[str, Any] | None:
    heads = _headwords(entry)
    if heads is None:
        return None
    surface, reading, is_kana_only = heads

    level = by_pair.get((surface, reading)) or by_surface.get(surface)
    if level is None:
        return None

    sense = _first_sense(entry)
    if sense is None:
        return None
    gloss, pos = sense

    seq = str(entry["id"])
    return {
        "seq": seq,
        "surface": surface,
        "reading": reading,
        "is_kana_only": is_kana_only,
        "gloss": gloss,
        "pos": pos,
        "jlpt": level,
        "sort_key": _sort_key(seq),
    }


def join_words(
    jmdict_entries: Iterable[dict[str, Any]],
    by_pair: dict[tuple[str, str], str],
    by_surface: dict[str, str],
) -> list[dict[str, Any]]:
    """Join JMdict entries against the JLPT lookups, applying the filtering/shaping
    rules in spec section 2.3. Returns plain dicts ready for `Word(**row)`."""
    rows = (_build_row(entry, by_pair, by_surface) for entry in jmdict_entries)
    return [row for row in rows if row is not None]


def _summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(LEVELS, 0)
    for row in rows:
        counts[row["jlpt"]] += 1
    return counts


def write_db(out_path: Path, rows: list[dict[str, Any]], jmdict_tag: str, jlpt_tag: str) -> None:
    out_path = validate_out_path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build into a sibling temp file and swap it in, so a failure part-way through
    # leaves any existing DB untouched rather than deleting it up front and
    # stranding a half-written file for the Docker build to COPY.
    fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, prefix=".seed-", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        # URL.create rather than an f-string: SQLAlchemy parses "sqlite:///<path>" as a
        # URL, so a "?" anywhere in the path would silently truncate the database name.
        engine = create_engine(URL.create("sqlite", database=str(tmp_path)))
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add_all(Word(**row) for row in rows)
                session.add_all(
                    [
                        Meta(key="jmdict_tag", value=jmdict_tag),
                        Meta(key="jlpt_tag", value=jlpt_tag),
                        Meta(key="seeded_at", value=dt.datetime.now(dt.UTC).isoformat()),
                        Meta(key="schema_version", value="1"),
                    ]
                )
                session.commit()
        finally:
            engine.dispose()
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def run(out: Path, jmdict_url: str | None, jlpt_url: str | None) -> int:
    headers = {"User-Agent": "daily-kotoba-seed"}
    with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as client:
        if jmdict_url:
            jmdict_tag = "manual"
        else:
            jmdict_url, jmdict_tag = resolve_latest_asset(JMDICT_REPO, JMDICT_ASSET_PATTERN, client)
        if jlpt_url:
            jlpt_tag = "manual"
        else:
            jlpt_url, jlpt_tag = resolve_latest_asset(JLPT_REPO, JLPT_ASSET_PATTERN, client)

        print(f"jmdict: {jmdict_url} (tag={jmdict_tag})")
        print(f"jlpt:   {jlpt_url} (tag={jlpt_tag})")

        jmdict_zip = _download_zip(jmdict_url, client)
        jlpt_zip = _download_zip(jlpt_url, client)

    jmdict_json_name = next(n for n in jmdict_zip.namelist() if n.endswith(".json"))
    jmdict_doc = json.loads(jmdict_zip.read(jmdict_json_name))
    jmdict_entries = jmdict_doc["words"]

    bank_pattern = re.compile(r"term_meta_bank_\d+\.json$")
    bank_names = sorted(n for n in jlpt_zip.namelist() if bank_pattern.match(Path(n).name))
    banks = [json.loads(jlpt_zip.read(name)) for name in bank_names]

    by_pair, by_surface = parse_jlpt_banks(banks)
    rows = join_words(jmdict_entries, by_pair, by_surface)

    write_db(out, rows, jmdict_tag, jlpt_tag)

    counts = _summarize(rows)
    print("word counts by level:")
    for level in LEVELS:
        print(f"  {level}: {counts[level]}")
    print(f"  total: {len(rows)}")

    short = [level for level in LEVELS if counts[level] < MIN_WORDS_PER_LEVEL]
    if short:
        print(
            f"ERROR: level(s) {', '.join(short)} have fewer than {MIN_WORDS_PER_LEVEL} words "
            "— the JMdict/JLPT join likely broke.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the daily-kotoba word database.")
    parser.add_argument("--out", required=True, type=Path, help="output SQLite path")
    parser.add_argument("--jmdict-url", default=None, help="override the JMdict asset URL")
    parser.add_argument("--jlpt-url", default=None, help="override the JLPT asset URL")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    try:
        # Validate up front so a bad argument fails before downloading ~10 MB.
        out = validate_out_path(args.out)
        for override in (args.jmdict_url, args.jlpt_url):
            if override:
                validate_source_url(override)
    except ValueError as exc:
        parser.error(str(exc))

    sys.exit(run(out, args.jmdict_url, args.jlpt_url))


if __name__ == "__main__":
    main()
