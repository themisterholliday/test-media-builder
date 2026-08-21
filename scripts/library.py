"""Build a faceted, scraper-ready view of the generated library.

`generated/` is organised by build stage, which says how a file was made rather than
what it tests. This module reads generated/manifest.json and projects the same files
into `library/`, where each capability is a TV show and every variant carrying that
capability is an episode:

    library/Chapters/Season 01/Chapters - S01E03 - chapters_mkv_10.mkv

Nothing is re-encoded. Episodes are hardlinks by default: the same inode under a
second name, so the tree costs no extra disk while still being a *real* file at that
path — which matters when a media server reaches it through a container bind mount or
a network share, where a symlink to a host path would dangle.

Run this after every generate. ffmpeg writes a new inode on each rebuild, so a link
made against the previous build would keep serving the old content; each run compares
inodes and relinks anything stale.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .ffutil import require_binaries
from .generate import CONFIG_PATH, GENERATED_DIR, ROOT, _load_config
from .ffutil import FFExecError
from .nfo_writer import build_episode_nfo, build_tvshow_nfo, write_nfo
from .thumbnails import tile_images

log = logging.getLogger("library")

LIBRARY_DIR = ROOT / "library"
STATE_NAME = ".library_state.json"
EPISODE_MAP_NAME = ".episode_map.json"
SEASON_DIR = "Season 01"
SHOWS_ROOT = "shows"   # faceted TV-show tree, for a Plex "TV Shows" library
FLAT_ROOT = "flat"     # one file per video variant, for a Plex "Other Videos" library
AUDIO_ROOT = "audio"   # audio-only variants, for a Plex "Music" library
ALBUM_NAME = "Test Library"
LINK_MODES = ("hardlink", "symlink", "copy")

# Facet definitions. Each yields (show_id, show_title) pairs for one manifest entry.
# Kept declarative so adding a dimension is a one-line change.
CODEC_LABELS = {
    "h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9",
    "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4 Part 2", "wmv2": "WMV",
}
SUBTITLE_LABELS = {
    "subrip": "SRT Subtitles", "ass": "ASS Subtitles",
    "webvtt": "WebVTT Subtitles", "mov_text": "mov_text Subtitles",
}


def _facet_video_codec(entry: dict) -> list[tuple[str, str]]:
    codec = (entry.get("video") or {}).get("codec")
    if not codec:
        return []
    return [(f"codec-{codec}", f"{CODEC_LABELS.get(codec, codec.upper())} Video")]


def _facet_container(entry: dict) -> list[tuple[str, str]]:
    # Sidecar subtitles declare a `format`, not a container; without this guard they
    # would collect under a show literally named "None".
    container = entry.get("container")
    if not container:
        return []
    return [(f"container-{container}", f"{container.upper()} Container")]


def _facet_audio_codec(entry: dict) -> list[tuple[str, str]]:
    seen = {t.get("codec") for t in entry.get("audio_tracks") or [] if t.get("codec")}
    return [(f"audio-{c}", f"{c.upper()} Audio") for c in sorted(seen)]


def _facet_subtitles(entry: dict) -> list[tuple[str, str]]:
    seen = {t.get("codec") for t in entry.get("subtitle_tracks") or [] if t.get("codec")}
    return [(f"subs-{c}", SUBTITLE_LABELS.get(c, f"{c} Subtitles")) for c in sorted(seen)]


def _facet_chapters(entry: dict) -> list[tuple[str, str]]:
    return [("chapters", "Chapters")] if entry.get("chapters") else []


# Display aspect buckets, widest first so the first match wins.
ASPECT_BUCKETS = [
    (2.60, 99.0, "aspect-ultrawide", "Ultra-Wide"),
    (2.30, 2.60, "aspect-scope", "Scope 2.39"),
    (1.95, 2.30, "aspect-2to1", "Univisium 2.0"),
    (1.82, 1.95, "aspect-flat", "Flat 1.85"),
    (1.72, 1.82, "aspect-169", "Widescreen 16x9"),
    (1.45, 1.72, "aspect-166", "Wide SD 1.66"),
    (1.20, 1.45, "aspect-43", "Academy 4x3"),
    (0.95, 1.20, "aspect-square", "Square"),
    (0.00, 0.95, "aspect-vertical", "Vertical"),
]


def _facet_aspect(entry: dict) -> list[tuple[str, str]]:
    """Group by display aspect, plus a separate show for rotated content.

    Uses the coded shape times the pixel aspect, because a file's display shape is not
    its width over its height whenever the pixels are not square.
    """
    video = entry.get("video") or {}
    width, height = video.get("width"), video.get("height")
    if not width or not height:
        return []
    shows = []
    if "rotation" in (entry.get("flags") or []):
        shows.append(("aspect-rotated", "Rotated"))
    dar = video.get("display_aspect") or (float(width) / float(height))
    for low, high, show_id, title in ASPECT_BUCKETS:
        if low <= dar < high:
            shows.append((show_id, title))
            break
    return shows


FACETS = {
    "codec": _facet_video_codec,
    "container": _facet_container,
    "audio": _facet_audio_codec,
    "subtitles": _facet_subtitles,
    "chapters": _facet_chapters,
    "aspect": _facet_aspect,
}

FACET_PLOTS = {
    "codec": "Every variant whose video stream uses this codec.",
    "container": "Every variant delivered in this container format.",
    "audio": "Every variant carrying at least one audio track in this codec.",
    "subtitles": "Every variant carrying at least one subtitle track in this format.",
    "chapters": "Every variant carrying chapter markers, including malformed ones.",
    "aspect": "Every variant that displays at this aspect ratio, pixel aspect included.",
}


def probe_from_entry(entry: dict) -> dict:
    """Rebuild an ffprobe-shaped dict from a manifest entry.

    The manifest already holds every field the NFO stream details need, so the episode
    NFOs are built from it rather than re-probing 190-odd files on every run.
    """
    video = entry.get("video") or {}
    streams: list[dict] = []
    if video.get("codec"):
        streams.append({
            "codec_type": "video",
            "codec_name": video.get("codec"),
            "profile": video.get("profile"),
            "pix_fmt": video.get("pix_fmt"),
            "width": video.get("width"),
            "height": video.get("height"),
            "avg_frame_rate": str(video["fps"]) if video.get("fps") else None,
            "bits_per_raw_sample": video.get("bit_depth"),
            "color_primaries": video.get("color_primaries"),
            "color_transfer": video.get("color_transfer"),
            "color_space": video.get("color_space"),
        })
    for track in entry.get("audio_tracks") or []:
        streams.append({
            "codec_type": "audio",
            "codec_name": track.get("codec"),
            "channels": track.get("channels"),
            "sample_rate": track.get("sample_rate"),
            "bit_rate": track.get("bitrate_bps"),
            "tags": {k: v for k, v in
                     (("language", track.get("language")), ("title", track.get("title"))) if v},
            "disposition": {"default": int(bool(track.get("default"))),
                            "forced": int(bool(track.get("forced")))},
        })
    for track in entry.get("subtitle_tracks") or []:
        streams.append({
            "codec_type": "subtitle",
            "codec_name": track.get("codec"),
            "tags": {k: v for k, v in
                     (("language", track.get("language")), ("title", track.get("title"))) if v},
            "disposition": {"default": int(bool(track.get("default"))),
                            "forced": int(bool(track.get("forced")))},
        })

    chapters = [
        {
            "start_time": c.get("start_sec"),
            "end_time": c.get("end_sec"),
            "tags": {"title": c.get("title")} if c.get("title") else {},
        }
        for c in entry.get("chapters") or []
    ]

    return {
        "format": {
            "format_name": entry.get("format"),
            "duration": str(entry["duration_sec"]) if entry.get("duration_sec") is not None else None,
            "size": str(entry["size"]) if entry.get("size") is not None else None,
            "bit_rate": str(entry["bitrate_bps"]) if entry.get("bitrate_bps") is not None else None,
        },
        "streams": streams,
        "chapters": chapters,
    }


def _safe_name(value: str) -> str:
    """Make a string safe for a filename on any of the usual filesystems."""
    out = "".join("_" if c in '/\\:*?"<>|' else c for c in str(value))
    return out.strip(". ") or "untitled"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("could not read %s; starting fresh", path)
        return default


def collect_shows(entries: list[dict], facets: list[str]) -> dict[str, dict]:
    """Group manifest entries into shows, keyed by facet id.

    Only built entries with a real file on disk take part: pending externals have
    nothing to link, and stream variants are directories rather than playable files.
    """
    shows: dict[str, dict] = {}
    for entry in entries:
        if entry.get("status") != "built" or entry.get("section") == "streams":
            continue
        for facet in facets:
            for show_id, show_title in FACETS[facet](entry):
                show = shows.setdefault(
                    show_id, {"title": show_title, "facet": facet, "entries": []}
                )
                show["entries"].append(entry)
    for show in shows.values():
        show["entries"].sort(key=lambda e: e["name"])

    # Two shows sharing a directory would silently merge their episodes and
    # overwrite one another's tvshow.nfo.
    by_dir: dict[str, list[str]] = {}
    for show_id, show in shows.items():
        by_dir.setdefault(_safe_name(show["title"]), []).append(show_id)
    collisions = {d: ids for d, ids in by_dir.items() if len(ids) > 1}
    if collisions:
        raise ValueError(f"facet titles collide on the same directory: {collisions}")

    return shows


def assign_episodes(shows: dict[str, dict], episode_map: dict) -> dict:
    """Assign stable episode numbers, never renumbering an existing episode.

    Sorting alphabetically on every run would renumber everything after any newly
    added variant, so previous assignments are kept and new variants take the next
    free slot.
    """
    for show_id, show in sorted(shows.items()):
        assigned = episode_map.setdefault(show_id, {})
        used = set(assigned.values())
        next_free = 1
        for entry in show["entries"]:
            name = entry["name"]
            if name in assigned:
                continue
            while next_free in used:
                next_free += 1
            assigned[name] = next_free
            used.add(next_free)
    return episode_map


def _link_file(src: Path, dst: Path, mode: str) -> str:
    """Place `src` at `dst` using the requested mode. Returns the action taken."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        if _is_current(src, dst, mode):
            return "current"
        dst.unlink()
        action = "relinked"
    else:
        action = "linked"

    if mode == "hardlink":
        try:
            os.link(src, dst)
        except OSError as e:
            log.warning("hardlink failed for %s (%s); copying instead", dst.name, e)
            shutil.copy2(src, dst)
    elif mode == "symlink":
        os.symlink(os.path.relpath(src, dst.parent), dst)
    else:
        shutil.copy2(src, dst)
    return action


