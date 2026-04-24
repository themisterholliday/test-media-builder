"""Create synthetic source assets if real ones are not supplied.

Produces /sources/source_master.mov, source_audio.wav, source_sub.ass using
ffmpeg's built-in synthetic generators (testsrc2, sine) plus a per-second
timestamped ASS subtitle track. Idempotent with duration awareness — if a
*synthesized* source is present but its duration does not match the target,
it is regenerated so the whole library stays consistent. User-supplied source
files (tracked via a sentinel under sources/.synthetic_sources.json) are
never overwritten, only warned about if duration mismatches.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .ffutil import FFExecError, ffprobe, run_ffmpeg

log = logging.getLogger("bootstrap")

DURATION_TOLERANCE_SECONDS = 0.5
SENTINEL_NAME = ".synthetic_sources.json"

SUB_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,1,2,10,10,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _fmt_ass_time(seconds: float) -> str:
    """Format seconds as H:MM:SS.cc for ASS Dialogue timing."""
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_subtitle_ass(duration_seconds: int) -> str:
    """Emit an ASS subtitle file with one 1-second cue per second of duration.

    Each cue's text is `[HH:MM:SS] Cue N of TOTAL — sync tracking` so that a
    viewer can visually confirm A/V/subtitle alignment at any point in playback.
    """
    lines: list[str] = []
    for i in range(duration_seconds):
        start = _fmt_ass_time(i)
        end = _fmt_ass_time(min(i + 1, duration_seconds))
        h = i // 3600
        m = (i % 3600) // 60
        s = i % 60
        shown_ts = f"{h:02d}:{m:02d}:{s:02d}"
        text = f"[{shown_ts}] Cue {i + 1} of {duration_seconds} — timestamp sync sample"
        lines.append(
            f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}"
        )
    return SUB_ASS_HEADER + "\n".join(lines) + "\n"


def _probe_duration(path: Path) -> float | None:
    """Return the ffprobe-reported duration in seconds, or None if unavailable."""
    try:
        probe = ffprobe(path)
    except FFExecError:
        return None
    fmt = probe.get("format") or {}
    if fmt.get("duration") is None:
        return None
    try:
        return float(fmt["duration"])
    except (TypeError, ValueError):
        return None


def _duration_matches(path: Path, target: float) -> bool:
    if not path.exists():
        return False
    actual = _probe_duration(path)
    if actual is None:
        return False
    return abs(actual - target) <= DURATION_TOLERANCE_SECONDS


def _subtitle_cue_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return path.read_text(encoding="utf-8", errors="replace").count("Dialogue:")
    except OSError:
        return 0


def _read_sentinel(sources_dir: Path) -> dict:
    path = sources_dir / SENTINEL_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_sentinel_entry(sources_dir: Path, name: str, duration: int) -> None:
    data = _read_sentinel(sources_dir)
    data[name] = {
        "synthetic": True,
        "duration_seconds": duration,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (sources_dir / SENTINEL_NAME).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _is_synthetic(sources_dir: Path, name: str) -> bool:
    """True if the named source file was produced by a prior bootstrap (per sentinel)."""
    entry = _read_sentinel(sources_dir).get(name)
    return bool(entry and entry.get("synthetic"))


def ensure_sources(
    sources_dir: Path,
    duration: int = 300,
    width: int = 1920,
    height: int = 1080,
    framerate: int = 24,
) -> None:
    """Create or refresh synthetic source assets so they match `duration` seconds.

    Regenerates any source whose actual duration differs from the target.
    Subtitles are regenerated whenever the cue count does not match the target
    duration (one cue per second).
    """
    sources_dir.mkdir(parents=True, exist_ok=True)

    master = sources_dir / "source_master.mov"
    if master.exists() and not _is_synthetic(sources_dir, master.name):
        if not _duration_matches(master, duration):
            actual = _probe_duration(master)
            log.warning("user-supplied %s has duration %.1fs (target %ds) — leaving as-is; "
                        "pass --duration %d to match, or delete it to regenerate synthetically",
                        master.name, actual or 0, duration, int(actual or duration))
        else:
            log.info("user-supplied %s matches target %ds, using as-is", master.name, duration)
    elif _duration_matches(master, duration):
        log.info("synthetic %s matches target %ds, skipping", master.name, duration)
    else:
        action = "regenerating" if master.exists() else "generating"
        log.info("%s synthetic master video (%ds @ %dx%d %dfps): %s",
                 action, duration, width, height, framerate, master)
        run_ffmpeg([
            "-f", "lavfi",
            "-i", f"testsrc2=size={width}x{height}:rate={framerate}:duration={duration}",
            "-c:v", "prores_ks",
            "-profile:v", "3",
            "-pix_fmt", "yuv422p10le",
            str(master),
        ], timeout=max(600, duration * 10))
        _write_sentinel_entry(sources_dir, master.name, duration)

    audio = sources_dir / "source_audio.wav"
    if audio.exists() and not _is_synthetic(sources_dir, audio.name):
        if not _duration_matches(audio, duration):
            actual = _probe_duration(audio)
            log.warning("user-supplied %s has duration %.1fs (target %ds) — leaving as-is",
                        audio.name, actual or 0, duration)
        else:
            log.info("user-supplied %s matches target %ds, using as-is", audio.name, duration)
    elif _duration_matches(audio, duration):
        log.info("synthetic %s matches target %ds, skipping", audio.name, duration)
    else:
        action = "regenerating" if audio.exists() else "generating"
        log.info("%s synthetic master audio (%ds): %s", action, duration, audio)
        run_ffmpeg([
            "-f", "lavfi",
            "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-f", "lavfi",
            "-i", f"sine=frequency=880:sample_rate=48000:duration={duration}",
            "-filter_complex", "[0:a][1:a]amerge=inputs=2[aout]",
            "-map", "[aout]",
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(audio),
        ], timeout=max(300, duration * 5))
        _write_sentinel_entry(sources_dir, audio.name, duration)

    sub = sources_dir / "source_sub.ass"
    if sub.exists() and not _is_synthetic(sources_dir, sub.name):
        have_cues = _subtitle_cue_count(sub)
        log.info("user-supplied %s (%d cues) — leaving as-is", sub.name, have_cues)
    else:
        have_cues = _subtitle_cue_count(sub)
        if have_cues == duration:
            log.info("synthetic %s has %d cues, matches target", sub.name, have_cues)
        else:
            action = "regenerating" if sub.exists() else "generating"
            log.info("%s synthetic subtitle (%d cues, one per second): %s", action, duration, sub)
            sub.write_text(build_subtitle_ass(duration), encoding="utf-8")
            _write_sentinel_entry(sources_dir, sub.name, duration)
