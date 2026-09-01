"""Renders a Word to a 1-bit PNG word card.

Pipeline: draw on an "L" canvas (antialiased text), then hard-threshold to mode "1".
Dithering is deliberately not used — dithered text on a 1-bit e-paper panel reads as
mud; a hard threshold keeps glyph edges crisp.
"""

from __future__ import annotations

import io
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from daily_kotoba import fonts

if TYPE_CHECKING:
    from daily_kotoba.models import Word

# Bump whenever the layout changes; cache.py folds this into the cache path so stale
# rendered art can never survive a deploy.
RENDER_VERSION = 5


class TitleStyle(StrEnum):
    """Which heading to draw opposite the JLPT badge.

    A closed set rather than free text: the rendered PNG is cached to disk, so an
    open-ended parameter would let callers mint unbounded cache entries, and it is
    not something a caller has any reason to choose freely.
    """

    NONE = "none"
    JA = "ja"
    EN = "en"


_TITLE_TEXT: dict[TitleStyle, str | None] = {
    TitleStyle.NONE: None,
    TitleStyle.JA: "日本語",
    TitleStyle.EN: "JAPANESE",
}


class Polarity(StrEnum):
    """Which pixel value carries the ink in the emitted PNG.

    POSITIVE is what a human expects to see. MASK exists because a 1-bit image is
    not always consumed as a picture: ESPHome hands a BINARY `online_image` to LVGL
    as LV_COLOR_FORMAT_A1 — alpha only — and its decoder sets a bit for *bright*
    pixels. A positive card therefore arrives with its paper opaque and its glyphs
    punched out as holes, and an LVGL image widget has no equivalent of
    `it.image()`'s COLOR_OFF/COLOR_ON to swap them back. MASK emits the card the way
    an alpha mask is read: set bits are ink.

    Named for the axis it moves rather than "invert", which says what happens to the
    bytes but not why anyone would want it.
    """

    POSITIVE = "positive"
    MASK = "mask"


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

# Thresholding an outline to 1bpp leaves stems on fractional pixel boundaries, so
# the same text is crisper at some ppem than others. Measured two ways over a Latin
# sample in Noto Sans JP Bold: share of stems at the dominant width, and coefficient
# of variation of stem widths (scale-invariant). The two rank sizes differently, so
# this ladder keeps only sizes that score well on *both* — the effect is real but
# modest (~20% on the better metric), not night-and-day.
_SHARP_SIZES = (15, 20, 21, 26)


def _snap_sharp(size: int) -> int:
    """Snap a Latin point size to the nearest measured-crisp value, never rounding
    up past the caller's request (that space may not exist)."""
    candidates = [s for s in _SHARP_SIZES if s <= size]
    if not candidates:
        return min(_SHARP_SIZES[0], size)
    return max(candidates)


def _get_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    font = _font_cache.get(key)
    if font is None:
        font = ImageFont.truetype(str(path), size)
        _font_cache[key] = font
    return font


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font, anchor="la")
    return bbox[2] - bbox[0]


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font, anchor="la")
    return bbox[3] - bbox[1]


def _measure_row(draw: ImageDraw.ImageDraw, lines: list[str], font: ImageFont.FreeTypeFont) -> dict:
    # Measuring actual glyph ink (rather than a generic leading multiplier) matters a
    # lot here: CJK glyphs at a large point size and short Latin captions have very
    # different bbox/em ratios, and a fudge factor either wastes space or overflows.
    heights = [_text_height(draw, line, font) for line in lines]
    inter = round(font.size * 0.25) if len(lines) > 1 else 0
    total = sum(heights) + inter * (len(lines) - 1)
    return {"lines": lines, "font": font, "heights": heights, "inter": inter, "total": total}


