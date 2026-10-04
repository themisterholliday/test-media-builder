"""Post-creation verifier for the media test library.

Runs after generate.py. For each declared variant:
  1. Existence + non-zero size check.
  2. Decode smoke test (ffmpeg -v error -i X -f null -) — must return 0 with no stderr.
  3. Structural contract re-check (same logic as build-time, re-run post-hoc).
  4. NFO parse + uniqueid check (UUID and matrix name).
  5. Per-flag bitstream assertions (moof presence for fMP4, annex-b start codes,
     HDR metadata persistence, missing-metadata enforcement, track-count contracts).

Writes generated/verify_report.json and exits non-zero if any variant failed.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from . import chapters as chapters_mod
from .encoders import effective_duration
from .ffutil import FFExecError, config_sha256, ffmpeg_bin, require_binaries
from .generate import (
    CONFIG_PATH,
    GENERATED_DIR,
    ROOT,
    SECTIONS,
    _all_plans,
    _filter_plans,
    _load_config,
    _overlay_record,
    _probe_for,
    _probe_target,
    _verify_contract,
)
from .ids import mint

log = logging.getLogger("verify")

# Populated from the matrix in main() before any plan is verified.
_OVERLAY_SETTINGS: dict = {}
_DEFAULTS: dict = {}

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
    cmd = [ffmpeg_bin(), "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"]
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
            [ffmpeg_bin(), "-hide_banner", "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
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


def _has_box(path: Path, fourcc: bytes, max_scan: int = 4 * 1024 * 1024) -> bool:
    """Best-effort scan for an ISOBMFF box fourcc within the first max_scan bytes."""
    with path.open("rb") as f:
        data = f.read(max_scan)
    return fourcc in data


def _is_annexb(path: Path) -> bool:
    """True when NAL units are start-code framed rather than length-prefixed.

    Two traps make the obvious checks wrong. Reading the head of the container fails on
    MPEG-TS, where the first bytes are packet framing (sync byte 0x47) and no start code
    is visible. Demuxing to raw h264 fails the other way: ffmpeg applies
    h264_mp4toannexb on the way out, so even an AVCC source comes back Annex B.

    What actually distinguishes them is how the container stores NALs — an `avcC` box
    means length-prefixed AVCC.
    """
    if _has_box(path, b"avcC"):
        return False
    with path.open("rb") as f:
        data = f.read(64 * 1024)
    return b"\x00\x00\x01" in data


def _bitstream_assertions(plan, probe: dict, report: VariantReport) -> None:
    """Run per-flag assertions that go beyond structural probe contracts."""
    v = plan.variant
    flags = set(v.get("flags", []))
    _chapter_assertions(plan, probe, report)
    _geometry_assertions(plan, probe, report)
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
            report.errors.append("annexb: NAL units are not start-code framed")

    if "avcc" in flags:
        ok = not _is_annexb(Path(plan.out_path))
        report.checks["avcc.length_prefixed"] = ok
        if not ok:
            report.errors.append("avcc: expected length-prefixed NALs, found Annex B start codes")

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


def _chapter_assertions(plan, probe: dict, report: VariantReport) -> None:
    """Check chapters against the spec that produced them.

    Well-formed variants are held to count, titles, ordering and bounds. The
    deliberately malformed ones are only recorded, not failed: ffmpeg is entitled to
    normalise or drop a nonsensical chapter, and what it actually did is the finding.
    """
    variant = plan.variant
    flags = set(variant.get("flags", []))
    spec = chapters_mod.spec_for(variant)
    if not spec:
        return

    actual = probe.get("chapters", []) or []
    report.checks["chapters.probed"] = True

    if "chapters_stripped" in flags:
        ok = len(actual) == 0
        report.checks["chapters.stripped"] = ok
        if not ok:
            report.errors.append(f"chapters_stripped: expected 0 chapters, found {len(actual)}")
        return

    duration = effective_duration(variant, _DEFAULTS)
    try:
        expected = chapters_mod.resolve(spec, duration)
    except chapters_mod.ChapterSpecError as e:
        report.errors.append(f"chapters: unresolvable spec: {e}")
        report.checks["chapters.spec_valid"] = False
        return
    report.checks["chapters.spec_valid"] = True

    malformed = {f for f in flags if f.startswith("chapters_")}
    count_ok = len(actual) == len(expected)
    if count_ok:
        report.checks["chapters.count"] = True
    elif malformed:
        # A muxer is entitled to drop or merge a nonsensical chapter, and what it chose
        # to do is the finding. Record that as its own true check rather than a false
        # one, so a false value anywhere in the report always means something is wrong.
        report.checks["chapters.muxer_normalised"] = True
        report.warnings.append(
            f"chapters: authored {len(expected)}, muxer kept {len(actual)} "
            f"(malformed case {sorted(malformed)})")
    else:
        report.checks["chapters.count"] = False
        report.errors.append(f"chapters: expected {len(expected)}, found {len(actual)}")

    actual_titles = [(c.get("tags") or {}).get("title", "") for c in actual]
    if count_ok:
        expected_titles = [c["title"] for c in expected]
        titles_ok = actual_titles == expected_titles
        report.checks["chapters.titles"] = titles_ok
        if not titles_ok:
            first = next((i for i, (a, b) in enumerate(zip(actual_titles, expected_titles)) if a != b), 0)
            message = (f"chapters: title {first} is {actual_titles[first]!r}, "
                       f"expected {expected_titles[first]!r}")
            (report.warnings if malformed else report.errors).append(message)

    starts = []
    for c in actual:
        try:
            starts.append(float(c.get("start_time", 0)))
        except (TypeError, ValueError):
            starts.append(0.0)
    monotonic = all(b >= a for a, b in zip(starts, starts[1:]))
    report.checks["chapters.monotonic"] = monotonic

    ends = []
    for c in actual:
        try:
            ends.append(float(c.get("end_time", 0)))
        except (TypeError, ValueError):
            ends.append(0.0)
    within = (max(ends) <= duration + 1.0) if ends else True
    report.checks["chapters.within_duration"] = within

    if not malformed:
        if not monotonic:
            report.errors.append("chapters: start times are not monotonic")
        if not within:
            report.errors.append(
                f"chapters: last chapter ends at {max(ends):.3f}s, past the {duration}s duration")


def _ratio(value: str | None) -> float | None:
    """Parse an ffprobe ratio like '16:9' into a float."""
    if not value or value in ("N/A", "0:1"):
        return None
    try:
        num, den = value.split(":")
        return float(num) / float(den) if float(den) else None
    except (ValueError, ZeroDivisionError):
        return None


def _geometry_assertions(plan, probe: dict, report: VariantReport) -> None:
    """Check declared geometry and colour against what the file actually carries.

    Aspect ratio is the dimension this library most recently found to be inert, so the
    declared shape is asserted rather than assumed: a variant that says 2.39:1 must
    probe as 2.39:1.
    """
    variant = plan.variant
    video = variant.get("video") or {}
    stream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream:
        return

    declared_sar = video.get("sar")
    if declared_sar:
        want = _ratio(str(declared_sar).replace("/", ":"))
        got = _ratio(stream.get("sample_aspect_ratio"))
        ok = want is not None and got is not None and abs(want - got) < 0.01
        report.checks["geometry.sar"] = ok
        if not ok:
            report.errors.append(
                f"sar mismatch: declared {declared_sar}, probed {stream.get('sample_aspect_ratio')}")
    elif variant.get("width") and variant.get("height") and not video.get("pad_to") and not video.get("aspect"):
        # Square pixels are the default, so the coded shape must be the display shape.
        want = float(variant["width"]) / float(variant["height"])
        got = _ratio(stream.get("display_aspect_ratio"))
        ok = got is not None and abs(want - got) < 0.02
        report.checks["geometry.dar"] = ok
        if not ok:
            report.errors.append(
                f"display aspect mismatch: {variant['width']}x{variant['height']} implies "
                f"{want:.3f}, probed {stream.get('display_aspect_ratio')} ({got})")

    rotate = video.get("rotate")
    if rotate is not None:
        side = stream.get("side_data_list") or []
        found = next((d.get("rotation") for d in side if "rotation" in d), None)
        # ffprobe normalises into -180..180, so 270 reports as -90 and 180 as -180.
        expected = ((int(rotate) + 180) % 360) - 180
        ok = found is not None and int(found) == expected
        report.checks["geometry.rotation"] = ok
        if not ok:
            report.errors.append(
                f"rotation metadata: expected {expected} (declared {rotate}), probed {found!r}")

    for key, probe_key in (("color_range", "color_range"), ("colorspace", "color_space")):
        declared = video.get(key)
        if declared and not video.get("hdr"):
            got = stream.get(probe_key)
            ok = got == declared
            report.checks[f"colour.{key}"] = ok
            if not ok:
                report.errors.append(f"{key} mismatch: declared {declared}, probed {got!r}")


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
    # Identity is carried by two ids: the default one is the UUID, and a second
    # `testlib-name` element holds the matrix name. Both are checked, because a
    # mismatch in either means the sidecar belongs to a different variant.
    ids = {node.get("type"): (node.text or "") for node in root.findall("uniqueid")}
    want_uuid = plan.variant.get("uuid") or mint(plan.variant["name"])
    want_name = plan.variant["name"]
    got_uuid = ids.get("testlib", "")
    got_name = ids.get("testlib-name", "")

    # An NFO written before identifiers existed carries the name in the default slot.
    if got_uuid == want_name and not got_name:
        report.errors.append(
            f"nfo predates asset identity: uniqueid is {got_uuid!r}, expected {want_uuid!r} "
            f"-- regenerate with `python3 -m scripts.generate --refresh-nfo`")
        report.checks["nfo.uniqueid_match"] = False
    elif got_uuid != want_uuid or got_name != want_name:
        report.errors.append(
            f"nfo uniqueid mismatch: uuid {got_uuid!r} != {want_uuid!r} "
            f"or name {got_name!r} != {want_name!r}")
        report.checks["nfo.uniqueid_match"] = False
    else:
        report.checks["nfo.uniqueid_match"] = True

    _verify_overlay_record(plan, root, report)


def _verify_overlay_record(plan, root, report: VariantReport) -> None:
    """Check the NFO's recorded overlay state matches what the matrix declared.

    This asserts metadata only. Whether the text actually rendered into the pixels is
    confirmed visually through the thumbnails and contact sheet.
    """
    expected = _overlay_record(plan, _OVERLAY_SETTINGS)
    element = root.find(".//{https://plexapp.com/testlib/v1}overlay")
    if element is None:
        element = root.find(".//overlay")
    if element is None:
        # An NFO written before the overlay feature existed. Report it rather than fail.
        report.warnings.append("nfo has no <overlay> record — regenerate to add it")
        report.checks["overlay.recorded"] = False
        return
    report.checks["overlay.recorded"] = True
    actual_enabled = element.get("enabled")
    if actual_enabled != expected.get("enabled"):
        report.errors.append(
            f"overlay state mismatch: nfo says enabled={actual_enabled!r}, "
            f"matrix implies {expected.get('enabled')!r}"
        )
        report.checks["overlay.matches_config"] = False
    else:
        report.checks["overlay.matches_config"] = True


def _verify_audio_channels(plan, probe: dict, report: VariantReport) -> None:
    """Assert the audio track carries the channel count the matrix declares.

    Several encoders accept a channel count they cannot deliver and exit 0 anyway:
    E-AC-3 and AC-3 downmix 7.1 to 5.1(side), ALAC yields 7 channels in a 6.1(back)
    layout, and TrueHD writes a track reporting 0 channels. Nothing else in the
    pipeline notices, so a variant would claim 8 channels while shipping 6 -- exactly
    the quiet mismatch this library exists to catch.

    Multi-track edge cases declare `audio_tracks` rather than `audio` and are checked
    for stream count elsewhere, so they are skipped here.
    """
    declared = (plan.variant.get("audio") or {}).get("channels")
    if not declared:
        return
    audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        return
    actual = audio_streams[0].get("channels")
    if actual == declared:
        report.checks["audio.channels_match"] = True
        return
    report.checks["audio.channels_match"] = False
    layout = audio_streams[0].get("channel_layout") or "unknown"
    report.errors.append(
        f"audio channels: declared {declared}, file carries {actual} ({layout}) -- "
        f"the encoder accepted the request and delivered something else")


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

    _verify_audio_channels(plan, probe, report)
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
    global _OVERLAY_SETTINGS
    _OVERLAY_SETTINGS = (cfg.get("defaults") or {}).get("overlay") or {}
    global _DEFAULTS
    _DEFAULTS = cfg["defaults"]
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
        # Records what produced this run. A report read without it cannot be told
        # apart from a stale one, and a stale report that says "ok" is worse than
        # no report at all.
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ffmpeg": ffmpeg_bin(),
            "matrix_sha256": config_sha256(cfg),
        },
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
