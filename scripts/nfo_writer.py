"""NFO XML writer: Kodi/Plex-compatible <movie> schema + <testlib> custom extension.

The top half of each NFO is standard Kodi/Jellyfin/Plex scraper-compatible
markup. Custom test-library metadata (edge-case flags, ffmpeg command, source
provenance, HDR details) lives in a <testlib> namespaced extension that
standard scrapers ignore.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable
from xml.dom import minidom
from xml.etree import ElementTree as ET

log = logging.getLogger("nfo")

TESTLIB_NS = "https://plexapp.com/testlib/v1"


def _sub(parent: ET.Element, tag: str, text: str | int | float | None = None) -> ET.Element:
    """Append a child element to parent and set text if not None."""
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _get_stream(probe: dict, codec_type: str) -> list[dict]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == codec_type]


def _pick_int(*candidates: Any) -> int | None:
    """Return first candidate that can be coerced to int, or None."""
    for c in candidates:
        if c is None:
            continue
        try:
            return int(c)
        except (TypeError, ValueError):
            continue
    return None


def _rate_to_float(rate: str | None) -> float | None:
    """Parse ffprobe rate strings like '24000/1001' into a float."""
    if not rate or rate == "0/0":
        return None
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            n, d = float(num), float(den)
            return n / d if d else None
        except ValueError:
            return None
    try:
        return float(rate)
    except ValueError:
        return None


def build_nfo(
    *,
    variant_id: str,
    title: str,
    description: str,
    tags: Iterable[str],
    probe: dict,
    ffmpeg_cmd: list[str] | None,
    ffmpeg_version: str,
    source_refs: list[dict],
    edge_flags: Iterable[str],
    hdr: dict | None,
    output_sha256: str | None,
    output_size: int | None,
) -> bytes:
    """Build a Kodi-compatible NFO document as pretty-printed UTF-8 XML bytes."""
    root = ET.Element("movie")

    _sub(root, "title", title)
    _sub(root, "plot", description)
    _sub(root, "outline", description)
    uid = _sub(root, "uniqueid", variant_id)
    uid.set("type", "testlib")
    uid.set("default", "true")

    for tag in tags:
        _sub(root, "tag", tag)

    fmt = probe.get("format", {}) or {}
    duration = fmt.get("duration")
    if duration is not None:
        try:
            _sub(root, "runtime", max(1, int(round(float(duration) / 60))))
        except ValueError:
            pass

    fileinfo = _sub(root, "fileinfo")
    streamdetails = _sub(fileinfo, "streamdetails")

    video_streams = _get_stream(probe, "video")
    audio_streams = _get_stream(probe, "audio")
    subtitle_streams = _get_stream(probe, "subtitle")

    for vs in video_streams:
        v = _sub(streamdetails, "video")
        _sub(v, "codec", vs.get("codec_name"))
        width = _pick_int(vs.get("width"))
        height = _pick_int(vs.get("height"))
        if width and height:
            _sub(v, "aspect", f"{width / height:.4f}")
            _sub(v, "width", width)
            _sub(v, "height", height)
        if duration is not None:
            try:
                _sub(v, "durationinseconds", int(round(float(duration))))
            except ValueError:
                pass
        fps = _rate_to_float(vs.get("avg_frame_rate")) or _rate_to_float(vs.get("r_frame_rate"))
        if fps:
            _sub(v, "framerate", f"{fps:.3f}")

    for as_ in audio_streams:
        a = _sub(streamdetails, "audio")
        _sub(a, "codec", as_.get("codec_name"))
        _sub(a, "language", (as_.get("tags") or {}).get("language", "und"))
        channels = _pick_int(as_.get("channels"))
        if channels is not None:
            _sub(a, "channels", channels)

    for ss in subtitle_streams:
        s = _sub(streamdetails, "subtitle")
        _sub(s, "language", (ss.get("tags") or {}).get("language", "und"))

    ET.register_namespace("testlib", TESTLIB_NS)
    ext = ET.SubElement(root, f"{{{TESTLIB_NS}}}testlib")

    _sub(ext, "id", variant_id)
    _sub(ext, "generator", "ffmpeg" if ffmpeg_cmd else "external")
    _sub(ext, "ffmpeg_version", ffmpeg_version)
    if ffmpeg_cmd is not None:
        _sub(ext, "command", " ".join(_shellquote(a) for a in ffmpeg_cmd))

    container = _sub(ext, "container")
    _sub(container, "format", fmt.get("format_name"))
    if fmt.get("duration") is not None:
        _sub(container, "duration", fmt.get("duration"))
    if fmt.get("size") is not None:
        _sub(container, "size", fmt.get("size"))
    if fmt.get("bit_rate") is not None:
        _sub(container, "bitrate", fmt.get("bit_rate"))

    if video_streams:
        vs = video_streams[0]
        v_detail = _sub(ext, "video_details")
        v_detail.set("codec", vs.get("codec_name", "") or "")
        v_detail.set("profile", str(vs.get("profile", "") or ""))
        v_detail.set("level", str(vs.get("level", "") or ""))
        v_detail.set("pix_fmt", str(vs.get("pix_fmt", "") or ""))
        v_detail.set("bit_depth", str(vs.get("bits_per_raw_sample", "") or ""))
        v_detail.set("color_primaries", str(vs.get("color_primaries", "") or ""))
        v_detail.set("color_transfer", str(vs.get("color_transfer", "") or ""))
        v_detail.set("color_space", str(vs.get("color_space", "") or ""))
        fps = _rate_to_float(vs.get("avg_frame_rate")) or _rate_to_float(vs.get("r_frame_rate"))
        if fps:
            v_detail.set("fps", f"{fps:.3f}")
        if vs.get("width") and vs.get("height"):
            v_detail.set("width", str(vs["width"]))
            v_detail.set("height", str(vs["height"]))

    audio_wrap = _sub(ext, "audio_tracks")
    for idx, as_ in enumerate(audio_streams):
        a = _sub(audio_wrap, "audio")
        a.set("index", str(idx))
        a.set("codec", as_.get("codec_name", "") or "")
        a.set("channels", str(as_.get("channels", "") or ""))
        a.set("sample_rate", str(as_.get("sample_rate", "") or ""))
        a.set("bit_rate", str(as_.get("bit_rate", "") or ""))
        tags_a = as_.get("tags") or {}
        a.set("language", tags_a.get("language", "und"))
        if tags_a.get("title"):
            a.set("title", tags_a["title"])
        disp = as_.get("disposition") or {}
        if disp.get("default"):
            a.set("default", "true")
        if disp.get("forced"):
            a.set("forced", "true")

    sub_wrap = _sub(ext, "subtitle_tracks")
    for idx, ss in enumerate(subtitle_streams):
        s = _sub(sub_wrap, "subtitle")
        s.set("index", str(idx))
        s.set("codec", ss.get("codec_name", "") or "")
        tags_s = ss.get("tags") or {}
        s.set("language", tags_s.get("language", "und"))
        if tags_s.get("title"):
            s.set("title", tags_s["title"])
        disp = ss.get("disposition") or {}
        if disp.get("default"):
            s.set("default", "true")
        if disp.get("forced"):
            s.set("forced", "true")

    if hdr:
        hdr_el = _sub(ext, "hdr")
        for k, v in hdr.items():
            if isinstance(v, (str, int, float)) and k != "x265_params":
                hdr_el.set(k, str(v))

    flags_el = _sub(ext, "edge_case_flags")
    for f in edge_flags:
        _sub(flags_el, "flag", f)

    sources_el = _sub(ext, "sources")
    for ref in source_refs:
        s = _sub(sources_el, "source")
        for k, v in ref.items():
            if v is not None:
                s.set(k, str(v))

    out_el = _sub(ext, "output")
    if output_sha256 is not None:
        out_el.set("sha256", output_sha256)
    if output_size is not None:
        out_el.set("size", str(output_size))

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")
    return pretty


def write_nfo(path: Path, payload: bytes) -> None:
    """Write an NFO payload to disk, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    log.info("wrote nfo: %s", path)


def _shellquote(arg: str) -> str:
    if not arg:
        return "''"
    if any(c.isspace() or c in "\"'$`\\*?[]{}()<>|&;#~" for c in arg):
        return "'" + arg.replace("'", "'\\''") + "'"
    return arg
