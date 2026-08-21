"""Build FFMETADATA chapter sidecars from a matrix `chapters` spec.

Chapter generation is kept pure and separate from ffmpeg so the deliberately
malformed cases (overlapping, zero-length, past end-of-file) can be checked
without running an encode. `resolve` is the single source of truth: the encoder
writes it, and verify.py asserts against it.

Spec shapes, both scaling with the configured duration:

    {"mode": "even", "count": 10, "title_prefix": "Chapter"}
    {"mode": "even", "count": 3, "titles": ["Cold Open", "Titles", "Act One"]}
    {"mode": "explicit", "unit": "fraction",
     "entries": [{"start": 0.0, "end": 0.5, "title": "First"}]}

`unit` is "fraction" (of total duration) or "seconds". Fractions are the default
because they keep a spec meaningful whether the library is built at 30 s or 300 s.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("chapters")

TIMEBASE = "1/1000"
_TIMEBASE_DIVISOR = 1000

# FFMETADATA treats these as syntax; a literal must be backslash-escaped.
_META_ESCAPES = ("\\", "=", ";", "#", "\n")


class ChapterSpecError(ValueError):
    """Raised when a chapters spec cannot be resolved into concrete chapters."""


def escape_meta(value: str) -> str:
    """Escape a string for use as an FFMETADATA value."""
    out = str(value)
    for ch in _META_ESCAPES:
        out = out.replace(ch, "\\" + ch) if ch != "\n" else out.replace(ch, "\\\n")
    return out


def _title_for(spec: dict, index: int, total: int) -> str:
    """Return the title for chapter `index`, from explicit titles or a prefix."""
    titles = spec.get("titles")
    if titles:
        if index < len(titles):
            return str(titles[index])
        # Fewer titles than chapters: keep numbering rather than repeating the last.
        return f"{spec.get('title_prefix', 'Chapter')} {index + 1}"
    return f"{spec.get('title_prefix', 'Chapter')} {index + 1} of {total}"


def _resolve_even(spec: dict, duration_seconds: float) -> list[dict]:
    """Split the duration into `count` equal chapters."""
    count = int(spec.get("count", 0))
    if count < 1:
        raise ChapterSpecError(f"'even' chapters need count >= 1, got {count!r}")
    step_ms = (duration_seconds * _TIMEBASE_DIVISOR) / count
    chapters = []
    for i in range(count):
        start_ms = round(i * step_ms)
        end_ms = round((i + 1) * step_ms)
        chapters.append({
            "index": i,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "title": _title_for(spec, i, count),
        })
    return chapters


def _resolve_explicit(spec: dict, duration_seconds: float) -> list[dict]:
    """Convert declared entries into millisecond chapters, scaling fractions by duration."""
    entries = spec.get("entries") or []
    if not entries:
        raise ChapterSpecError("'explicit' chapters need a non-empty 'entries' list")
    unit = spec.get("unit", "fraction")
    if unit not in ("fraction", "seconds"):
        raise ChapterSpecError(f"unknown chapters unit: {unit!r}")
    scale = duration_seconds * _TIMEBASE_DIVISOR if unit == "fraction" else _TIMEBASE_DIVISOR

    chapters = []
    for i, entry in enumerate(entries):
        try:
            start_ms = round(float(entry["start"]) * scale)
            end_ms = round(float(entry["end"]) * scale)
        except (KeyError, TypeError, ValueError) as e:
            raise ChapterSpecError(f"entry {i} needs numeric 'start' and 'end': {entry!r}") from e
        chapters.append({
            "index": i,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "title": str(entry.get("title") or _title_for(spec, i, len(entries))),
        })
    return chapters


def resolve(spec: dict, duration_seconds: float) -> list[dict]:
    """Resolve a chapters spec into concrete chapters.

    Returns dicts of index, start_ms, end_ms and title. Deliberately does not
    validate ordering or bounds — the malformed variants exist precisely to
    produce chapters that overlap, invert, or run past the end of the file.
    """
    if not spec:
        return []
    mode = spec.get("mode", "even")
    if mode == "even":
        return _resolve_even(spec, duration_seconds)
    if mode == "explicit":
        return _resolve_explicit(spec, duration_seconds)
    raise ChapterSpecError(f"unknown chapters mode: {mode!r}")


def build_ffmetadata(spec: dict, duration_seconds: float) -> str:
    """Render a chapters spec as an FFMETADATA1 document."""
    chapters = resolve(spec, duration_seconds)
    lines = [";FFMETADATA1"]
    for chapter in chapters:
        lines += [
            "[CHAPTER]",
            f"TIMEBASE={TIMEBASE}",
            f"START={chapter['start_ms']}",
            f"END={chapter['end_ms']}",
            f"title={escape_meta(chapter['title'])}",
        ]
    return "\n".join(lines) + "\n"


def write_ffmetadata(path: Path, spec: dict, duration_seconds: float) -> Path:
    """Write the FFMETADATA sidecar for a spec and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ffmetadata(spec, duration_seconds), encoding="utf-8")
    log.debug("wrote chapter metadata: %s", path)
    return path


def spec_for(variant: dict) -> dict | None:
    """Return a variant's chapters spec, or None when it declares no chapters."""
    spec = variant.get("chapters")
    return spec if isinstance(spec, dict) and spec else None
