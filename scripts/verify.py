"""Post-creation verifier for the media test library.

Runs after generate.py. For each declared variant:
  1. Existence + non-zero size check.
  2. Decode smoke test (ffmpeg -v error -i X -f null -) — must return 0 with no stderr.
  3. Structural contract re-check (same logic as build-time, re-run post-hoc).
  4. NFO parse + uniqueid-matches-filename check.
  5. Per-flag bitstream assertions (moof presence for fMP4, annex-b start codes,
     HDR metadata persistence, missing-metadata enforcement, track-count contracts).

Writes generated/verify_report.json and exits non-zero if any variant failed.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .ffutil import FFExecError, ffprobe, require_binaries
from .generate import (
    CONFIG_PATH,
    GENERATED_DIR,
    ROOT,
    SECTIONS,
    _all_plans,
    _filter_plans,
    _load_config,
    _probe_for,
    _probe_target,
    _verify_contract,
)

log = logging.getLogger("verify")

# Scaled at call time based on configured duration — see _decode_timeout_for().
_DECODE_TIMEOUT_FLOOR = 120


def _decode_timeout_for(duration_seconds: int) -> int:
    """Decode-only pass is usually realtime-or-faster, so 2x duration is plenty with a 120 s floor."""
    return max(_DECODE_TIMEOUT_FLOOR, int(duration_seconds) * 2)


@dataclass
class VariantReport:
    name: str
    section: str
    path: str
    status: str  # "ok" | "failed" | "skipped" | "pending_external"
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _decode_smoke(path: Path, timeout: int) -> tuple[bool, str]:
    """Decode-only pass. Returns (ok, stderr) — ok means rc==0 and empty stderr."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"decode smoke test timed out after {timeout}s"
    ok = proc.returncode == 0 and not proc.stderr.strip()
    return ok, proc.stderr.strip()


def _decode_smoke_dash(manifest: Path, timeout: int) -> tuple[bool, str]:
    """ffmpeg 8.1 has no DASH demuxer. Decode each init+chunk sequence instead."""
    out_dir = manifest.parent
    errs: list[str] = []
    for init in sorted(out_dir.glob("init-*.m4s")):
        rep = init.stem.split("-")[-1]  # e.g. "0"
        chunks = sorted(out_dir.glob(f"chunk-{rep}-*.m4s"))
        if not chunks:
            errs.append(f"no chunks for representation {rep}")
            continue
        inputs: list[str] = []
        cat_pipe = subprocess.Popen(
            ["cat", str(init), *[str(c) for c in chunks]],
            stdout=subprocess.PIPE,
        )
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
            stdin=cat_pipe.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if cat_pipe.stdout:
            cat_pipe.stdout.close()
        cat_pipe.wait(timeout=5)
        if proc.returncode != 0 or proc.stderr.strip():
            errs.append(f"rep {rep}: {proc.stderr.strip() or 'rc!=0'}")
    return (not errs), "; ".join(errs)


def _read_nfo(path: Path) -> tuple[ET.Element | None, str]:
    try:
        tree = ET.parse(path)
        return tree.getroot(), ""
    except (ET.ParseError, OSError) as e:
        return None, f"nfo parse failed: {e}"


def _bytes_head(path: Path, n: int = 8) -> bytes:
    with path.open("rb") as f:
        return f.read(n)


def _has_box(path: Path, fourcc: bytes, max_scan: int = 4 * 1024 * 1024) -> bool:
    """Best-effort scan for an ISOBMFF box fourcc within the first max_scan bytes."""
    with path.open("rb") as f:
        data = f.read(max_scan)
    return fourcc in data


def _is_annexb(path: Path) -> bool:
    head = _bytes_head(path, 64)
    return b"\x00\x00\x00\x01" in head[:32] or b"\x00\x00\x01" in head[:32]


