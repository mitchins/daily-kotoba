"""Font path resolution.

Note: Pillow's `Image.getdata()` is deprecated (removal in Pillow 14) — avoid it in
production code paths. `render.py` uses `Image.point()` / direct pixel access via
`Image.load()` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

NOTO_REGULAR = "NotoSansJP-Regular.otf"
NOTO_BOLD = "NotoSansJP-Bold.otf"


def font_dir() -> Path:
    return Path(os.environ.get("KOTOBA_FONT_DIR", "/app/fonts"))


def regular_path() -> Path:
    return font_dir() / NOTO_REGULAR


def bold_path() -> Path:
    return font_dir() / NOTO_BOLD
