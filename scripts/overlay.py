"""Build drawtext filter fragments for the on-screen PTS / media-info overlay.

Two overlays exist, and they are produced at different stages:

* The *source* overlay is burned into source_master.mov once during bootstrap. It carries
  frame-accurate data (PTS, frame number, timecode) and every variant inherits it, so the
  same reference clock appears in all outputs.
* The *variant* overlay is added during each encode. It carries static per-file facts
  (name, codec, container, resolution) that identify the file from a single screenshot.

Both live here so the escaping and geometry rules are written once.
"""
from __future__ import annotations

# Characters that terminate an option or a filter inside a filtergraph description.
# Escaped per character, so a literal backslash is handled before anything else.
_TEXT_ESCAPES = {
    "\\": r"\\",
    ":": r"\:",
    "'": r"\'",
    "%": r"\%",
    ",": r"\,",
    "[": r"\[",
    "]": r"\]",
    ";": r"\;",
}

# Fallback font. Monospace keeps digit columns from jittering frame to frame.
DEFAULT_FONT = "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"
DEFAULT_FONT_SIZE_RATIO = 0.030
DEFAULT_MARGIN_RATIO = 0.020
DEFAULT_LINE_SPACING = 1.35


def escape_text(value: str) -> str:
    """Escape a literal string for use as a drawtext `text=` value.

    Only static text needs this. Expansion sequences such as %{pts} must not be escaped
    or drawtext prints them verbatim instead of evaluating them.
    """
    return "".join(_TEXT_ESCAPES.get(ch, ch) for ch in str(value))


def is_enabled(overlay: dict | None) -> bool:
    """Return True when the matrix config asks for an on-screen overlay."""
    return bool(overlay) and bool(overlay.get("enabled"))


def _metrics(overlay: dict, height: int) -> tuple[int, int, int]:
    """Return (font_size, margin, line_height) in pixels for a given frame height."""
    font_size = max(10, round(height * float(overlay.get("font_size_ratio", DEFAULT_FONT_SIZE_RATIO))))
    margin = max(4, round(height * float(overlay.get("margin_ratio", DEFAULT_MARGIN_RATIO))))
    line_height = round(font_size * DEFAULT_LINE_SPACING)
    return font_size, margin, line_height


def _drawtext(overlay: dict, *, text: str, font_size: int, x: str, y: str) -> str:
    """Compose a single drawtext filter. `text` must already be escaped if it is literal."""
    font_file = overlay.get("font_file", DEFAULT_FONT)
    font_color = overlay.get("font_color", "white")
    box_color = overlay.get("box_color", "black@0.55")
    return (
        f"drawtext=fontfile='{font_file}'"
        f":text='{text}'"
        f":fontcolor={font_color}"
        f":fontsize={font_size}"
        f":box=1:boxcolor={box_color}:boxborderw={max(2, font_size // 6)}"
        f":x={x}:y={y}"
    )


def source_overlay_filters(overlay: dict, height: int) -> list[str]:
    """Return drawtext filters carrying per-frame timing data, anchored top-left.

    Emits the presentation timestamp in seconds, the frame index, and an HH:MM:SS.mmm
    reading. These are ffmpeg expansions evaluated per frame, not literal text.
    """
    if not is_enabled(overlay):
        return []
    font_size, margin, line_height = _metrics(overlay, height)
    rows = [
        r"PTS %{pts\:flt}  FRAME %{n}",
        r"TC  %{pts\:hms}",
    ]
    return [
        _drawtext(overlay, text=row, font_size=font_size, x=str(margin), y=str(margin + i * line_height))
        for i, row in enumerate(rows)
    ]


def variant_overlay_filters(overlay: dict, height: int, lines: list[str]) -> list[str]:
    """Return drawtext filters carrying static per-file text, anchored bottom-left.

    Anchored with the `h` expression rather than a computed constant so the text stays
    inside the frame even if a later filter changes the height.
    """
    if not is_enabled(overlay) or not lines:
        return []
    font_size, margin, line_height = _metrics(overlay, height)
    filters = []
    total = len(lines)
    for i, line in enumerate(lines):
        # Stack upward from the bottom margin: the last line sits closest to the edge.
        offset = (total - i) * line_height
        filters.append(
            _drawtext(
                overlay,
                text=escape_text(line),
                font_size=font_size,
                x=str(margin),
                y=f"h-{margin}-{offset}",
            )
        )
    return filters


def describe(overlay: dict) -> dict:
    """Summarize what the overlay puts on screen, for the NFO and manifest records."""
    if not is_enabled(overlay):
        return {"enabled": "false"}
    return {
        "enabled": "true",
        "source_fields": "pts,frame,timecode",
        "source_position": "top-left",
        "variant_fields": "name,codec,container,resolution,fps,audio",
        "variant_position": "bottom-left",
    }
