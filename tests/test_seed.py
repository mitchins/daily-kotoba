from __future__ import annotations

import pytest

from daily_kotoba.seed import (
    join_words,
    parse_jlpt_banks,
    validate_out_path,
    validate_source_url,
)


def _freq(reading: str | None, level: str) -> list:
    meta = {"frequency": {"value": -1, "displayValue": level}}
    if reading is not None:
        meta["reading"] = reading
    return meta


JLPT_BANKS = [
    [
        ["楽しい", "freq", _freq("たのしい", "N4")],
        ["食べる", "freq", _freq("たべる", "N5")],
        # appears at two levels -> easiest (N5) should win
        ["変わる", "freq", _freq("かわる", "N3")],
    ],
    [
        ["変わる", "freq", _freq("かわる", "N5")],
        # surface-only entry (no reading) feeds by_surface but not by_pair
        ["経済", "freq", _freq(None, "N2")],
        # kana-only word: surface == reading in the bank too
        ["ありがとう", "freq", _freq("ありがとう", "N5")],
    ],
]


def _sense(gloss_texts, pos=("adj-i",), misc=None):
    return {
        "partOfSpeech": list(pos),
        "misc": misc or [],
        "gloss": [{"lang": "eng", "text": t} for t in gloss_texts],
    }


def test_parse_jlpt_banks_easiest_wins():
    by_pair, by_surface = parse_jlpt_banks(JLPT_BANKS)
    assert by_pair[("楽しい", "たのしい")] == "N4"
    assert by_pair[("食べる", "たべる")] == "N5"
    assert by_pair[("変わる", "かわる")] == "N5"  # N5 beats N3
    assert by_surface["経済"] == "N2"
    assert "経済" not in {s for (s, _r) in by_pair}


def test_join_words_basic_and_level_lookup():
    by_pair, by_surface = parse_jlpt_banks(JLPT_BANKS)
    entries = [
        {
            "id": "1000220",
            "kanji": [{"common": True, "text": "楽しい", "tags": []}],
            "kana": [{"common": True, "text": "たのしい", "tags": []}],
            "sense": [_sense(["fun", "enjoyable", "pleasant", "extra"])],
        }
    ]
    rows = join_words(entries, by_pair, by_surface)
    assert len(rows) == 1
    row = rows[0]
    assert row["seq"] == "1000220"
    assert row["surface"] == "楽しい"
    assert row["reading"] == "たのしい"
    assert row["is_kana_only"] is False
    assert row["jlpt"] == "N4"
    assert row["pos"] == "i-adjective"
    assert row["gloss"] == "fun, enjoyable, pleasant"  # only first 3 glosses


def test_join_words_drops_unlevelled_entry():
    by_pair, by_surface = parse_jlpt_banks(JLPT_BANKS)
    entries = [
        {
            "id": "999",
            "kanji": [{"common": True, "text": "未知語", "tags": []}],
            "kana": [{"common": True, "text": "みちご", "tags": []}],
            "sense": [_sense(["unknown word"])],
        }
    ]
    rows = join_words(entries, by_pair, by_surface)
    assert rows == []


def test_join_words_filters_archaic():
    by_pair, by_surface = parse_jlpt_banks(JLPT_BANKS)
    entries = [
        {
            "id": "1",
            "kanji": [{"common": True, "text": "食べる", "tags": []}],
            "kana": [{"common": True, "text": "たべる", "tags": []}],
            "sense": [_sense(["to eat"], misc=["arch"])],
        }
    ]
    rows = join_words(entries, by_pair, by_surface)
    assert rows == []


def test_join_words_kana_only_detection():
    by_pair, by_surface = parse_jlpt_banks(JLPT_BANKS)
    entries = [
        {
            "id": "2",
            "kanji": [],
            "kana": [{"common": True, "text": "ありがとう", "tags": []}],
            "sense": [_sense(["thank you"])],
        }
    ]
    rows = join_words(entries, by_pair, by_surface)
    assert len(rows) == 1
    assert rows[0]["is_kana_only"] is True
    assert rows[0]["surface"] == "ありがとう"
    assert rows[0]["jlpt"] == "N5"


def test_join_words_gloss_truncated_on_word_boundary():
    by_pair, by_surface = parse_jlpt_banks(JLPT_BANKS)
    long_gloss = "a" * 40 + " " + "b" * 40 + " " + "c" * 40  # > 100 chars total
    entries = [
        {
            "id": "3",
            "kanji": [{"common": True, "text": "楽しい", "tags": []}],
            "kana": [{"common": True, "text": "たのしい", "tags": []}],
            "sense": [_sense([long_gloss])],
        }
    ]
    rows = join_words(entries, by_pair, by_surface)
    gloss = rows[0]["gloss"]
    assert len(gloss) <= 101  # 100 chars + ellipsis char, trimmed to a word boundary
    assert gloss.endswith("…")
    assert " " not in gloss[-5:-1] or gloss[-2] != " "


def test_join_words_pos_mapping_and_verb_variants():
    by_pair, by_surface = parse_jlpt_banks(JLPT_BANKS)
    entries = [
        {
            "id": "4",
            "kanji": [{"common": True, "text": "変わる", "tags": []}],
            "kana": [{"common": True, "text": "かわる", "tags": []}],
            "sense": [_sense(["to change"], pos=("v5r",))],
        }
    ]
    rows = join_words(entries, by_pair, by_surface)
    assert rows[0]["pos"] == "verb"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/scriptin/jmdict-simplified/releases/download/x/y.json.zip",
        "https://api.github.com/repos/a/b/releases/latest",
        "https://objects.githubusercontent.com/blob",
    ],
)
def test_validate_source_url_allows_github_https(url):
    assert validate_source_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b.zip",  # not https
        "https://evil.example.com/a.zip",  # host not allowed
        "https://github.com.evil.example/a.zip",  # suffix-confusion attempt
        "file:///etc/passwd",
        "/relative/path.zip",
    ],
)
def test_validate_source_url_rejects_everything_else(url):
    with pytest.raises(ValueError):
        validate_source_url(url)


def test_validate_out_path_resolves_and_requires_db_suffix(tmp_path):
    resolved = validate_out_path(tmp_path / "sub" / ".." / "seed.db")
    assert resolved.is_absolute()
    assert resolved == tmp_path / "seed.db"


@pytest.mark.parametrize("bad", ["seed.sqlite", "seed.db?mode=memory", "seed"])
def test_validate_out_path_rejects_non_db_suffix(tmp_path, bad):
    with pytest.raises(ValueError):
        validate_out_path(tmp_path / bad)
