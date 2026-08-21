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

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .ffutil import FFExecError, ffprobe, run_ffmpeg
from .overlay import is_enabled as overlay_enabled, source_overlay_filters

log = logging.getLogger("bootstrap")

DURATION_TOLERANCE_SECONDS = 0.5
SENTINEL_NAME = ".synthetic_sources.json"

IMAGE_DIR_NAME = "images"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
DEFAULT_IMAGE_HOLD_SECONDS = 5
SLIDESHOW_CYCLE_NAME = ".slideshow_cycle.mov"

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


def _write_sentinel_entry(sources_dir: Path, name: str, duration: int, signature: str = "") -> None:
    data = _read_sentinel(sources_dir)
    data[name] = {
        "synthetic": True,
        "duration_seconds": duration,
        "signature": signature,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (sources_dir / SENTINEL_NAME).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _is_synthetic(sources_dir: Path, name: str) -> bool:
    """True if the named source file was produced by a prior bootstrap (per sentinel)."""
    entry = _read_sentinel(sources_dir).get(name)
    return bool(entry and entry.get("synthetic"))


def _signature_matches(sources_dir: Path, name: str, signature: str) -> bool:
    """True if the recorded build signature for a synthetic source is still current.

    Duration alone cannot detect a changed overlay or a changed image set — both
    produce a file of identical length that looks different.
    """
    entry = _read_sentinel(sources_dir).get(name) or {}
    return entry.get("signature", "") == signature


def list_source_images(sources_dir: Path) -> list[Path]:
    """Return user-supplied still images, name-sorted so slideshow order is deterministic."""
    image_dir = sources_dir / IMAGE_DIR_NAME
    if not image_dir.is_dir():
        return []
    return sorted(
        p for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def image_set_fingerprint(images: list[Path]) -> str:
    """Fingerprint the image set by name and size, so swapping an image is detected."""
    if not images:
        return ""
    parts = []
    for p in images:
        try:
            parts.append(f"{p.name}:{p.stat().st_size}")
        except OSError:
            parts.append(f"{p.name}:?")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def master_signature(overlay: dict | None, images: list[Path] | None = None,
                     image_hold_seconds: int = DEFAULT_IMAGE_HOLD_SECONDS) -> str:
    """Return a stable fingerprint of every input that changes the master's picture.

    Duration is tracked separately. This covers the overlay settings and the image
    set, neither of which changes the file's length.
    """
    payload = {
        "overlay": overlay or {"enabled": False},
        "images": image_set_fingerprint(images or []),
        "image_hold_seconds": image_hold_seconds if images else 0,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _build_slideshow_cycle(
    cycle_path: Path,
    images: list[Path],
    *,
    width: int,
    height: int,
    framerate: int,
    hold: float,
) -> None:
    """Render one pass through the image set to a normalized intermediate clip.

    Each still is opened as its own input and scaled/padded independently, then the
    results are joined with the concat *filter*. The concat demuxer cannot be used
    here: it requires every input to share a resolution and pixel format, so a set of
    mixed-size images silently truncates to the first one.
    """
    if not images:
        raise ValueError("cannot build a slideshow with no images")

    args: list[str] = []
    for image in images:
        args += ["-loop", "1", "-framerate", str(framerate), "-t", str(hold), "-i", str(image)]

    chains = [
        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={framerate}[v{i}]"
        for i in range(len(images))
    ]
    labels = "".join(f"[v{i}]" for i in range(len(images)))
    chains.append(f"{labels}concat=n={len(images)}:v=1:a=0[out]")

    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[out]",
        "-c:v", "prores_ks",
        "-profile:v", "3",
        "-pix_fmt", "yuv422p10le",
        str(cycle_path),
    ]
    run_ffmpeg(args, timeout=max(300, int(hold * len(images)) * 20))


def ensure_sources(
    sources_dir: Path,
    duration: int = 300,
    width: int = 1920,
    height: int = 1080,
    framerate: int = 24,
    overlay: dict | None = None,
    image_mode: str = "auto",
    image_hold_seconds: int = DEFAULT_IMAGE_HOLD_SECONDS,
    source_audio: str = "source_audio.wav",
) -> None:
    """Create or refresh synthetic source assets so they match `duration` seconds.

    Regenerates any source whose actual duration differs from the target, or whose
    recorded build signature no longer matches the requested overlay and image set.
    Subtitles are regenerated whenever the cue count does not match the target
    duration (one cue per second).

    `image_mode` selects the master's picture content:
      "auto"      use images from sources/images/ when present, else the test pattern
      "slideshow" require images, warning and falling back if the directory is empty
      "none"      always use the synthetic test pattern
    """
    sources_dir.mkdir(parents=True, exist_ok=True)

    master = sources_dir / "source_master.mov"
    images = list_source_images(sources_dir) if image_mode != "none" else []
    if image_mode == "slideshow" and not images:
        log.warning("image_mode is 'slideshow' but no images found under %s/%s — "
                    "falling back to the synthetic test pattern",
                    sources_dir, IMAGE_DIR_NAME)
    signature = master_signature(overlay, images, image_hold_seconds)
    if master.exists() and not _is_synthetic(sources_dir, master.name):
        if not _duration_matches(master, duration):
            actual = _probe_duration(master)
            log.warning("user-supplied %s has duration %.1fs (target %ds) — leaving as-is; "
                        "pass --duration %d to match, or delete it to regenerate synthetically",
                        master.name, actual or 0, duration, int(actual or duration))
        else:
            log.info("user-supplied %s matches target %ds, using as-is", master.name, duration)
    elif _duration_matches(master, duration) and _signature_matches(sources_dir, master.name, signature):
        log.info("synthetic %s matches target %ds, skipping", master.name, duration)
    else:
        action = "regenerating" if master.exists() else "generating"
        source_label = f"{len(images)} image(s)" if images else "testsrc2 pattern"
        log.info("%s synthetic master video (%ds @ %dx%d %dfps, source=%s, overlay=%s): %s",
                 action, duration, width, height, framerate, source_label,
                 "on" if overlay_enabled(overlay) else "off", master)

        cycle_path = sources_dir / SLIDESHOW_CYCLE_NAME
        if images:
            # Two passes: normalize one cycle, then loop it to fill the duration. The
            # cycle is already uniform, so looping it is safe where concat was not.
            _build_slideshow_cycle(
                cycle_path, images,
                width=width, height=height, framerate=framerate, hold=image_hold_seconds,
            )
            args = ["-stream_loop", "-1", "-i", str(cycle_path)]
        else:
            args = [
                "-f", "lavfi",
                "-i", f"testsrc2=size={width}x{height}:rate={framerate}:duration={duration}",
            ]

        # Overlay is drawn last so the text is never scaled or padded with the picture.
        filters = source_overlay_filters(overlay or {}, height)
        if filters:
            args += ["-vf", ",".join(filters)]
        args += [
            "-t", str(duration),
            "-c:v", "prores_ks",
            "-profile:v", "3",
            "-pix_fmt", "yuv422p10le",
            str(master),
        ]
        try:
            run_ffmpeg(args, timeout=max(600, duration * 10))
        finally:
            cycle_path.unlink(missing_ok=True)
        _write_sentinel_entry(sources_dir, master.name, duration, signature)

    audio = sources_dir / source_audio
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
