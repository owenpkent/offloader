"""Font registration.

The reference report is set in Verdana, which ships with Windows and macOS. We
register the real face when we can find it and fall back to DejaVu Sans (bundled
with matplotlib/many Linux distros) and finally Helvetica, so report generation
never hard-fails on a bare CI container.
"""

from __future__ import annotations

import platform
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from . import layout

_FONT_DIRS = [
    Path(r"C:\Windows\Fonts"),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/usr/share/fonts/truetype/msttcorefonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/TTF"),
    Path.home() / ".fonts",
]

#: (registered name, [candidate filenames]) in preference order.
_CANDIDATES = {
    layout.FONT_REGULAR: ["verdana.ttf", "Verdana.ttf", "DejaVuSans.ttf"],
    layout.FONT_BOLD: ["verdanab.ttf", "Verdana Bold.ttf", "DejaVuSans-Bold.ttf"],
}

_FALLBACK = {
    layout.FONT_REGULAR: "Helvetica",
    layout.FONT_BOLD: "Helvetica-Bold",
}

_resolved: dict[str, str] | None = None


def _find(filenames: list[str]) -> Path | None:
    for directory in _FONT_DIRS:
        if not directory.is_dir():
            continue
        for filename in filenames:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    return None


def register() -> dict[str, str]:
    """Register the report fonts and return {logical name: usable font name}.

    Idempotent — repeated calls reuse the first resolution.
    """
    global _resolved
    if _resolved is not None:
        return _resolved

    resolved: dict[str, str] = {}
    for name, filenames in _CANDIDATES.items():
        path = _find(filenames)
        if path is not None:
            try:
                pdfmetrics.registerFont(TTFont(name, str(path)))
                resolved[name] = name
                continue
            except Exception:  # pragma: no cover - corrupt/locked font file
                pass
        resolved[name] = _FALLBACK[name]

    _resolved = resolved
    return resolved


def font(name: str) -> str:
    """Map a logical font name to whatever is actually available."""
    return register().get(name, _FALLBACK.get(name, "Helvetica"))


def using_reference_fonts() -> bool:
    """True when the real Verdana was found, i.e. metrics match the reference."""
    mapping = register()
    return mapping.get(layout.FONT_REGULAR) == layout.FONT_REGULAR


def describe() -> str:
    mapping = register()
    return (
        f"{mapping[layout.FONT_REGULAR]}/{mapping[layout.FONT_BOLD]}"
        f" on {platform.system()}"
    )
