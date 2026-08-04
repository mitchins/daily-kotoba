"""Renders a Word to a 1-bit PNG word card.

Pipeline: draw on an "L" canvas (antialiased text), then hard-threshold to mode "1".
Dithering is deliberately not used — dithered text on a 1-bit e-paper panel reads as
mud; a hard threshold keeps glyph edges crisp.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from daily_kotoba import fonts

if TYPE_CHECKING:
    from daily_kotoba.models import Word

# Bump whenever the layout changes; cache.py folds this into the cache path so stale
# rendered art can never survive a deploy.
RENDER_VERSION = 1

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


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


def render_card(word: Word, width: int, height: int) -> bytes:
    # Type is scaled by `scale`, not raw height: an extreme aspect ratio the size
    # guards still permit (e.g. 96x480) would otherwise pick ~48px text for a 38px
    # line box, leaving nothing that fits — not even the ellipsis. Both factors are
    # loose enough that every sane card size is unaffected.
    scale = min(height, round(width * 0.7))
    p = max(1, round(min(width, height) * 0.06))
    canvas = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(canvas)

    regular = fonts.regular_path()
    bold = fonts.bold_path()
    max_width = width - 2 * p

    # --- badge (top-right) -------------------------------------------------
    badge_size = max(1, round(scale * 0.09))
    badge_font = _get_font(bold, badge_size)
    badge_pad = max(1, round(scale * 0.02))
    bbox = draw.textbbox((0, 0), word.jlpt, font=badge_font, anchor="la")
    box_w = (bbox[2] - bbox[0]) + 2 * badge_pad
    box_h = (bbox[3] - bbox[1]) + 2 * badge_pad
    bx1, by0 = width - p, p
    bx0, by1 = bx1 - box_w, by0 + box_h
    draw.rounded_rectangle(
        [bx0, by0, bx1, by1], radius=max(2, round(box_h * 0.25)), outline=0, width=2
    )
    draw.text(((bx0 + bx1) / 2, (by0 + by1) / 2), word.jlpt, font=badge_font, fill=0, anchor="mm")

    # --- reading + surface ---------------------------------------------------
    rows: list[dict] = []
    if not word.is_kana_only:
        reading_font = _get_font(regular, max(1, round(scale * 0.13)))
        # Fixed size, so unlike the surface it cannot auto-shrink — clamp instead,
        # or an unusually long reading would bleed past the edge.
        reading = _clamp_line(draw, word.reading, reading_font, max_width)
        rows.append(_measure_row(draw, [reading], reading_font))

    surface_lo = max(1, round(scale * 0.12))
    surface_hi = max(surface_lo, round(scale * 0.42))
    surface_size = _fit_surface_size(draw, word.surface, bold, surface_lo, surface_hi, max_width)
    surface_row = _measure_row(draw, [word.surface], _get_font(bold, surface_size))
    rows.append(surface_row)

    # --- gloss (shrinkable) + pos (droppable) --------------------------------
    gloss_size = max(1, round(scale * 0.10))
    gloss_min_size = max(1, round(scale * 0.05))

    def build_gloss(size: int) -> dict:
        font = _get_font(regular, size)
        lines = _wrap_gloss(draw, word.gloss, font, max_width)
        return _measure_row(draw, lines, font)

    gloss_row = build_gloss(gloss_size)

    pos_row: dict | None = None
    if word.pos:
        pos_font = _get_font(regular, max(1, round(scale * 0.075)))
        pos_row = _measure_row(draw, [f"({word.pos})"], pos_font)

    gap = max(1, round(scale * 0.03))
    body_top = p + box_h + gap
    body_bottom = height - p
    available = max(0, body_bottom - body_top)

    def all_rows() -> list[dict]:
        return [*rows, gloss_row, *([pos_row] if pos_row else [])]

    # Degrade order per spec: shrink the gloss first, then drop the POS line — but a
    # few points off the surface is a smaller visual hit than losing the POS line
    # outright, so it's tried first as a narrow safety net (e.g. a very short surface
    # that auto-fit maxed out at height*0.42, missing the budget by just a few px).
    # "Never clip" is the one non-negotiable part of this ordering.
    size = gloss_size
    while _stack_height(all_rows(), gap) > available and size > gloss_min_size:
        size -= 1
        gloss_row = build_gloss(size)

    while _stack_height(all_rows(), gap) > available and surface_size > surface_lo:
        surface_size -= 1
        surface_row = _measure_row(draw, [word.surface], _get_font(bold, surface_size))
        rows[-1] = surface_row

    if _stack_height(all_rows(), gap) > available and pos_row is not None:
        pos_row = None

    final_rows = all_rows()
    stack_h = _stack_height(final_rows, gap)
    y = body_top + max(0.0, (available - stack_h) / 2) if available > stack_h else body_top

    for row in final_rows:
        cursor = y
        for i, line in enumerate(row["lines"]):
            h = row["heights"][i]
            draw.text((width / 2, cursor + h / 2), line, font=row["font"], fill=0, anchor="mm")
            cursor += h + row["inter"]
        y += row["total"] + gap

    # Hard threshold, never dithered: text on a 1-bit e-paper panel needs crisp edges.
    bw = canvas.point(lambda v: 255 if v >= 128 else 0, mode="1")
    buf = io.BytesIO()
    bw.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
