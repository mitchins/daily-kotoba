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
import re
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from daily_kotoba.models import Base, Meta, Word

logger = logging.getLogger(__name__)

JMDICT_REPO = "scriptin/jmdict-simplified"
JMDICT_ASSET_PATTERN = re.compile(r"^jmdict-eng-common-.*\.json\.zip$")

JLPT_REPO = "stephenmk/yomitan-jlpt-vocab"
JLPT_ASSET_PATTERN = re.compile(r"^jlpt\.zip$")

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


def _download_zip(url: str, client: httpx.Client) -> zipfile.ZipFile:
    resp = client.get(url)
    resp.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _easier(a: str, b: str) -> str:
    return a if _LEVEL_RANK[a] > _LEVEL_RANK[b] else b


def parse_jlpt_banks(
    banks: Iterable[list[Any]],
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """Fold yomitan term_meta_bank entries into (surface, reading) and surface lookups,
    keeping the easiest level whenever a key is seen more than once."""
    by_pair: dict[tuple[str, str], str] = {}
    by_surface: dict[str, str] = {}

    for bank in banks:
        for entry in bank:
            surface, _kind, meta = entry[0], entry[1], entry[2]
            freq = meta.get("frequency") if isinstance(meta, dict) else None
            if not isinstance(freq, dict):
                continue
            level = freq.get("displayValue")
            if level not in _LEVEL_RANK:
                continue

            if surface in by_surface:
                by_surface[surface] = _easier(level, by_surface[surface])
            else:
                by_surface[surface] = level

            reading = meta.get("reading")
            if reading:
                key = (surface, reading)
                by_pair[key] = _easier(level, by_pair[key]) if key in by_pair else level

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


def join_words(
    jmdict_entries: Iterable[dict[str, Any]],
    by_pair: dict[tuple[str, str], str],
    by_surface: dict[str, str],
) -> list[dict[str, Any]]:
    """Join JMdict entries against the JLPT lookups, applying the filtering/shaping
    rules in spec section 2.3. Returns plain dicts ready for `Word(**row)`."""
    rows: list[dict[str, Any]] = []

    for entry in jmdict_entries:
        kana = entry.get("kana") or []
        kanji = entry.get("kanji") or []
        if not kana:
            continue

        reading = next((k["text"] for k in kana if k.get("common")), kana[0]["text"])
        is_kana_only = not kanji
        if kanji:
            surface = next((k["text"] for k in kanji if k.get("common")), kanji[0]["text"])
        else:
            surface = reading

        level = by_pair.get((surface, reading)) or by_surface.get(surface)
        if level is None:
            continue

        senses = entry.get("sense") or []
        if not senses:
            continue
        sense0 = senses[0]

        if set(sense0.get("misc") or []) & _ARCHAIC_MISC:
            continue

        gloss_texts = [
            g["text"] for g in (sense0.get("gloss") or []) if g.get("lang", "eng") == "eng"
        ][:3]
        if not gloss_texts:
            continue
        gloss = _truncate_gloss(", ".join(gloss_texts))

        pos_tags = sense0.get("partOfSpeech") or []
        pos = _map_pos(pos_tags[0]) if pos_tags else None

        seq = str(entry["id"])
        # blake2b gives an unsigned 64-bit spread; SQLite INTEGER is signed 64-bit, so
        # reinterpret the top half as negative (two's complement) rather than losing
        # entries to an OverflowError. Only the deterministic shuffle order matters here.
        raw = int.from_bytes(hashlib.blake2b(seq.encode(), digest_size=8).digest(), "big")
        sort_key = raw - 2**64 if raw >= 2**63 else raw

        rows.append(
            {
                "seq": seq,
                "surface": surface,
                "reading": reading,
                "is_kana_only": is_kana_only,
                "gloss": gloss,
                "pos": pos,
                "jlpt": level,
                "sort_key": sort_key,
            }
        )

    return rows


def _summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(LEVELS, 0)
    for row in rows:
        counts[row["jlpt"]] += 1
    return counts


def write_db(out_path: Path, rows: list[dict[str, Any]], jmdict_tag: str, jlpt_tag: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    engine = create_engine(f"sqlite:///{out_path}")
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

    engine.dispose()


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
    sys.exit(run(args.out, args.jmdict_url, args.jlpt_url))


if __name__ == "__main__":
    main()
