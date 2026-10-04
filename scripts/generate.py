"""Generate the media test library from config/media_matrix.json.

Usage:
    python -m scripts.generate                     # generate everything
    python -m scripts.generate --only video,edge   # only selected sections
    python -m scripts.generate --variant h264_mp4_baseline
    python -m scripts.generate --check             # dry-run status report
    python -m scripts.generate --dry-run           # print commands, don't execute
    python -m scripts.generate --force             # rebuild even if output exists
    python -m scripts.generate --bootstrap         # synthesize missing sources
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import platform
import re
import shutil
import tempfile
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import chapters as chapters_mod
from .bootstrap import ensure_sources, list_source_images
from .encoders import (
    build_audio_only,
    identity_metadata_args,
    build_edge,
    build_stream,
    build_subtitle_embedded,
    build_subtitle_external,
    build_video,
    codec_family,
    effective_duration,
)
from .ffutil import (
    FFBinaryMissing,
    FFCapabilityMissing,
    FFExecError,
    configure_binaries,
    config_sha256,
    ffmpeg_bin,
    ffmpeg_version,
    ffprobe,
    require_binaries,
    require_filters,
    resolve_binary,
    sha256_file,
    run_ffmpeg,
)
from .nfo_writer import build_nfo, write_nfo
from .overlay import describe as describe_overlay

log = logging.getLogger("generate")

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
GENERATED_DIR = ROOT / "generated"
CONFIG_PATH = ROOT / "config" / "media_matrix.json"

SECTIONS = ("video", "masters", "audio", "subtitles", "streams", "edge", "external")

CONTAINER_EXT = {
    "mp4": "mp4",
    "mkv": "mkv",
    "webm": "webm",
    "ts": "ts",
    "m4a": "m4a",
    "ogg": "ogg",
    "ac3": "ac3",
    "eac3": "eac3",
    "mp3": "mp3",
    "flac": "flac",
    "wav": "wav",
    "avi": "avi",
    "wmv": "wmv",
    "m2ts": "m2ts",
    "mov": "mov",
    "3gp": "3gp",
    "mxf": "mxf",
    "ogv": "ogv",
    "vob": "vob",
}


@dataclass
class Plan:
    """Resolved plan for a single matrix entry."""
    variant: dict
    section: str
    out_path: Path
    nfo_path: Path
    kind: str  # "video" | "audio" | "subtitle_external" | "subtitle_embedded" | "stream" | "edge" | "external"
    stream_manifest: Path | None = None


@dataclass
class Outcome:
    plan: Plan
    status: str  # "generated" | "skipped" | "failed" | "pending_external" | "contract_failed"
    message: str = ""
    ffmpeg_cmd: list[str] | None = None
    probe: dict | None = None


def _load_config(path: Path) -> dict:
    """Load the matrix config and apply any ffmpeg binary paths it declares.

    Every entry point (generate, verify, manifest, thumbnails) goes through here, so
    the configured binary applies uniformly without each script repeating the wiring.
    """
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    defaults = cfg.get("defaults") or {}
    configure_binaries(defaults.get("ffmpeg_bin"), defaults.get("ffprobe_bin"))
    global _SOURCE_DEFAULTS
    _SOURCE_DEFAULTS = defaults
    return cfg


# Source filenames come from the matrix. Captured at config load so the handful of
# helpers that need them do not each have to be threaded the defaults dict.
_SOURCE_DEFAULTS: dict = {}


def _source_names(defaults: dict) -> tuple[str, str, str]:
    """Return (video, audio, subtitle) source filenames as declared in the matrix."""
    return (
        defaults.get("source_video", "source_master.mov"),
        defaults.get("source_audio", "source_audio.wav"),
        defaults.get("source_subtitle", "source_sub.ass"),
    )


def _filename_for(variant: dict, container: str) -> str:
    ext = CONTAINER_EXT.get(container, container)
    return f"{variant['name']}.{ext}"


def _plan_video(variant: dict) -> Plan:
    container = variant["container"]
    fname = _filename_for(variant, container)
    out_path = GENERATED_DIR / "video" / fname
    nfo_path = out_path.with_suffix(".nfo")
    return Plan(variant=variant, section="video", out_path=out_path, nfo_path=nfo_path, kind="video")


def _plan_audio_only(variant: dict) -> Plan:
    container = variant["container"]
    fname = _filename_for(variant, container)
    out_path = GENERATED_DIR / "audio" / fname
    nfo_path = out_path.with_suffix(".nfo")
    return Plan(variant=variant, section="audio", out_path=out_path, nfo_path=nfo_path, kind="audio")


def _plan_subtitle(variant: dict) -> Plan:
    if variant.get("embed"):
        container = variant["container"]
        fname = _filename_for(variant, container)
        out_path = GENERATED_DIR / "subtitles" / fname
        kind = "subtitle_embedded"
    else:
        ext = {"srt": "srt", "webvtt": "vtt", "ass": "ass"}[variant["format"]]
        out_path = GENERATED_DIR / "subtitles" / f"{variant['name']}.{ext}"
        kind = "subtitle_external"
    nfo_path = out_path.with_suffix(".nfo")
    return Plan(variant=variant, section="subtitles", out_path=out_path, nfo_path=nfo_path, kind=kind)


def _plan_stream(variant: dict) -> Plan:
    out_dir = GENERATED_DIR / "streams" / variant["name"]
    manifest_name = "playlist.m3u8" if variant["type"] == "hls" else "manifest.mpd"
    manifest_path = out_dir / manifest_name
    nfo_path = out_dir.parent / f"{variant['name']}.nfo"
    return Plan(
        variant=variant,
        section="streams",
        out_path=out_dir,
        nfo_path=nfo_path,
        kind="stream",
        stream_manifest=manifest_path,
    )


def _plan_edge(variant: dict) -> Plan:
    container = variant["container"]
    fname = _filename_for(variant, container)
    out_path = GENERATED_DIR / "edge" / fname
    nfo_path = out_path.with_suffix(".nfo")
    return Plan(variant=variant, section="edge", out_path=out_path, nfo_path=nfo_path, kind="edge")


def _plan_master(variant: dict) -> Plan:
    """Plan a real-content master.

    Masters are ingested exactly like an external sample -- the file already exists
    and is copied in, never re-encoded -- so the kind stays "external" while the
    section distinguishes them in reports and in the library tree.
    """
    fname = _filename_for(variant, variant["container"])
    out_path = GENERATED_DIR / "masters" / fname
    return Plan(variant=variant, section="masters", out_path=out_path,
                nfo_path=out_path.with_suffix(".nfo"), kind="external")


def _plan_external(variant: dict) -> Plan:
    container = variant["container"]
    fname = _filename_for(variant, container)
    out_path = GENERATED_DIR / "external" / fname
    nfo_path = out_path.with_suffix(".nfo")
    return Plan(variant=variant, section="external", out_path=out_path, nfo_path=nfo_path, kind="external")


def _all_plans(cfg: dict) -> list[Plan]:
    plans: list[Plan] = []
    plans += [_plan_video(v) for v in cfg.get("video_variants", [])]
    plans += [_plan_audio_only(v) for v in cfg.get("audio_only_variants", [])]
    plans += [_plan_subtitle(v) for v in cfg.get("subtitle_variants", [])]
    plans += [_plan_stream(v) for v in cfg.get("stream_variants", [])]
    plans += [_plan_edge(v) for v in cfg.get("edge_cases", [])]
    plans += [_plan_master(v) for v in cfg.get("master_variants", [])]
    plans += [_plan_external(v) for v in cfg.get("external_variants", [])]
    return plans


def _filter_plans(plans: list[Plan], *, sections: set[str] | None, variant_names: set[str] | None) -> list[Plan]:
    out = plans
    if sections:
        out = [p for p in out if p.section in sections]
    if variant_names:
        out = [p for p in out if p.variant["name"] in variant_names]
    return out


def _verify_contract(plan: Plan, probe: dict) -> list[str]:
    """Assert structural contract declared in matrix matches ffprobe output. Returns list of errors (empty if OK)."""
    errs: list[str] = []
    v = plan.variant
    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    if plan.kind == "audio":
        if video_streams:
            errs.append(f"audio-only variant has {len(video_streams)} video stream(s)")
        if len(audio_streams) != 1:
            errs.append(f"expected 1 audio stream, got {len(audio_streams)}")
        return errs

    if plan.kind == "subtitle_external":
        if video_streams or audio_streams:
            errs.append("external subtitle must have no video or audio")
        if len(subtitle_streams) != 1:
            errs.append(f"expected 1 subtitle stream, got {len(subtitle_streams)}")
        return errs

    if plan.kind == "edge":
        flags = set(v.get("flags", []))
        if "no_audio" in flags:
            if audio_streams:
                errs.append("no_audio edge case produced an audio stream")
            if len(video_streams) != 1:
                errs.append(f"expected 1 video stream, got {len(video_streams)}")
            return errs
        if "audio_only" in flags:
            if video_streams:
                errs.append("audio_only edge case produced a video stream")
            if len(audio_streams) != 1:
                errs.append(f"expected 1 audio stream, got {len(audio_streams)}")
            return errs
        if "multi_audio" in flags:
            expected = len(v.get("audio_tracks", []))
            if len(audio_streams) != expected:
                errs.append(f"expected {expected} audio streams, got {len(audio_streams)}")
        if "multi_subtitle" in flags:
            expected = len(v.get("subtitle_tracks", []))
            if len(subtitle_streams) != expected:
                errs.append(f"expected {expected} subtitle streams, got {len(subtitle_streams)}")

    if plan.kind in ("video", "subtitle_embedded", "edge", "stream", "external"):
        if not video_streams:
            if not (plan.kind == "edge" and "audio_only" in set(v.get("flags", []))):
                errs.append("expected at least one video stream")
        elif plan.kind in ("video", "subtitle_embedded", "external"):
            # An ingested file is not built here, so its declaration is the only thing
            # asserting the drop-in is the file the matrix means. Fields are checked
            # only when declared, which keeps the unfilled external slots valid.
            vs = video_streams[0]
            expected_pix = v.get("video", {}).get("pix_fmt")
            if expected_pix and vs.get("pix_fmt") != expected_pix:
                errs.append(f"video pix_fmt mismatch: expected {expected_pix}, got {vs.get('pix_fmt')}")
            expected_codec_family = _codec_family(v.get("video", {}).get("codec", ""))
            if expected_codec_family and vs.get("codec_name") != expected_codec_family:
                errs.append(
                    f"video codec mismatch: expected {expected_codec_family}, got {vs.get('codec_name')}"
                )
            if plan.kind == "external":
                # Geometry, colour and channel count are asserted only for ingested
                # files. A built variant's declared width/height is the content area
                # before `pad_to` and before the scale filter rewrites SAR, so the
                # declaration and the output legitimately differ; that geometry has
                # its own model and its own checks.
                declared_hdr = (v.get("video") or {}).get("hdr") or {}
                for field, key in (("color_primaries", "color_primaries"),
                                   ("color_trc", "color_transfer"),
                                   ("colorspace", "color_space")):
                    want = declared_hdr.get(field)
                    if want and vs.get(key) != want:
                        errs.append(f"video {field} mismatch: expected {want}, got {vs.get(key)}")
                for field in ("width", "height"):
                    want = v.get(field)
                    if want and vs.get(field) != want:
                        errs.append(f"{field} mismatch: expected {want}, got {vs.get(field)}")
                want_ch = (v.get("audio") or {}).get("channels")
                if want_ch and audio_streams and audio_streams[0].get("channels") != want_ch:
                    errs.append(f"audio channels mismatch: expected {want_ch}, "
                                f"got {audio_streams[0].get('channels')}")

    if plan.kind == "subtitle_embedded":
        if len(subtitle_streams) != 1:
            errs.append(f"expected 1 embedded subtitle, got {len(subtitle_streams)}")

    return errs


def _codec_family(codec: str) -> str:
    """Map an ffmpeg encoder name to the codec family ffprobe will report.

    Delegates to encoders.codec_family so the contract check and the on-screen
    codec label can never disagree.
    """
    return codec_family(codec)


def _chapters_path_for(plan: Plan, defaults: dict) -> Path | None:
    """Write the FFMETADATA sidecar for a variant that declares chapters.

    Written here rather than in encoders.py so the builders stay pure arg-builders.
    The sidecar is kept as a build artifact so the NFO's recorded command can be
    pasted and re-run as-is.
    """
    spec = chapters_mod.spec_for(plan.variant)
    if not spec:
        return None
    path = GENERATED_DIR / "_chapters" / f"{plan.variant['name']}.ffmeta"
    duration = effective_duration(plan.variant, defaults)
    return chapters_mod.write_ffmetadata(path, spec, duration)


def _with_identity(args: list[str], variant: dict) -> list[str]:
    """Insert the identity metadata immediately before the output path.

    ffmpeg applies `-metadata` to the output file it precedes, so position matters:
    the tags must sit after every codec option and before the trailing path.

    A variant flagged `missing_metadata` exists to carry no container tags at all.
    It builds with `-map_metadata -1`, and appending tags after that strip would
    silently defeat the only thing the variant tests, so it is skipped.
    """
    if "missing_metadata" in set(variant.get("flags", [])):
        return args
    stamp = identity_metadata_args(variant)
    if not stamp:
        return args
    return args[:-1] + stamp + args[-1:]


def _build_command(plan: Plan, defaults: dict) -> tuple[list[str], Path]:
    """Return (ffmpeg_args, primary_output_path_for_probe)."""
    v = plan.variant
    chapters_path = _chapters_path_for(plan, defaults)
    if plan.kind == "video":
        return _with_identity(build_video(v, defaults, SOURCES_DIR, plan.out_path, chapters_path), v), plan.out_path
    if plan.kind == "audio":
        return _with_identity(build_audio_only(v, defaults, SOURCES_DIR, plan.out_path), v), plan.out_path
    if plan.kind == "subtitle_external":
        return build_subtitle_external(v, defaults, SOURCES_DIR, plan.out_path), plan.out_path
    if plan.kind == "subtitle_embedded":
        return _with_identity(build_subtitle_embedded(v, defaults, SOURCES_DIR, plan.out_path), v), plan.out_path
    if plan.kind == "stream":
        args, manifest = build_stream(v, defaults, SOURCES_DIR, plan.out_path)
        return args, manifest
    if plan.kind == "edge":
        return _with_identity(build_edge(v, defaults, SOURCES_DIR, plan.out_path, chapters_path), v), plan.out_path
    raise ValueError(f"no command builder for kind: {plan.kind}")


def _should_skip(plan: Plan, *, force: bool) -> bool:
    """Return True if output exists, NFO exists, and sources are not newer than output."""
    if force:
        return False
    if plan.kind == "stream":
        m = plan.stream_manifest
        if not (m is not None and m.exists() and m.stat().st_size > 0 and plan.nfo_path.exists()):
            return False
        out_mtime = m.stat().st_mtime
    else:
        if not (plan.out_path.exists() and plan.out_path.stat().st_size > 0 and plan.nfo_path.exists()):
            return False
        out_mtime = plan.out_path.stat().st_mtime

    # Invalidate when any source (including synthesized ones) is newer than the output.
    for src_name in _source_names(_SOURCE_DEFAULTS):
        src = SOURCES_DIR / src_name
        if src.exists() and src.stat().st_mtime > out_mtime:
            return False
    return True


def _probe_target(plan: Plan) -> Path:
    return plan.stream_manifest if plan.kind == "stream" and plan.stream_manifest else plan.out_path


def _ffmpeg_timeout_for(defaults: dict) -> int:
    """Scale per-ffmpeg timeout with configured output duration.

    Slow encoders (libx265 10-bit HDR at 1080p, libvpx-vp9, libaom-av1) can spend
    many realtime minutes per minute of content. Use 10x content duration as the
    ceiling with a 600s floor — comfortably covers every encoder in the matrix.
    """
    return max(600, int(defaults.get("duration_seconds", 10)) * 10)


def _truncate_file(path: Path, percent: int) -> None:
    """Truncate a file to `percent`% of its current size. Used for truncated-edge variants."""
    if not path.is_file():
        return
    size = path.stat().st_size
    target = max(1, (size * int(percent)) // 100)
    with path.open("r+b") as f:
        f.truncate(target)
    log.info("truncated %s to %d%% (%d bytes)", path.name, percent, target)


def _apply_rotation(path: Path, degrees: int, timeout: int) -> None:
    """Stamp a display-matrix rotation onto an existing file.

    ffmpeg 8 ignores `-metadata:s:v rotate=` — it writes no side data at all. The only
    working route is a stream-copy remux with the input-side -display_rotation flag,
    so this runs as a post-encode step rather than part of the encode.
    """
    # Keep the real extension: ffmpeg selects the muxer from it, and a ".tmp" suffix
    # fails with "Error initializing the muxer".
    tmp = path.with_name(f"{path.stem}.rot-tmp{path.suffix}")
    args = ["-display_rotation", str(degrees), "-i", str(path), "-c", "copy", str(tmp)]
    try:
        run_ffmpeg(args, timeout=timeout)
        tmp.replace(path)
        log.info("applied %d degree rotation to %s", degrees, path.name)
    finally:
        tmp.unlink(missing_ok=True)


def _probe_dash_stream(init: Path) -> dict:
    """Probe one DASH stream by joining its init segment to its first media chunk.

    An init segment carries no frames, so ffprobe falls back to the movie timescale
    and reports nonsense rates such as 12288/1 with avg_frame_rate 0/0. Prepending the
    init to a single chunk gives the true rate. Falls back to the init alone if no
    chunk is present.
    """
    stream_id = init.stem.split("-", 1)[1] if "-" in init.stem else ""
    chunks = sorted(init.parent.glob(f"chunk-{stream_id}-*.m4s")) if stream_id else []
    if not chunks:
        return ffprobe(init)
    fd, tmp_name = tempfile.mkstemp(suffix=".m4s")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(init.read_bytes())
            out.write(chunks[0].read_bytes())
        return ffprobe(tmp)
    except (OSError, FFExecError) as e:
        log.warning("dash chunk probe failed for %s (%s); using init segment only", init.name, e)
        return ffprobe(init)
    finally:
        tmp.unlink(missing_ok=True)


def _probe_dash(manifest_path: Path) -> dict:
    """ffprobe in ffmpeg 8.x has no DASH demuxer — merge probes of each init segment instead."""
    out_dir = manifest_path.parent
    merged_streams: list[dict] = []
    base_format: dict = {}
    total_size = 0
    for child in out_dir.iterdir():
        if child.is_file():
            total_size += child.stat().st_size
    for init in sorted(out_dir.glob("init-*.m4s")):
        probe = _probe_dash_stream(init)
        merged_streams.extend(probe.get("streams", []))
        if not base_format:
            base_format = dict(probe.get("format", {}) or {})
    base_format["format_name"] = "dash"
    base_format["filename"] = str(manifest_path)
    base_format["size"] = str(total_size)
    # The probe covers one init segment plus one chunk, so its duration is that chunk's,
    # not the presentation's. The MPD carries the real total.
    total_duration = _mpd_duration(manifest_path)
    if total_duration is not None:
        base_format["duration"] = f"{total_duration:.6f}"
    return {"streams": merged_streams, "format": base_format}


def _mpd_duration(manifest_path: Path) -> float | None:
    """Read mediaPresentationDuration from a DASH manifest as seconds."""
    try:
        text = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'mediaPresentationDuration="([^"]+)"', text)
    if not match:
        return None
    # ISO-8601 duration, e.g. PT5M0.0S
    iso = re.match(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", match.group(1))
    if not iso:
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in iso.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _probe_for(plan: Plan) -> dict:
    target = _probe_target(plan)
    if plan.kind == "stream" and plan.variant.get("type") == "dash":
        return _probe_dash(target)
    return ffprobe(target)


def _ingest(src: Path, dst: Path) -> None:
    """Place a supplied file into generated/, hardlinking when the filesystem allows.

    The masters run to gigabytes and are never modified in place, so a hardlink is
    both correct and the difference between a few kilobytes and several gigabytes of
    duplication. A cross-device link or an unsupported filesystem falls back to a copy.
    """
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError as e:
        log.debug("hardlink failed for %s (%s); copying instead", dst.name, e)
        shutil.copy2(src, dst)


def _execute(plan: Plan, defaults: dict, *, dry_run: bool, force: bool, ffmpeg_ver: str) -> Outcome:
    if plan.kind == "external":
        src = Path(plan.variant["source_path"])
        if not src.is_absolute():
            src = ROOT / src
        if not src.exists():
            return Outcome(plan=plan, status="pending_external", message=f"external sample missing: {src}")

        plan.out_path.parent.mkdir(parents=True, exist_ok=True)
        if force or not plan.out_path.exists():
            _ingest(src, plan.out_path)

        probe = _probe_for(plan)
        errs = _verify_contract(plan, probe)
        if errs:
            return Outcome(plan=plan, status="contract_failed", message="; ".join(errs), probe=probe)

        _write_nfo_for(plan, probe=probe, ffmpeg_cmd=None, ffmpeg_version=ffmpeg_ver,
                       extra_source=src, overlay=defaults.get("overlay"))
        return Outcome(plan=plan, status="generated", probe=probe)

    if _should_skip(plan, force=force):
        return Outcome(plan=plan, status="skipped", message="output and nfo already present")

    wanted = effective_duration(plan.variant, defaults)
    if wanted > int(defaults["duration_seconds"]):
        return Outcome(
            plan=plan,
            status="failed",
            message=(f"variant duration {wanted}s exceeds the source duration "
                     f"{defaults['duration_seconds']}s; raise --duration or lower the variant's "
                     f"duration_seconds"),
        )

    args, probe_target = _build_command(plan, defaults)
    if dry_run:
        log.info("DRY-RUN: ffmpeg %s", " ".join(args))
        return Outcome(plan=plan, status="generated", message="dry-run", ffmpeg_cmd=["ffmpeg", *args])

    plan.out_path.parent.mkdir(parents=True, exist_ok=True) if plan.kind != "stream" else None

    ff_timeout = _ffmpeg_timeout_for(defaults)
    try:
        run_ffmpeg(args, timeout=ff_timeout)
    except FFExecError as e:
        return Outcome(plan=plan, status="failed", message=str(e), ffmpeg_cmd=["ffmpeg", *args])

    if not probe_target.exists():
        return Outcome(plan=plan, status="failed", message=f"ffmpeg returned 0 but output missing: {probe_target}")

    flags = set(plan.variant.get("flags", []))
    truncate_pct = plan.variant.get("truncate_to_percent")
    if "truncated" in flags and truncate_pct is not None and plan.kind != "stream":
        _truncate_file(plan.out_path, truncate_pct)

    rotation = (plan.variant.get("video") or {}).get("rotate")
    if rotation and plan.kind != "stream":
        try:
            _apply_rotation(plan.out_path, int(rotation), ff_timeout)
        except FFExecError as e:
            return Outcome(plan=plan, status="failed", message=f"rotation remux failed: {e}")

    probe = _probe_for(plan)
    errs = _verify_contract(plan, probe)
    if "truncated" in flags:
        errs = []
    if errs:
        return Outcome(
            plan=plan,
            status="contract_failed",
            message="; ".join(errs),
            ffmpeg_cmd=["ffmpeg", *args],
            probe=probe,
        )

    _write_nfo_for(plan, probe=probe, ffmpeg_cmd=["ffmpeg", *args], ffmpeg_version=ffmpeg_ver,
                   overlay=defaults.get("overlay"))
    return Outcome(plan=plan, status="generated", ffmpeg_cmd=["ffmpeg", *args], probe=probe)


def _overlay_record(plan: Plan, overlay: dict | None) -> dict:
    """Describe what the on-screen overlay carries for this variant.

    Variants with no video stream, external copies, and per-variant opt-outs all
    record an explicit disabled state rather than being left blank.
    """
    if plan.kind in ("audio", "subtitle_external", "external"):
        return {"enabled": "false", "reason": "no video stream" if plan.kind != "external" else "external copy"}
    if plan.variant.get("burn_in") is False:
        return {"enabled": "false", "reason": "burn_in disabled for this variant"}
    return describe_overlay(overlay or {})


def _write_nfo_for(
    plan: Plan,
    *,
    probe: dict,
    ffmpeg_cmd: list[str] | None,
    ffmpeg_ver: str,
    extra_source: Path | None = None,
    overlay: dict | None = None,
) -> None:
    v = plan.variant
    hdr = (v.get("video") or {}).get("hdr") if isinstance(v.get("video"), dict) else None

    source_refs: list[dict] = []
    for key, fname in zip(
        ("source_video", "source_audio", "source_subtitle"),
        _source_names(_SOURCE_DEFAULTS),
    ):
        src = SOURCES_DIR / fname
        if src.exists() and plan.kind != "external":
            source_refs.append({"kind": key, "file": src.name, "sha256": sha256_file(src)})
    if plan.kind != "external":
        # Record any user-supplied stills that produced the master's picture, so a
        # swapped image is detectable from the NFO alone.
        for image in list_source_images(SOURCES_DIR):
            source_refs.append({
                "kind": "source_image",
                "file": image.name,
                "sha256": sha256_file(image),
            })
    if extra_source is not None:
        source_refs.append({
            "kind": "external",
            "file": str(extra_source.name),
            "path": str(extra_source),
            "sha256": sha256_file(extra_source),
            "license_note": v.get("license_note", ""),
        })

    tags = [v["name"]]
    if v.get("container"):
        tags.append(v["container"])
    for f in v.get("flags", []):
        tags.append(f)

    target = _probe_target(plan)
    output_sha = sha256_file(target) if target.is_file() else None
    output_size = target.stat().st_size if target.is_file() else None

    payload = build_nfo(
        variant_id=v["name"],
        variant_uuid=v.get("uuid"),
        attribution=v.get("attribution"),
        title=v.get("title", v["name"]),
        description=v.get("description", ""),
        tags=tags,
        probe=probe,
        ffmpeg_cmd=ffmpeg_cmd[1:] if ffmpeg_cmd else None,
        ffmpeg_version=ffmpeg_ver,
        source_refs=source_refs,
        edge_flags=v.get("flags", []),
        hdr=hdr if isinstance(hdr, dict) else None,
        output_sha256=output_sha,
        output_size=output_size,
        overlay=_overlay_record(plan, overlay),
    )
    write_nfo(plan.nfo_path, payload)


def _refresh_nfos(plans: list[Plan], defaults: dict, ffmpeg_ver: str) -> int:
    """Rewrite every NFO from the outputs already on disk, without re-encoding.

    Metadata-only fixes (a new NFO field, a corrected derivation) would otherwise
    need a full rebuild, which costs hours for a field that takes a probe to compute.
    The `<command>` element is dropped because the original arguments are not
    recoverable here; a real build restores it.
    """
    written = 0
    for plan in plans:
        target = _probe_target(plan)
        if not target.exists():
            log.info("skip (not built): %s", plan.variant["name"])
            continue
        try:
            probe = _probe_for(plan)
        except (FFExecError, FileNotFoundError) as e:
            log.error("probe failed for %s: %s", plan.variant["name"], e)
            continue
        extra = None
        if plan.kind == "external":
            src = Path(plan.variant["source_path"])
            extra = src if src.is_absolute() else ROOT / src
        _write_nfo_for(
            plan, probe=probe, ffmpeg_cmd=None, ffmpeg_version=ffmpeg_ver,
            extra_source=extra, overlay=defaults.get("overlay"),
        )
        written += 1
    return written


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate the media test library.")
    p.add_argument("--only", default="", help=f"Comma-separated sections to include. Valid: {','.join(SECTIONS)}")
    p.add_argument("--variant", default="", help="Comma-separated variant names to include.")
    p.add_argument("--force", action="store_true", help="Regenerate even if output exists.")
    p.add_argument("--dry-run", action="store_true", help="Print ffmpeg commands without running them.")
    p.add_argument("--check", action="store_true", help="Report what would be built without doing it.")
    p.add_argument("--bootstrap", action="store_true", help="Synthesize missing sources before generating.")
    p.add_argument("--duration", type=int, default=None,
                   help="Override defaults.duration_seconds (output length in seconds).")
    p.add_argument("--duration-minutes", type=float, default=None,
                   help="Override defaults.duration_seconds in minutes (0.5 = 30s, 5 = 5min).")
    p.add_argument("--jobs", "-j", type=int, default=1,
                   help="Parallel worker processes (default 1 = serial).")
    p.add_argument("--no-burn-in", action="store_true",
                   help="Disable the on-screen PTS / media-info overlay for this run.")
    p.add_argument("--refresh-nfo", action="store_true",
                   help="Rewrite NFOs from existing outputs without re-encoding anything.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def _overlay_settings(cfg: dict, *, disabled: bool) -> dict:
    """Resolve on-screen overlay settings from the matrix, honoring --no-burn-in.

    Absent config means disabled, so an older matrix keeps its current behavior.
    """
    overlay = dict((cfg.get("defaults") or {}).get("overlay") or {})
    overlay.setdefault("enabled", False)
    if disabled:
        overlay["enabled"] = False
    return overlay


def _missing_sources() -> list[Path]:
    return [p for p in (SOURCES_DIR / n for n in _source_names(_SOURCE_DEFAULTS))
            if not p.exists()]


def _print_check(plans: list[Plan]) -> int:
    by_section: dict[str, list[Plan]] = {}
    for p in plans:
        by_section.setdefault(p.section, []).append(p)

    total = len(plans)
    existing = 0
    missing_external: list[str] = []
    for p in plans:
        target = _probe_target(p)
        if p.section == "external":
            src = Path(p.variant["source_path"])
            if not src.is_absolute():
                src = ROOT / src
            if not src.exists():
                missing_external.append(f"  - {p.variant['name']}: {src}")
                continue
        if target.exists() and target.stat().st_size > 0 and p.nfo_path.exists():
            existing += 1

    sys.stdout.write(f"total entries: {total}\n")
    for section in SECTIONS:
        entries = by_section.get(section, [])
        sys.stdout.write(f"  {section:>10}: {len(entries)}\n")
    sys.stdout.write(f"already built: {existing}\n")
    sys.stdout.write(f"pending external samples: {len(missing_external)}\n")
    for line in missing_external:
        sys.stdout.write(line + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        require_binaries()
    except FFBinaryMissing as e:
        log.error("precondition failed: %s", e)
        return 2

    cfg = _load_config(CONFIG_PATH)
    defaults = cfg["defaults"]

    if ns.duration is not None and ns.duration_minutes is not None:
        log.error("pass either --duration or --duration-minutes, not both")
        return 2
    if ns.duration is not None:
        defaults["duration_seconds"] = int(ns.duration)
    elif ns.duration_minutes is not None:
        defaults["duration_seconds"] = int(round(ns.duration_minutes * 60))
    log.info("target duration: %d seconds", defaults["duration_seconds"])

    overlay = _overlay_settings(cfg, disabled=ns.no_burn_in)
    defaults["overlay"] = overlay
    if overlay.get("enabled"):
        if ns.check or ns.dry_run:
            log.info("on-screen overlay: enabled (not executing)")
        else:
            try:
                require_filters("drawtext")
            except FFCapabilityMissing as e:
                log.error("precondition failed: %s", e)
                return 2
            log.info("on-screen overlay: enabled")
    else:
        log.info("on-screen overlay: disabled")

    if ns.bootstrap:
        ensure_sources(SOURCES_DIR, duration=defaults["duration_seconds"], overlay=overlay,
                       image_mode=defaults.get("image_mode", "auto"),
                       image_hold_seconds=defaults.get("image_hold_seconds", 5),
                       source_audio=defaults.get("source_audio", "source_audio.wav"))

    missing = _missing_sources()
    is_real_run = not (ns.check or ns.dry_run)
    if missing and is_real_run:
        log.warning("missing sources — auto-bootstrapping synthetic test patterns")
        ensure_sources(SOURCES_DIR, duration=defaults["duration_seconds"], overlay=overlay,
                       image_mode=defaults.get("image_mode", "auto"),
                       image_hold_seconds=defaults.get("image_hold_seconds", 5),
                       source_audio=defaults.get("source_audio", "source_audio.wav"))
    elif missing:
        log.warning("missing sources (run --bootstrap or supply them): %s",
                    [str(m) for m in missing])
    elif is_real_run:
        # Sources exist; if duration changed, refresh them so downstream outputs rebuild.
        ensure_sources(SOURCES_DIR, duration=defaults["duration_seconds"], overlay=overlay,
                       image_mode=defaults.get("image_mode", "auto"),
                       image_hold_seconds=defaults.get("image_hold_seconds", 5),
                       source_audio=defaults.get("source_audio", "source_audio.wav"))

    plans = _all_plans(cfg)
    sections = {s.strip() for s in ns.only.split(",") if s.strip()} or None
    variant_names = {s.strip() for s in ns.variant.split(",") if s.strip()} or None
    plans = _filter_plans(plans, sections=sections, variant_names=variant_names)

    if ns.check:
        return _print_check(plans)

    if ns.refresh_nfo:
        count = _refresh_nfos(plans, defaults, ffmpeg_version())
        sys.stdout.write(f"refreshed {count} nfo file(s)\n")
        return 0

    ffv = ffmpeg_version()
    outcomes: list[Outcome] = []

    def _run_one(plan: Plan) -> Outcome:
        try:
            return _execute(plan, defaults, dry_run=ns.dry_run, force=ns.force, ffmpeg_ver=ffv)
        except Exception as e:  # noqa: BLE001
            return Outcome(plan=plan, status="failed", message=repr(e))

    def _log_outcome(outcome: Outcome) -> None:
        plan = outcome.plan
        if outcome.status in ("failed", "contract_failed"):
            log.error("[%s] %s: %s", outcome.status, plan.variant["name"], outcome.message)
        elif outcome.status == "pending_external":
            log.warning("pending external: %s (%s)", plan.variant["name"], outcome.message)
        elif outcome.status == "skipped":
            log.info("skipped (exists): %s", plan.variant["name"])
        else:
            log.info("ok: %s", plan.variant["name"])

    if ns.jobs <= 1:
        for plan in plans:
            log.info("[%s/%s] -> %s", plan.section, plan.variant["name"], plan.out_path)
            outcome = _run_one(plan)
            outcomes.append(outcome)
            _log_outcome(outcome)
    else:
        log.info("running with %d parallel workers", ns.jobs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=ns.jobs) as pool:
            futures = {pool.submit(_run_one, plan): plan for plan in plans}
            for fut in concurrent.futures.as_completed(futures):
                outcome = fut.result()
                outcomes.append(outcome)
                _log_outcome(outcome)

    summary: dict[str, int] = {}
    for o in outcomes:
        summary[o.status] = summary.get(o.status, 0) + 1
    sys.stdout.write("\n=== summary ===\n")
    for k in sorted(summary):
        sys.stdout.write(f"{k:>20}: {summary[k]}\n")

    if not ns.dry_run:
        _write_build_manifest(cfg, ffv, outcomes, defaults["duration_seconds"])

    hard_failures = [o for o in outcomes if o.status in ("failed", "contract_failed")]
    return 1 if hard_failures else 0


def _write_build_manifest(cfg: dict, ffmpeg_ver: str, outcomes: list["Outcome"], duration_seconds: int) -> None:
    """Write generated/build_manifest.json with reproducibility metadata."""
    matrix_sha = config_sha256(cfg)
    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "ffmpeg_version": ffmpeg_ver,
        "python_version": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "matrix_sha256": matrix_sha,
        "ffmpeg_path": resolve_binary(ffmpeg_bin()) or ffmpeg_bin(),
        "duration_seconds": duration_seconds,
        "entry_counts": {
            status: sum(1 for o in outcomes if o.status == status)
            for status in sorted({o.status for o in outcomes})
        },
    }
    path = GENERATED_DIR / "build_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("build manifest: %s", path)


if __name__ == "__main__":
    raise SystemExit(main())