def _bitstream_assertions(plan, probe: dict, report: VariantReport) -> None:
    """Run per-flag assertions that go beyond structural probe contracts."""
    v = plan.variant
    flags = set(v.get("flags", []))
    fmt = probe.get("format", {}) or {}
    tags_fmt = (fmt.get("tags") or {})

    if "fragmented_mp4" in flags:
        ok = _has_box(Path(plan.out_path), b"moof")
        report.checks["fragmented_mp4.has_moof"] = ok
        if not ok:
            report.errors.append("fragmented_mp4: no 'moof' box found in first 4MiB")

    if "annexb" in flags:
        ok = _is_annexb(Path(plan.out_path))
        report.checks["annexb.start_codes"] = ok
        if not ok:
            report.warnings.append("annexb: no start codes visible in first 32 bytes (may be inside TS packets)")

    if "missing_metadata" in flags:
        encoder = tags_fmt.get("encoder")
        ok = encoder is None or encoder == ""
        report.checks["missing_metadata.no_encoder_tag"] = ok
        if not ok:
            report.errors.append(f"missing_metadata: encoder tag still present: {encoder!r}")

    if "no_audio" in flags:
        streams = probe.get("streams", [])
        audio_count = sum(1 for s in streams if s.get("codec_type") == "audio")
        ok = audio_count == 0
        report.checks["no_audio.zero_audio_streams"] = ok
        if not ok:
            report.errors.append(f"no_audio: found {audio_count} audio streams")

    if "audio_only" in flags:
        streams = probe.get("streams", [])
        video_count = sum(1 for s in streams if s.get("codec_type") == "video")
        ok = video_count == 0
        report.checks["audio_only.zero_video_streams"] = ok
        if not ok:
            report.errors.append(f"audio_only: found {video_count} video streams")

    if "multi_audio" in flags:
        expected = len(v.get("audio_tracks", []))
        actual = sum(1 for s in probe.get("streams", []) if s.get("codec_type") == "audio")
        ok = expected == actual
        report.checks["multi_audio.track_count"] = ok
        if not ok:
            report.errors.append(f"multi_audio: expected {expected} audio streams, got {actual}")

    if "multi_subtitle" in flags:
        expected = len(v.get("subtitle_tracks", []))
        actual = sum(1 for s in probe.get("streams", []) if s.get("codec_type") == "subtitle")
        ok = expected == actual
        report.checks["multi_subtitle.track_count"] = ok
        if not ok:
            report.errors.append(f"multi_subtitle: expected {expected} subtitle streams, got {actual}")

    hdr = (v.get("video") or {}).get("hdr") if isinstance(v.get("video"), dict) else None
    if isinstance(hdr, dict):
        vs = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None) or {}
        for key in ("color_primaries", "color_trc", "colorspace"):
            expected = hdr.get(key)
            if expected:
                actual = vs.get("color_primaries" if key == "color_primaries" else ("color_transfer" if key == "color_trc" else "color_space"))
                ok = actual == expected
                report.checks[f"hdr.{key}"] = ok
                if not ok:
                    report.errors.append(f"hdr: expected {key}={expected}, got {actual}")

    if plan.kind == "subtitle_embedded":
        expected_lang = v.get("language")
        if expected_lang:
            subs = [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]
            languages = [(s.get("tags") or {}).get("language") for s in subs]
            ok = expected_lang in languages
            report.checks["subtitle_embedded.language"] = ok
            if not ok:
                report.errors.append(f"embedded subtitle missing language {expected_lang}: saw {languages}")


def _verify_nfo(plan, report: VariantReport) -> None:
    nfo = Path(plan.nfo_path)
    if not nfo.exists():
        report.errors.append(f"nfo missing: {nfo}")
        report.checks["nfo.exists"] = False
        return
    report.checks["nfo.exists"] = True
    root, err = _read_nfo(nfo)
    if root is None:
        report.errors.append(err)
        report.checks["nfo.parses"] = False
        return
    report.checks["nfo.parses"] = True
    uid = root.findtext("uniqueid")
    if uid != plan.variant["name"]:
        report.errors.append(f"nfo uniqueid mismatch: {uid!r} != {plan.variant['name']!r}")
        report.checks["nfo.uniqueid_match"] = False
    else:
        report.checks["nfo.uniqueid_match"] = True