def _fit_surface_size(
    draw: ImageDraw.ImageDraw, text: str, path: Path, lo: int, hi: int, max_width: int
) -> int:
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        w = _text_width(draw, text, _get_font(path, mid))
        if w <= max_width:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _word_wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _fit_ellipsis(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    if _text_width(draw, f"{text}…", font) <= max_width:
        return f"{text}…"
    words = text.split(" ")
    while words:
        words.pop()
        candidate = f"{' '.join(words)}…"
        if _text_width(draw, candidate, font) <= max_width:
            return candidate
    return "…"


def _clamp_line(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    """Width backstop. _word_wrap breaks on spaces, so a single unbreakable token
    can still overrun the box; ellipsize rather than bleed off the canvas."""
    if _text_width(draw, text, font) <= max_width:
        return text
    return _fit_ellipsis(draw, text, font, max_width)


def _wrap_gloss(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """Wrap to at most 2 lines, preferring to break after ", "; falls back to
    word-wrap for an overlong segment, and ellipsizes anything past 2 lines."""
    segments = text.split(", ")
    lines: list[str] = []
    current = ""
    for seg in segments:
        if current and _text_width(draw, f"{current}, {seg}", font) <= max_width:
            current = f"{current}, {seg}"
            continue
        if current:
            lines.append(current)
            current = ""
        # A segment can exceed the box on its own — notably the first one, where
        # there is no preceding text to force the break.
        if _text_width(draw, seg, font) > max_width:
            wrapped = _word_wrap(draw, seg, font, max_width)
            lines.extend(wrapped[:-1])
            current = wrapped[-1]
        else:
            current = seg
    if current:
        lines.append(current)

    return [_clamp_line(draw, line, font, max_width) for line in lines[:2]]


def _stack_height(rows: list[dict], gap: int) -> int:
    return sum(r["total"] for r in rows) + gap * max(0, len(rows) - 1)


def _draw_header(
    draw: ImageDraw.ImageDraw,
    word: Word,
    title: TitleStyle,
    bold: Path,
    scale: int,
    pad: int,
    width: int,
) -> int:
    """Draw the JLPT badge (top-right) and the optional title (top-left).

    Returns the header height so the body knows where it may start. The title lives
    in the image rather than the display lambda so it can carry CJK — the firmware
    has no kanji glyphs, which is the whole reason this card is an image at all.
    """
    badge_size = max(1, round(scale * 0.09))
    badge_font = _get_font(bold, badge_size)
    badge_pad = max(1, round(scale * 0.02))
    bbox = draw.textbbox((0, 0), word.jlpt, font=badge_font, anchor="la")
    box_w = (bbox[2] - bbox[0]) + 2 * badge_pad
    box_h = (bbox[3] - bbox[1]) + 2 * badge_pad
    bx1, by0 = width - pad, pad
    bx0, by1 = bx1 - box_w, by0 + box_h
    draw.rounded_rectangle(
        [bx0, by0, bx1, by1], radius=max(2, round(box_h * 0.25)), outline=0, width=2
    )
    draw.text(((bx0 + bx1) / 2, (by0 + by1) / 2), word.jlpt, font=badge_font, fill=0, anchor="mm")

    title_text = _TITLE_TEXT[title]
    if title_text:
        title_font = _get_font(bold, badge_size)
        title_text = _clamp_line(draw, title_text, title_font, bx0 - 2 * pad)
        draw.text((pad, (by0 + by1) / 2), title_text, font=title_font, fill=0, anchor="lm")

    return box_h


def _build_rows(
    draw: ImageDraw.ImageDraw,
    word: Word,
    regular: Path,
    bold: Path,
    scale: int,
    max_width: int,
) -> tuple[list[dict], dict, dict | None, int, int]:
    """Measure every body row at its preferred size, before any fitting."""
    rows: list[dict] = []
    if not word.is_kana_only:
        reading_font = _get_font(regular, max(1, round(scale * 0.13)))
        # Fixed size, so unlike the surface it cannot auto-shrink — clamp instead,
        # or an unusually long reading would bleed past the edge.
        reading = _clamp_line(draw, word.reading, reading_font, max_width)
        rows.append(_measure_row(draw, [reading], reading_font))

    surface_lo = max(1, round(scale * 0.12))
    surface_hi = max(surface_lo, round(scale * 0.36))
    surface_size = _fit_surface_size(draw, word.surface, bold, surface_lo, surface_hi, max_width)
    rows.append(_measure_row(draw, [word.surface], _get_font(bold, surface_size)))

    gloss_row = _build_gloss(draw, word, bold, _snap_sharp(max(1, round(scale * 0.135))), max_width)

    pos_row: dict | None = None
    if word.pos:
        pos_font = _get_font(regular, _snap_sharp(max(1, round(scale * 0.10))))
        pos_row = _measure_row(draw, [f"({word.pos})"], pos_font)

    return rows, gloss_row, pos_row, surface_size, surface_lo


def _build_gloss(
    draw: ImageDraw.ImageDraw, word: Word, bold: Path, size: int, max_width: int
) -> dict:
    # Bold: the gloss competes with Inter@700 elsewhere on the board.
    font = _get_font(bold, size)
    return _measure_row(draw, _wrap_gloss(draw, word.gloss, font, max_width), font)


def _fit_rows(
    draw: ImageDraw.ImageDraw,
    word: Word,
    bold: Path,
    rows: list[dict],
    gloss_row: dict,
    pos_row: dict | None,
    surface_size: int,
    surface_lo: int,
    scale: int,
    max_width: int,
    gap: int,
    available: int,
) -> list[dict]:
    """Shrink the gloss, then the surface, then drop the POS line until the stack
    fits. Order matters: a few points off the surface is a smaller visual hit than
    losing the POS line outright. "Never clip" is the non-negotiable part.
    """

    def all_rows() -> list[dict]:
        return [*rows, gloss_row, *([pos_row] if pos_row else [])]

    gloss_min = max(1, round(scale * 0.08))
    size = gloss_row["font"].size
    # `size > _SHARP_SIZES[0]` keeps the gloss on the measured ladder: _snap_sharp
    # passes unmeasured sizes through below the floor, and rendering one of those
    # defeats the point of snapping at all.
    while (
        _stack_height(all_rows(), gap) > available and size > gloss_min and size > _SHARP_SIZES[0]
    ):
        size = _snap_sharp(size - 1)
        gloss_row = _build_gloss(draw, word, bold, size, max_width)

    while _stack_height(all_rows(), gap) > available and surface_size > surface_lo:
        surface_size -= 1
        rows[-1] = _measure_row(draw, [word.surface], _get_font(bold, surface_size))

    if _stack_height(all_rows(), gap) > available and pos_row is not None:
        pos_row = None

    return all_rows()


def _paint_rows(
    draw: ImageDraw.ImageDraw,
    final_rows: list[dict],
    width: int,
    body_top: int,
    available: int,
    gap: int,
) -> None:
    stack_h = _stack_height(final_rows, gap)
    y = body_top + max(0.0, (available - stack_h) / 2) if available > stack_h else body_top
    for row in final_rows:
        cursor = y
        for i, line in enumerate(row["lines"]):
            h = row["heights"][i]
            draw.text((width / 2, cursor + h / 2), line, font=row["font"], fill=0, anchor="mm")
            cursor += h + row["inter"]
        y += row["total"] + gap


def render_card(
    word: Word,
    width: int,
    height: int,
    title: TitleStyle = TitleStyle.NONE,
    polarity: Polarity = Polarity.POSITIVE,
) -> bytes:
    # Type is scaled by `scale`, not raw height: an extreme aspect ratio the size
    # guards still permit (e.g. 96x480) would otherwise pick ~48px text for a 38px
    # line box, leaving nothing that fits — not even the ellipsis. Both factors are
    # loose enough that every sane card size is unaffected.
    scale = min(height, round(width * 0.7))
    pad = max(1, round(min(width, height) * 0.06))
    canvas = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(canvas)

    regular = fonts.regular_path()
    bold = fonts.bold_path()
    max_width = width - 2 * pad

    box_h = _draw_header(draw, word, title, bold, scale, pad, width)

    rows, gloss_row, pos_row, surface_size, surface_lo = _build_rows(
        draw, word, regular, bold, scale, max_width
    )

    gap = max(1, round(scale * 0.03))
    body_top = pad + box_h + gap
    available = max(0, (height - pad) - body_top)

    final_rows = _fit_rows(
        draw,
        word,
        bold,
        rows,
        gloss_row,
        pos_row,
        surface_size,
        surface_lo,
        scale,
        max_width,
        gap,
        available,
    )
    _paint_rows(draw, final_rows, width, body_top, available, gap)

    # Hard threshold, never dithered: text on a 1-bit e-paper panel needs crisp edges.
    # Polarity is folded into this same pass rather than inverting afterwards, so a
    # mask is bit-for-bit the complement of a positive card — there is no second
    # quantisation that could soften an edge.
    paper, ink = (0, 255) if polarity is Polarity.MASK else (255, 0)
    bw = canvas.point(lambda v: paper if v >= 128 else ink, mode="1")
    buf = io.BytesIO()
    bw.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
