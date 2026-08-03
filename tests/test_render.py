from __future__ import annotations

import io

import pytest
from PIL import Image

from daily_kotoba.models import Word
from daily_kotoba.render import render_card


def _word(**overrides) -> Word:
    defaults = dict(
        id=1,
        seq="1",
        surface="楽しい",
        reading="たのしい",
        is_kana_only=False,
        gloss="fun, enjoyable, pleasant",
        pos="i-adjective",
        jlpt="N4",
        sort_key=1,
        last_shown_on=None,
        shown_count=0,
    )
    defaults.update(overrides)
    return Word(**defaults)


@pytest.mark.usefixtures("kotoba_settings")
@pytest.mark.parametrize("width,height", [(760, 300), (96, 48), (800, 480), (400, 200)])
def test_render_kanji_word_correct_mode_and_size(width, height):
    png_bytes = render_card(_word(), width, height)
    img = Image.open(io.BytesIO(png_bytes))
    assert img.mode == "1"
    assert img.size == (width, height)


@pytest.mark.usefixtures("kotoba_settings")
def test_render_kana_only_word():
    word = _word(surface="ありがとう", reading="ありがとう", is_kana_only=True, pos="expression")
    png_bytes = render_card(word, 760, 300)
    img = Image.open(io.BytesIO(png_bytes))
    assert img.mode == "1"
    assert img.size == (760, 300)


@pytest.mark.usefixtures("kotoba_settings")
def test_render_no_pos():
    word = _word(pos=None)
    png_bytes = render_card(word, 760, 300)
    img = Image.open(io.BytesIO(png_bytes))
    assert img.mode == "1"
    assert img.size == (760, 300)


@pytest.mark.usefixtures("kotoba_settings")
def test_render_long_gloss_wraps_without_crashing():
    word = _word(
        gloss="a very long gloss that should wrap across two lines and possibly get "
        "truncated with an ellipsis if it still overflows the available space"
    )
    png_bytes = render_card(word, 400, 200)
    img = Image.open(io.BytesIO(png_bytes))
    assert img.mode == "1"
    assert img.size == (400, 200)


def _edge_ink(img: Image.Image, margin: int = 2) -> list[tuple[int, int]]:
    """Coordinates of any black pixel inside the outer `margin` — i.e. content
    that ran off the card instead of being wrapped or ellipsized."""
    px = img.load()
    w, h = img.size
    edges = [(x, y) for x in range(w) for y in (*range(margin), *range(h - margin, h))]
    edges += [(x, y) for y in range(h) for x in (*range(margin), *range(w - margin, w))]
    return [c for c in edges if px[c] == 0]


@pytest.mark.usefixtures("kotoba_settings")
@pytest.mark.parametrize("width,height", [(760, 300), (800, 480), (400, 200), (96, 48)])
def test_render_never_bleeds_past_the_canvas_edge(width, height):
    # A long *first* gloss segment has no preceding text to force a line break, so
    # it used to be emitted unwrapped and bled off both sides. Real JMdict entry.
    word = _word(
        surface="碑",
        reading="いしぶみ",
        gloss=(
            "stone monument bearing an inscription (esp. memorial for future "
            "generations), stele, stela"
        ),
        pos="noun",
        jlpt="N1",
    )
    img = Image.open(io.BytesIO(render_card(word, width, height)))
    assert _edge_ink(img) == []


@pytest.mark.usefixtures("kotoba_settings")
@pytest.mark.parametrize("width,height", [(760, 300), (800, 480), (96, 48)])
def test_render_long_reading_does_not_bleed(width, height):
    # The reading is drawn at a fixed size with no auto-fit, so a short surface
    # paired with an overlong reading is the case that escapes every other guard.
    word = _word(surface="一", reading="あ" * 40)
    img = Image.open(io.BytesIO(render_card(word, width, height)))
    assert _edge_ink(img) == []


@pytest.mark.usefixtures("kotoba_settings")
def test_render_unbreakable_token_is_ellipsized():
    word = _word(gloss="Pneumonoultramicroscopicsilicovolcanoconiosis" * 3)
    img = Image.open(io.BytesIO(render_card(word, 400, 200)))
    assert _edge_ink(img) == []