def _is_current(src: Path, dst: Path, mode: str) -> bool:
    """True when `dst` already reflects the current content of `src`.

    For hardlinks this compares inodes: ffmpeg writes a fresh inode on every rebuild,
    so an existing link with a different inode is silently serving stale content.
    """
    try:
        if mode == "hardlink":
            return src.stat().st_ino == dst.stat().st_ino
        if mode == "symlink":
            return dst.is_symlink() and dst.resolve() == src.resolve()
        src_stat, dst_stat = src.stat(), dst.stat()
        return src_stat.st_size == dst_stat.st_size and dst_stat.st_mtime >= src_stat.st_mtime
    except OSError:
        return False


def _episode_basename(show_title: str, episode: int, variant_name: str) -> str:
    return f"{_safe_name(show_title)} - S01E{episode:02d} - {_safe_name(variant_name)}"


def build_library(
    manifest: dict,
    *,
    facets: list[str],
    link_mode: str,
    min_episodes: int,
    library_dir: Path = LIBRARY_DIR,
    no_posters: bool = False,
) -> dict:
    """Project the manifest into the faceted show tree. Returns a summary dict."""
    entries = manifest.get("entries", [])
    entries_by_name = {e["name"]: e for e in entries
                       if e.get("status") == "built" and e.get("section") != "streams"}
    shows = collect_shows(entries, facets)
    shows = {k: v for k, v in shows.items() if len(v["entries"]) >= min_episodes}

    state_path = library_dir / STATE_NAME
    map_path = library_dir / EPISODE_MAP_NAME
    previous = set(_read_json(state_path, {}).get("paths", []))
    episode_map = assign_episodes(shows, _read_json(map_path, {}))

    created: set[str] = set()
    counts = {"linked": 0, "relinked": 0, "current": 0}

    for show_id, show in sorted(shows.items()):
        show_dir = library_dir / SHOWS_ROOT / _safe_name(show["title"])
        season_dir = show_dir / SEASON_DIR
        season_dir.mkdir(parents=True, exist_ok=True)

        show_thumbs: list[Path] = []
        tvshow_path = show_dir / "tvshow.nfo"
        write_nfo(tvshow_path, build_tvshow_nfo(
            show_id=show_id,
            title=show["title"],
            plot=FACET_PLOTS.get(show["facet"], "A facet of the media test library."),
            episode_count=len(show["entries"]),
            tags=[show["facet"], show_id],
        ))
        created.add(str(tvshow_path.relative_to(library_dir)))

        for entry in show["entries"]:
            episode = episode_map[show_id][entry["name"]]
            src = ROOT / entry["path"]
            if not src.exists():
                log.warning("skip %s: source missing (%s)", entry["name"], src)
                continue
            base = _episode_basename(show["title"], episode, entry["name"])
            dst = season_dir / f"{base}{src.suffix}"
            counts[_link_file(src, dst, link_mode)] += 1
            created.add(str(dst.relative_to(library_dir)))

            nfo_path = season_dir / f"{base}.nfo"
            write_nfo(nfo_path, build_episode_nfo(
                show_title=show["title"],
                season=1,
                episode=episode,
                variant_id=entry["name"],
                title=entry.get("title") or entry["name"],
                description=entry.get("description", ""),
                tags=[entry["name"], *(entry.get("flags") or [])],
                probe=probe_from_entry(entry),
                ffmpeg_cmd=None,
                generator="library",
                ffmpeg_version=manifest.get("_meta", {}).get("ffmpeg_version", "unknown"),
                source_refs=[],
                edge_flags=entry.get("flags", []),
                hdr=entry.get("hdr"),
                output_sha256=entry.get("sha256"),
                output_size=entry.get("size"),
                overlay=entry.get("overlay"),
            ))
            created.add(str(nfo_path.relative_to(library_dir)))

            thumb = GENERATED_DIR / "_thumbs" / f"{entry['name']}.jpg"
            if thumb.exists():
                thumb_dst = season_dir / f"{base}-thumb.jpg"
                _link_file(thumb, thumb_dst, link_mode)
                created.add(str(thumb_dst.relative_to(library_dir)))
                show_thumbs.append(thumb)

        if show_thumbs and not no_posters:
            poster = show_dir / "poster.jpg"
            try:
                tile_images(show_thumbs, poster, cols=min(4, len(show_thumbs)))
                created.add(str(poster.relative_to(library_dir)))
            except FFExecError as e:
                log.warning("poster failed for %s: %s", show["title"], e)

    build_flat_tree(entries_by_name, library_dir, link_mode, created, counts)
    build_audio_tree(entries_by_name, library_dir, link_mode, created, counts)

    removed = _prune(library_dir, previous - created)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"paths": sorted(created)}, indent=2), encoding="utf-8")
    map_path.write_text(json.dumps(episode_map, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "shows": len(shows),
        "episodes": counts["linked"] + counts["relinked"] + counts["current"],
        **counts,
        "removed": removed,
    }


def build_flat_tree(entries: dict, library_dir: Path, link_mode: str,
                    created: set[str], counts: dict) -> None:
    """Link every video variant once, under its own name, in a flat directory.

    The faceted tree is good for browsing but Plex's TV scanner rewrites show names
    it believes contain technical tokens, so "Widescreen 16x9" collapses into
    "Widescreen". A flat Other Videos library sidesteps the scanner's naming rules and
    gives exactly one item per variant, named as the matrix names it.
    """
    flat_dir = library_dir / FLAT_ROOT
    flat_dir.mkdir(parents=True, exist_ok=True)
    for name, entry in sorted(entries.items()):
        src = ROOT / entry["path"]
        if not src.exists():
            continue
        # Audio-only variants are included too: Plex Music refuses tracks in MKV or raw
        # EAC3, so a video-files library is the only route to the surround formats.
        dst = flat_dir / f"{_safe_name(name)}{src.suffix}"
        counts[_link_file(src, dst, link_mode)] += 1
        created.add(str(dst.relative_to(library_dir)))
        _link_thumb(name, dst.with_suffix(""), library_dir, link_mode, created)


def build_audio_tree(entries: dict, library_dir: Path, link_mode: str,
                     created: set[str], counts: dict) -> None:
    """Link audio-only variants as Artist/Album/Track for a Plex Music library.

    A TV library does not index files without a video stream, so roughly twenty
    variants -- DTS, TrueHD, ALAC, FLAC, PCM and friends -- are unreachable from apps
    unless they are presented as music.
    """
    for name, entry in sorted(entries.items()):
        if entry.get("video"):
            continue
        src = ROOT / entry["path"]
        if not src.exists():
            continue
        codec = ((entry.get("audio_tracks") or [{}])[0].get("codec") or "unknown").upper()
        album_dir = library_dir / AUDIO_ROOT / _safe_name(f"{codec} Audio") / _safe_name(ALBUM_NAME)
        album_dir.mkdir(parents=True, exist_ok=True)
        dst = album_dir / f"{_safe_name(name)}{src.suffix}"
        counts[_link_file(src, dst, link_mode)] += 1
        created.add(str(dst.relative_to(library_dir)))


def _link_thumb(variant: str, base: Path, library_dir: Path, link_mode: str,
                created: set[str]) -> None:
    """Link a variant's thumbnail beside a media file, if one was generated."""
    thumb = GENERATED_DIR / "_thumbs" / f"{variant}.jpg"
    if not thumb.exists():
        return
    dst = base.with_name(f"{base.name}-thumb.jpg")
    _link_file(thumb, dst, link_mode)
    created.add(str(dst.relative_to(library_dir)))


def _prune(library_dir: Path, stale: set[str]) -> int:
    """Remove only paths this tool created on a previous run.

    Anything not recorded in the state file is left alone — the library directory may
    hold files a person put there, and this must never delete them.
    """
    removed = 0
    for rel in sorted(stale):
        path = library_dir / rel
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
        except OSError as e:
            log.warning("could not remove stale %s: %s", path, e)
    for directory in sorted(library_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return removed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the faceted show library from manifest.json.")
    p.add_argument("--manifest", default=str(GENERATED_DIR / "manifest.json"))
    p.add_argument("--output", default=str(LIBRARY_DIR), help="Library root directory.")
    p.add_argument("--link-mode", choices=LINK_MODES, default="hardlink",
                   help="How episodes reference their source file (default hardlink).")
    p.add_argument("--facets", default=",".join(FACETS),
                   help=f"Comma-separated facets. Valid: {','.join(FACETS)}")
    p.add_argument("--min-episodes", type=int, default=1,
                   help="Skip shows with fewer than this many episodes.")
    p.add_argument("--no-posters", action="store_true",
                   help="Skip per-show poster generation (faster).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    require_binaries()
    _load_config(CONFIG_PATH)

    manifest_path = Path(ns.manifest)
    if not manifest_path.exists():
        log.error("manifest not found: %s — run 'make manifest' first", manifest_path)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    facets = [f.strip() for f in ns.facets.split(",") if f.strip()]
    unknown = [f for f in facets if f not in FACETS]
    if unknown:
        log.error("unknown facet(s): %s. Valid: %s", ", ".join(unknown), ", ".join(FACETS))
        return 2

    summary = build_library(
        manifest,
        facets=facets,
        link_mode=ns.link_mode,
        min_episodes=ns.min_episodes,
        library_dir=Path(ns.output),
        no_posters=ns.no_posters,
    )
    sys.stdout.write(
        f"library: {summary['shows']} shows, {summary['episodes']} episodes "
        f"({summary['linked']} new, {summary['relinked']} relinked, "
        f"{summary['current']} unchanged, {summary['removed']} pruned)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
