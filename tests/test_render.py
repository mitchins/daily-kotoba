from __future__ import annotations

import io

import pytest
from PIL import Image, ImageChops

from daily_kotoba.models import Word
from daily_kotoba.render import Polarity, TitleStyle, render_card


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


def _render_l(width: int, height: int, **kwargs) -> Image.Image:
    """Render a card and widen it to 8bpp for pixel comparison.

    Mode "1" packs rows to whole bytes, so the trailing padding bits of a row are
    not pixels and do not invert with them — a bytewise compare of the packed form
    would be wrong for any width that is not a multiple of 8.
    """
    img = Image.open(io.BytesIO(render_card(_word(), width, height, **kwargs)))
    assert img.mode == "1"
    assert img.size == (width, height)
    return img.convert("L")


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
    ellipsis_img = Image.open(io.BytesIO(render_card(_word(gloss="…"), 400, 200)))
    blank_img = Image.open(io.BytesIO(render_card(_word(gloss=""), 400, 200)))

    assert img.tobytes() == ellipsis_img.tobytes()
    assert img.tobytes() != blank_img.tobytes()
    assert _edge_ink(img) == []


@pytest.mark.usefixtures("kotoba_settings")
def test_render_title_is_drawn_and_optional():
    plain = render_card(_word(), 370, 233)
    titled = render_card(_word(), 370, 233, title=TitleStyle.JA)
    assert plain != titled  # the title actually renders

    img = Image.open(io.BytesIO(titled))
    assert img.mode == "1"
    assert img.size == (370, 233)
    assert _edge_ink(img) == []


@pytest.mark.usefixtures("kotoba_settings")
@pytest.mark.parametrize("style", list(TitleStyle))
@pytest.mark.parametrize("width,height", [(370, 233), (96, 48), (800, 480)])
def test_render_every_title_style_stays_inside_the_canvas(style, width, height):
    # The title is clamped to the space left of the badge, so no style may bleed off
    # the canvas or run under the N-level box, at any permitted size.
    img = Image.open(io.BytesIO(render_card(_word(), width, height, title=style)))
    assert img.size == (width, height)
    assert _edge_ink(img) == []


@pytest.mark.usefixtures("kotoba_settings")
@pytest.mark.parametrize("width,height", [(370, 233), (96, 48), (800, 480)])
def test_render_mask_is_the_exact_complement_of_positive(width, height):
    # The whole contract of the mask: same layout, every pixel flipped. Asserted
    # structurally rather than by counting ink, so it holds at any size and cannot
    # be satisfied by a card that merely happens to be mostly dark.
    positive = _render_l(width, height, polarity=Polarity.POSITIVE)
    mask = _render_l(width, height, polarity=Polarity.MASK)
    # Guards against a blank card satisfying the complement check vacuously.
    assert set(positive.tobytes()) == {0, 255}
    assert mask.tobytes() == ImageChops.invert(positive).tobytes()


@pytest.mark.usefixtures("kotoba_settings")
def test_render_mask_composes_with_title():
    # polarity and title are independent axes; a mask must still carry the heading.
    plain = render_card(_word(), 370, 233, polarity=Polarity.MASK)
    titled = render_card(_word(), 370, 233, title=TitleStyle.JA, polarity=Polarity.MASK)
    assert plain != titled

    positive = _render_l(370, 233, title=TitleStyle.JA)
    assert Image.open(io.BytesIO(titled)).convert("L").tobytes() == (
        ImageChops.invert(positive).tobytes()
    )


@pytest.mark.usefixtures("kotoba_settings")
def test_render_title_styles_are_distinct():
    seen = {s: render_card(_word(), 370, 233, title=s) for s in TitleStyle}
    assert len(set(seen.values())) == len(TitleStyle)


@pytest.mark.parametrize(
    "requested,expected",
    [(14, 14), (15, 15), (19, 15), (20, 20), (25, 21), (26, 26), (40, 26)],
)
def test_snap_sharp_never_rounds_up(requested, expected):
    # Rounding up could ask for space the layout has not budgeted, so the ladder
    # always steps down; below the ladder floor the request passes through.
    from daily_kotoba.render import _snap_sharp

    assert _snap_sharp(requested) == expected
