"""Write generated/manifest.json — a flat, machine-readable index of the library.

One entry per built variant with normalized fields for downstream test automation:
name, section, path, size, sha256, container, flags, codec, profile, pix_fmt,
width, height, fps, bit_depth, hdr, audio_tracks, subtitle_tracks. Missing
externals are included with status="pending_external".
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .bootstrap import image_set_fingerprint, list_source_images
from .ffutil import bit_depth_for, config_sha256, ffmpeg_bin, ffmpeg_version, rate_to_float, require_binaries, sha256_file
from .ids import mint, short
from .generate import (
    CONFIG_PATH,
    GENERATED_DIR,
    ROOT,
    SECTIONS,
    SOURCES_DIR,
    _all_plans,
    _filter_plans,
    _load_config,
    _overlay_record,
    _probe_for,
    _probe_target,
)

log = logging.getLogger("manifest")

# Populated from the matrix in main() before any entry is built.
_OVERLAY_SETTINGS: dict = {}


def _dar_float(stream: dict) -> float | None:
    """Return a video stream's display aspect as a float, honouring pixel aspect."""
    dar = stream.get("display_aspect_ratio")
    if dar and dar not in ("N/A", "0:1") and ":" in dar:
        try:
            num, den = dar.split(":")
            return round(float(num) / float(den), 4) if float(den) else None
        except (ValueError, ZeroDivisionError):
            pass
    width, height = stream.get("width"), stream.get("height")
    if width and height:
        return round(float(width) / float(height), 4)
    return None


def _entry_for(plan, probe: dict | None) -> dict:
    v = plan.variant
    target = _probe_target(plan)
    exists = target.exists() and (target.is_file() and target.stat().st_size > 0 or target.is_dir())

    variant_uuid = v.get("uuid") or mint(v["name"])
    out: dict = {
        "name": v["name"],
        "uuid": variant_uuid,
        "uuid_short": short(variant_uuid),
        "section": plan.section,
        "title": v.get("title", v["name"]),
        "description": v.get("description", ""),
        "path": str(target.relative_to(ROOT)) if exists and target.exists() else str(target.relative_to(ROOT) if target else ""),
        "container": v.get("container"),
        "flags": v.get("flags", []),
        "status": "built" if exists else "missing",
        "overlay": _overlay_record(plan, _OVERLAY_SETTINGS),
    }
    # Licence attribution must travel with the work, so it is copied wherever the
    # asset is described rather than living only in the matrix.
    if v.get("attribution"):
        out["attribution"] = v["attribution"]
    if plan.section == "external" and not exists:
        out["status"] = "pending_external"
        out["source_path"] = v.get("source_path")
        out["license_note"] = v.get("license_note")
        return out

    if exists and target.is_file():
        out["size"] = target.stat().st_size
        out["sha256"] = sha256_file(target)

    if probe:
        fmt = probe.get("format", {}) or {}
        out["format"] = fmt.get("format_name")
        if fmt.get("duration"):
            try:
                out["duration_sec"] = float(fmt["duration"])
            except ValueError:
                pass
        if fmt.get("bit_rate"):
            try:
                out["bitrate_bps"] = int(fmt["bit_rate"])
            except ValueError:
                pass

        video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
        audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
        sub_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]

        if video_streams:
            vs = video_streams[0]
            fps = rate_to_float(vs.get("avg_frame_rate")) or rate_to_float(vs.get("r_frame_rate"))
            out["video"] = {
                "codec": vs.get("codec_name"),
                "profile": vs.get("profile"),
                "pix_fmt": vs.get("pix_fmt"),
                "width": vs.get("width"),
                "height": vs.get("height"),
                "fps": round(fps, 3) if fps else None,
                "bit_depth": bit_depth_for(vs),
                "sample_aspect_ratio": vs.get("sample_aspect_ratio"),
                "display_aspect_ratio": vs.get("display_aspect_ratio"),
                "display_aspect": _dar_float(vs),
                "color_primaries": vs.get("color_primaries") or None,
                "color_transfer": vs.get("color_transfer") or None,
                "color_space": vs.get("color_space") or None,
            }
        out["audio_tracks"] = [
            {
                "codec": s.get("codec_name"),
                "channels": s.get("channels"),
                "sample_rate": int(s["sample_rate"]) if s.get("sample_rate") else None,
                "bitrate_bps": int(s["bit_rate"]) if s.get("bit_rate") else None,
                "language": (s.get("tags") or {}).get("language"),
                "title": (s.get("tags") or {}).get("title"),
                "default": bool((s.get("disposition") or {}).get("default")),
                "forced": bool((s.get("disposition") or {}).get("forced")),
            }
            for s in audio_streams
        ]
        out["subtitle_tracks"] = [
            {
                "codec": s.get("codec_name"),
                "language": (s.get("tags") or {}).get("language"),
                "title": (s.get("tags") or {}).get("title"),
                "default": bool((s.get("disposition") or {}).get("default")),
                "forced": bool((s.get("disposition") or {}).get("forced")),
            }
            for s in sub_streams
        ]

    hdr = (v.get("video") or {}).get("hdr") if isinstance(v.get("video"), dict) else None
    if isinstance(hdr, dict):
        out["hdr"] = {k: val for k, val in hdr.items() if k != "x265_params"}

    # Read from the probe rather than the spec, so the manifest reports what the file
    # actually carries after muxing rather than what was requested.
    out["chapters"] = [
        {
            "index": i,
            "start_sec": float(c["start_time"]) if c.get("start_time") is not None else None,
            "end_sec": float(c["end_time"]) if c.get("end_time") is not None else None,
            "title": (c.get("tags") or {}).get("title"),
        }
        for i, c in enumerate((probe or {}).get("chapters", []) or [])
    ]

    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emit manifest.json index of the library.")
    p.add_argument("--only", default="", help=f"Comma-separated sections. Valid: {','.join(SECTIONS)}")
    p.add_argument("--variant", default="")
    p.add_argument("--output", default=str(GENERATED_DIR / "manifest.json"))
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    require_binaries()
    cfg = _load_config(CONFIG_PATH)
    global _OVERLAY_SETTINGS
    _OVERLAY_SETTINGS = (cfg.get("defaults") or {}).get("overlay") or {}
    plans = _all_plans(cfg)
    sections = {s.strip() for s in ns.only.split(",") if s.strip()} or None
    variant_names = {s.strip() for s in ns.variant.split(",") if s.strip()} or None
    plans = _filter_plans(plans, sections=sections, variant_names=variant_names)

    entries: list[dict] = []
    for plan in plans:
        target = _probe_target(plan)
        probe = None
        if target.exists():
            try:
                probe = _probe_for(plan)
            except Exception as e:  # noqa: BLE001 — best effort; manifest still gets written
                log.warning("probe failed for %s: %s", plan.variant["name"], e)
        entries.append(_entry_for(plan, probe))

    out = {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ffmpeg_version": ffmpeg_version(),
            "matrix_sha256": config_sha256(cfg),
            # The matrix hash covers config only. Source images live outside it, so
            # fingerprint them separately or two visually different libraries would
            # claim identical provenance.
            "source_images_sha256": image_set_fingerprint(list_source_images(SOURCES_DIR)),
            "entry_count": len(entries),
        },
        "entries": entries,
    }

    out_path = Path(ns.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    sys.stdout.write(f"wrote {out_path} ({len(entries)} entries)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
