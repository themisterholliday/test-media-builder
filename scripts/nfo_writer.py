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

from .ffutil import bit_depth_for, rate_to_float
from .ids import mint, short

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


def add_streamdetails(root: ET.Element, probe: dict) -> None:
    """Append the Kodi <fileinfo><streamdetails> block.

    Shared by the <movie> and <episodedetails> schemas so both describe streams
    identically.
    """
    fmt = probe.get("format", {}) or {}
    duration = fmt.get("duration")
    fileinfo = _sub(root, "fileinfo")
    streamdetails = _sub(fileinfo, "streamdetails")

    for vs in _get_stream(probe, "video"):
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
        fps = rate_to_float(vs.get("avg_frame_rate")) or rate_to_float(vs.get("r_frame_rate"))
        if fps:
            _sub(v, "framerate", f"{fps:.3f}")

    for as_ in _get_stream(probe, "audio"):
        a = _sub(streamdetails, "audio")
        _sub(a, "codec", as_.get("codec_name"))
        _sub(a, "language", (as_.get("tags") or {}).get("language", "und"))
        channels = _pick_int(as_.get("channels"))
        if channels is not None:
            _sub(a, "channels", channels)

    for ss in _get_stream(probe, "subtitle"):
        sub = _sub(streamdetails, "subtitle")
        _sub(sub, "language", (ss.get("tags") or {}).get("language", "und"))


def build_nfo(
    *,
    variant_id: str,
    variant_uuid: str | None = None,
    attribution: str | None = None,
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
    overlay: dict | None = None,
    root_tag: str = "movie",
    head_extra: dict | None = None,
    generator: str | None = None,
) -> bytes:
    """Build a Kodi-compatible NFO as pretty-printed UTF-8 XML bytes.

    `root_tag` and `head_extra` let the episode schema reuse this whole body — the
    streamdetails block and the full <testlib> provenance extension are identical
    for a movie and an episode, so only the head differs.
    """
    root = ET.Element(root_tag)

    _sub(root, "title", title)
    for key, value in (head_extra or {}).items():
        _sub(root, key, value)
    _sub(root, "plot", description)
    _sub(root, "outline", description)
    # The UUID is the identifier external systems record, so it is the default one.
    # The matrix name rides along because it is what a person reads in a bug report.
    resolved_uuid = variant_uuid or mint(variant_id)
    uid = _sub(root, "uniqueid", resolved_uuid)
    uid.set("type", "testlib")
    uid.set("default", "true")
    name_uid = _sub(root, "uniqueid", variant_id)
    name_uid.set("type", "testlib-name")

    for tag in tags:
        _sub(root, "tag", tag)

    fmt = probe.get("format", {}) or {}
    duration = fmt.get("duration")
    if duration is not None:
        try:
            _sub(root, "runtime", max(1, int(round(float(duration) / 60))))
        except ValueError:
            pass

    video_streams = _get_stream(probe, "video")
    audio_streams = _get_stream(probe, "audio")
    subtitle_streams = _get_stream(probe, "subtitle")
    add_streamdetails(root, probe)

    ET.register_namespace("testlib", TESTLIB_NS)
    ext = ET.SubElement(root, f"{{{TESTLIB_NS}}}testlib")

    _sub(ext, "id", variant_id)
    _sub(ext, "uuid", resolved_uuid)
    _sub(ext, "uuid_short", short(resolved_uuid))
    if attribution:
        _sub(ext, "attribution", attribution)
    _sub(ext, "generator", generator or ("ffmpeg" if ffmpeg_cmd else "external"))
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
        v_detail.set("bit_depth", str(bit_depth_for(vs) or ""))
        v_detail.set("color_primaries", str(vs.get("color_primaries", "") or ""))
        v_detail.set("color_transfer", str(vs.get("color_transfer", "") or ""))
        v_detail.set("color_space", str(vs.get("color_space", "") or ""))
        fps = rate_to_float(vs.get("avg_frame_rate")) or rate_to_float(vs.get("r_frame_rate"))
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

    probe_chapters = probe.get("chapters", []) or []
    if probe_chapters:
        chapters_el = _sub(ext, "chapters")
        chapters_el.set("count", str(len(probe_chapters)))
        for idx, c in enumerate(probe_chapters):
            ch = _sub(chapters_el, "chapter")
            ch.set("index", str(idx))
            # A chapter legitimately starts at 0.0, which is falsy — test for None.
            start = c.get("start_time")
            end = c.get("end_time")
            ch.set("start", "" if start is None else str(start))
            ch.set("end", "" if end is None else str(end))
            ch.set("title", str((c.get("tags") or {}).get("title") or ""))

    if overlay:
        overlay_el = _sub(ext, "overlay")
        for k, v in overlay.items():
            if isinstance(v, (str, int, float, bool)):
                overlay_el.set(k, str(v))

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


def build_episode_nfo(*, show_title: str, season: int, episode: int, **kwargs) -> bytes:
    """Build a Kodi <episodedetails> NFO for one variant inside a facet show.

    Delegates to build_nfo so the stream details and the <testlib> provenance block
    are byte-identical to the movie schema.
    """
    return build_nfo(
        root_tag="episodedetails",
        head_extra={"showtitle": show_title, "season": season, "episode": episode},
        **kwargs,
    )


def build_tvshow_nfo(*, show_id: str, title: str, plot: str, episode_count: int,
                     tags: Iterable[str] = ()) -> bytes:
    """Build a Kodi <tvshow> NFO describing one facet of the library."""
    root = ET.Element("tvshow")
    _sub(root, "title", title)
    _sub(root, "showtitle", title)
    _sub(root, "plot", plot)
    _sub(root, "outline", plot)
    uid = _sub(root, "uniqueid", show_id)
    uid.set("type", "testlib")
    uid.set("default", "true")
    _sub(root, "season", 1)
    _sub(root, "episode", episode_count)
    for tag in tags:
        _sub(root, "tag", tag)

    ET.register_namespace("testlib", TESTLIB_NS)
    ext = ET.SubElement(root, f"{{{TESTLIB_NS}}}testlib")
    _sub(ext, "id", show_id)
    _sub(ext, "kind", "facet")
    _sub(ext, "episode_count", episode_count)

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