def _verify_plan(plan, skip_decode: bool = False, decode_timeout: int = _DECODE_TIMEOUT_FLOOR) -> VariantReport:
    report = VariantReport(
        name=plan.variant["name"],
        section=plan.section,
        path=str(plan.out_path),
        status="ok",
    )

    target = _probe_target(plan)
    if plan.section == "external":
        src = Path(plan.variant["source_path"])
        if not src.is_absolute():
            src = ROOT / src
        if not src.exists() or not target.exists():
            report.status = "pending_external"
            report.warnings.append(f"external sample not present: {src}")
            return report

    if not target.exists() or target.stat().st_size == 0:
        report.status = "failed"
        report.errors.append(f"missing or empty: {target}")
        report.checks["exists"] = False
        return report
    report.checks["exists"] = True

    flags = set(plan.variant.get("flags", []))
    skip_for_kind = plan.kind == "subtitle_external"
    if not skip_decode and "truncated" not in flags and not skip_for_kind:
        if plan.kind == "stream" and plan.variant.get("type") == "dash":
            ok, err = _decode_smoke_dash(target, decode_timeout)
        else:
            ok, err = _decode_smoke(target, decode_timeout)
        report.checks["decode"] = ok
        if not ok:
            report.errors.append(f"decode: {err}")
    elif "truncated" in flags:
        report.checks["decode_skipped_truncated"] = True
    elif skip_for_kind:
        report.checks["decode_skipped_subtitle"] = True

    try:
        probe = _probe_for(plan)
    except FFExecError as e:
        report.errors.append(f"probe failed: {e}")
        report.checks["probe"] = False
        report.status = "failed"
        return report
    report.checks["probe"] = True

    contract_errs = _verify_contract(plan, probe)
    report.checks["contract"] = not contract_errs
    for ce in contract_errs:
        report.errors.append(f"contract: {ce}")

    _verify_nfo(plan, report)
    _bitstream_assertions(plan, probe, report)

    if report.errors:
        report.status = "failed"
    return report


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify generated media test library.")
    p.add_argument("--only", default="", help=f"Comma-separated sections. Valid: {','.join(SECTIONS)}")
    p.add_argument("--variant", default="", help="Comma-separated variant names.")
    p.add_argument("--no-decode", action="store_true", help="Skip decode smoke test (faster).")
    p.add_argument("--output", default=str(GENERATED_DIR / "verify_report.json"),
                   help="Path to write the JSON report.")
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
    plans = _all_plans(cfg)
    sections = {s.strip() for s in ns.only.split(",") if s.strip()} or None
    variant_names = {s.strip() for s in ns.variant.split(",") if s.strip()} or None
    plans = _filter_plans(plans, sections=sections, variant_names=variant_names)

    decode_timeout = _decode_timeout_for(cfg["defaults"].get("duration_seconds", 10))
    reports: list[VariantReport] = []
    for plan in plans:
        log.info("verify %s/%s", plan.section, plan.variant["name"])
        try:
            r = _verify_plan(plan, skip_decode=ns.no_decode, decode_timeout=decode_timeout)
        except Exception as e:  # noqa: BLE001 — capture anything, continue
            r = VariantReport(
                name=plan.variant["name"],
                section=plan.section,
                path=str(plan.out_path),
                status="failed",
                errors=[f"unexpected: {e!r}"],
            )
        reports.append(r)
        if r.status == "failed":
            log.error("FAIL %s: %s", r.name, "; ".join(r.errors))
        elif r.status == "pending_external":
            log.warning("pending external: %s", r.name)
        else:
            log.info("ok %s", r.name)

    summary: dict[str, int] = {}
    for r in reports:
        summary[r.status] = summary.get(r.status, 0) + 1

    out = {
        "summary": summary,
        "total": len(reports),
        "variants": [asdict(r) for r in reports],
    }
    out_path = Path(ns.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    sys.stdout.write("\n=== verify summary ===\n")
    for k in sorted(summary):
        sys.stdout.write(f"{k:>20}: {summary[k]}\n")
    sys.stdout.write(f"report: {out_path}\n")

    return 1 if any(r.status == "failed" for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
