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
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .bootstrap import ensure_sources
from .encoders import (
    build_audio_only,
    build_edge,
    build_stream,
    build_subtitle_embedded,
    build_subtitle_external,
    build_video,
)
from .ffutil import FFBinaryMissing, FFExecError, ffprobe, require_binaries, run_ffmpeg
from .nfo_writer import build_nfo, write_nfo

log = logging.getLogger("generate")

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
GENERATED_DIR = ROOT / "generated"
CONFIG_PATH = ROOT / "config" / "media_matrix.json"

SECTIONS = ("video", "audio", "subtitles", "streams", "edge", "external")

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


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    """Compute hex SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ffmpeg_version() -> str:
    """Return the running ffmpeg binary version string, or 'unknown'."""
    try:
        out = subprocess.check_output(["ffmpeg", "-version"], text=True, timeout=10)
        m = re.search(r"ffmpeg version (\S+)", out)
        return m.group(1) if m else "unknown"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def _load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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
        elif plan.kind == "video" or plan.kind == "subtitle_embedded":
            vs = video_streams[0]
            expected_pix = v.get("video", {}).get("pix_fmt")
            if expected_pix and vs.get("pix_fmt") != expected_pix:
                errs.append(f"video pix_fmt mismatch: expected {expected_pix}, got {vs.get('pix_fmt')}")
            expected_codec_family = _codec_family(v.get("video", {}).get("codec", ""))
            if expected_codec_family and vs.get("codec_name") != expected_codec_family:
                errs.append(
                    f"video codec mismatch: expected {expected_codec_family}, got {vs.get('codec_name')}"
                )

    if plan.kind == "subtitle_embedded":
        if len(subtitle_streams) != 1:
            errs.append(f"expected 1 embedded subtitle, got {len(subtitle_streams)}")

    return errs


def _codec_family(codec: str) -> str:
    """Map an ffmpeg encoder name to the codec family ffprobe will report."""
    mapping = {
        "libx264": "h264",
        "libx265": "hevc",
        "libaom-av1": "av1",
        "libsvtav1": "av1",
        "librav1e": "av1",
        "libvpx-vp9": "vp9",
        "libopus": "opus",
        "libmp3lame": "mp3",
        "mpeg2video": "mpeg2video",
        "mpeg4": "mpeg4",
        "wmv1": "wmv1",
        "wmv2": "wmv2",
    }
    return mapping.get(codec, codec)


def _build_command(plan: Plan, defaults: dict) -> tuple[list[str], Path]:
    """Return (ffmpeg_args, primary_output_path_for_probe)."""
    v = plan.variant
    if plan.kind == "video":
        return build_video(v, defaults, SOURCES_DIR, plan.out_path), plan.out_path
    if plan.kind == "audio":
        return build_audio_only(v, defaults, SOURCES_DIR, plan.out_path), plan.out_path
    if plan.kind == "subtitle_external":
        return build_subtitle_external(v, defaults, SOURCES_DIR, plan.out_path), plan.out_path
    if plan.kind == "subtitle_embedded":
        return build_subtitle_embedded(v, defaults, SOURCES_DIR, plan.out_path), plan.out_path
    if plan.kind == "stream":
        args, manifest = build_stream(v, defaults, SOURCES_DIR, plan.out_path)
        return args, manifest
    if plan.kind == "edge":
        return build_edge(v, defaults, SOURCES_DIR, plan.out_path), plan.out_path
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
    for src_name in ("source_master.mov", "source_audio.wav", "source_sub.ass"):
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
        probe = ffprobe(init)
        merged_streams.extend(probe.get("streams", []))
        if not base_format:
            base_format = dict(probe.get("format", {}) or {})
    base_format["format_name"] = "dash"
    base_format["filename"] = str(manifest_path)
    base_format["size"] = str(total_size)
    return {"streams": merged_streams, "format": base_format}


def _probe_for(plan: Plan) -> dict:
    target = _probe_target(plan)
    if plan.kind == "stream" and plan.variant.get("type") == "dash":
        return _probe_dash(target)
    return ffprobe(target)


def _execute(plan: Plan, defaults: dict, *, dry_run: bool, force: bool, ffmpeg_version: str) -> Outcome:
    if plan.kind == "external":
        src = Path(plan.variant["source_path"])
        if not src.is_absolute():
            src = ROOT / src
        if not src.exists():
            return Outcome(plan=plan, status="pending_external", message=f"external sample missing: {src}")

        plan.out_path.parent.mkdir(parents=True, exist_ok=True)
        if force or not plan.out_path.exists():
            shutil.copy2(src, plan.out_path)

        probe = _probe_for(plan)
        errs = _verify_contract(plan, probe)
        if errs:
            return Outcome(plan=plan, status="contract_failed", message="; ".join(errs), probe=probe)

        _write_nfo_for(plan, probe=probe, ffmpeg_cmd=None, ffmpeg_version=ffmpeg_version, extra_source=src)
        return Outcome(plan=plan, status="generated", probe=probe)

    if _should_skip(plan, force=force):
        return Outcome(plan=plan, status="skipped", message="output and nfo already present")

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

    _write_nfo_for(plan, probe=probe, ffmpeg_cmd=["ffmpeg", *args], ffmpeg_version=ffmpeg_version)
    return Outcome(plan=plan, status="generated", ffmpeg_cmd=["ffmpeg", *args], probe=probe)


def _write_nfo_for(
    plan: Plan,
    *,
    probe: dict,
    ffmpeg_cmd: list[str] | None,
    ffmpeg_version: str,
    extra_source: Path | None = None,
) -> None:
    v = plan.variant
    hdr = (v.get("video") or {}).get("hdr") if isinstance(v.get("video"), dict) else None

    source_refs: list[dict] = []
    for key, fname in (
        ("source_video", "source_master.mov"),
        ("source_audio", "source_audio.wav"),
        ("source_subtitle", "source_sub.ass"),
    ):
        src = SOURCES_DIR / fname
        if src.exists() and plan.kind != "external":
            source_refs.append({"kind": key, "file": src.name, "sha256": _sha256(src)})
    if extra_source is not None:
        source_refs.append({
            "kind": "external",
            "file": str(extra_source.name),
            "path": str(extra_source),
            "sha256": _sha256(extra_source),
            "license_note": v.get("license_note", ""),
        })

    tags = [v["name"]]
    if v.get("container"):
        tags.append(v["container"])
    for f in v.get("flags", []):
        tags.append(f)

    target = _probe_target(plan)
    output_sha = _sha256(target) if target.is_file() else None
    output_size = target.stat().st_size if target.is_file() else None

    payload = build_nfo(
        variant_id=v["name"],
        title=v.get("title", v["name"]),
        description=v.get("description", ""),
        tags=tags,
        probe=probe,
        ffmpeg_cmd=ffmpeg_cmd[1:] if ffmpeg_cmd else None,
        ffmpeg_version=ffmpeg_version,
        source_refs=source_refs,
        edge_flags=v.get("flags", []),
        hdr=hdr if isinstance(hdr, dict) else None,
        output_sha256=output_sha,
        output_size=output_size,
    )
    write_nfo(plan.nfo_path, payload)


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
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def _missing_sources() -> list[Path]:
    return [p for p in (
        SOURCES_DIR / "source_master.mov",
        SOURCES_DIR / "source_audio.wav",
        SOURCES_DIR / "source_sub.ass",
    ) if not p.exists()]


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

    if ns.bootstrap:
        ensure_sources(SOURCES_DIR, duration=defaults["duration_seconds"])

    missing = _missing_sources()
    is_real_run = not (ns.check or ns.dry_run)
    if missing and is_real_run:
        log.warning("missing sources — auto-bootstrapping synthetic test patterns")
        ensure_sources(SOURCES_DIR, duration=defaults["duration_seconds"])
    elif missing:
        log.warning("missing sources (run --bootstrap or supply them): %s",
                    [str(m) for m in missing])
    elif is_real_run:
        # Sources exist; if duration changed, refresh them so downstream outputs rebuild.
        ensure_sources(SOURCES_DIR, duration=defaults["duration_seconds"])

    plans = _all_plans(cfg)
    sections = {s.strip() for s in ns.only.split(",") if s.strip()} or None
    variant_names = {s.strip() for s in ns.variant.split(",") if s.strip()} or None
    plans = _filter_plans(plans, sections=sections, variant_names=variant_names)

    if ns.check:
        return _print_check(plans)

    ffv = _ffmpeg_version()
    outcomes: list[Outcome] = []

    def _run_one(plan: Plan) -> Outcome:
        try:
            return _execute(plan, defaults, dry_run=ns.dry_run, force=ns.force, ffmpeg_version=ffv)
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


def _write_build_manifest(cfg: dict, ffmpeg_version: str, outcomes: list["Outcome"], duration_seconds: int) -> None:
    """Write generated/build_manifest.json with reproducibility metadata."""
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    matrix_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "ffmpeg_version": ffmpeg_version,
        "python_version": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "matrix_sha256": matrix_sha,
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
