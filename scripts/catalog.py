"""Generate `docs/CATALOG.md` — the single catalogue of every declared variant.

This replaces the hand-written matrix document and the separate identifier register.
Both described the same rows, and the hand-written one drifted every time the matrix
changed: sections that did not exist, counts that disagreed with their own tables,
variants missing entirely.

Everything here is derived from `config/media_matrix.json`, with build status from
`generated/manifest.json` when one is present. A section heading cannot claim a count
its table does not have, because both come from the same list.

    python3 -m scripts.catalog            # write docs/CATALOG.md
    python3 -m scripts.catalog --check    # fail if it is out of date
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .generate import CONFIG_PATH, GENERATED_DIR, ROOT
from .ids import check_uuids, iter_variants, short

log = logging.getLogger("catalog")

CATALOG_PATH = ROOT / "docs" / "CATALOG.md"

SECTION_TITLES = {
    "video_variants": ("Video", "Standard video + audio files. Codec profiles, containers, bit "
                                "depths, HDR modes, frame rates, aspect ratios, interlacing and "
                                "chapter structures."),
    "master_variants": ("Masters", "Real content, ingested rather than encoded — transcodes of the "
                                   "Netflix Open Content *Sol Levante* film (CC BY 4.0)."),
    "audio_only_variants": ("Audio-only", "No video stream. Channel layouts, bit depths and sample "
                                          "rates for the music and podcast ingest paths."),
    "subtitle_variants": ("Subtitles", "External sidecars and embedded subtitle tracks."),
    "stream_variants": ("Streams", "Adaptive streaming outputs. These are directories, not files."),
    "edge_cases": ("Edge cases", "Deliberately pathological files that stress specific pipeline "
                                 "behaviour."),
    "external_variants": ("External", "Media ffmpeg cannot author, ingested whole. Three come from "
                                      "the Sol Levante masters; the rest are acquired by "
                                      "`make fetch` — see [Source assets](#source-assets)."),
}

CODEC_LABEL = {
    "libx264": "H.264", "libx265": "HEVC", "libvpx-vp9": "VP9", "libaom-av1": "AV1",
    "libsvtav1": "AV1", "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4 ASP", "libxvid": "Xvid",
    "h263": "H.263", "dnxhd": "DNxHD", "prores_ks": "ProRes", "wmv2": "WMV2",
    "flv": "Sorenson", "libtheora": "Theora",
}
AUDIO_LABEL = {
    "aac": "AAC", "ac3": "AC-3", "eac3": "E-AC-3", "libopus": "Opus", "libmp3lame": "MP3",
    "mp2": "MP2", "flac": "FLAC", "alac": "ALAC", "libvorbis": "Vorbis", "dca": "DTS",
    "truehd": "TrueHD", "pcm_s16le": "PCM s16", "pcm_s24le": "PCM s24", "wmav2": "WMA2",
}
LAYOUT = {1: "mono", 2: "stereo", 3: "2.1", 6: "5.1", 8: "7.1", 12: "7.1.4", 14: "9.1.4"}


def _video_label(variant: dict) -> str:
    """Render a variant's video codec and profile for a table cell."""
    video = variant.get("video") or {}
    if not video:
        return "—"
    base = CODEC_LABEL.get(video.get("codec"), video.get("codec") or "—")
    profile = video.get("profile")
    return f"{base} {profile}" if profile else base


def _audio_label(variant: dict) -> str:
    """Render a variant's audio codec and channel layout for a table cell."""
    audio = variant.get("audio") or {}
    if not audio:
        tracks = variant.get("audio_tracks")
        return f"{len(tracks)} tracks" if tracks else "none"
    codec = AUDIO_LABEL.get(audio.get("codec"), audio.get("codec") or "—")
    channels = audio.get("channels")
    return f"{codec} {LAYOUT.get(channels, f'{channels}ch' if channels else '')}".strip()


def _geometry(variant: dict, defaults: dict) -> str:
    """Render a variant's output geometry, marking the default when unset."""
    if not (variant.get("video") or variant.get("width")):
        return "—"
    width = variant.get("width", defaults.get("width"))
    height = variant.get("height", defaults.get("height"))
    fps = variant.get("framerate", defaults.get("framerate"))
    return f"{width}×{height} @ {fps}"


def _section_table(variants: list, built: dict, defaults: dict) -> list[str]:
    """Render one section as a Markdown table."""
    lines = ["| ID | Name | What it is | Container | Video | Audio | Status |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for v in variants:
        entry = built.get(v["name"], {})
        status = entry.get("status", "not built")
        lines.append(
            f"| `{short(v['uuid'])}` | `{v['name']}` | {v.get('title') or v['name']} "
            f"| {v.get('container') or '—'} | {_video_label(v)} | {_audio_label(v)} | {status} |")
    return lines


def _coverage(cfg: dict) -> tuple[list[str], Counter]:
    """Compute what the matrix collectively exercises, so the table cannot go stale."""
    containers, vcodecs, acodecs, layouts, geometries, flags = (
        Counter(), Counter(), Counter(), Counter(), Counter(), Counter())
    defaults = cfg.get("defaults", {})
    for _section, v in iter_variants(cfg):
        if v.get("container"):
            containers[v["container"]] += 1
        video = v.get("video") or {}
        if video.get("codec"):
            vcodecs[CODEC_LABEL.get(video["codec"], video["codec"])] += 1
        for audio in ([v["audio"]] if v.get("audio") else []) + (v.get("audio_tracks") or []):
            if audio.get("codec"):
                acodecs[AUDIO_LABEL.get(audio["codec"], audio["codec"])] += 1
            channels = audio.get("channels")
            if channels:
                layouts[LAYOUT.get(channels, f"{channels}ch")] += 1
        if video:
            geometries[_geometry(v, defaults)] += 1
        for flag in v.get("flags") or []:
            flags[flag] += 1

    def row(name: str, counter: Counter) -> str:
        items = ", ".join(f"{k} ({n})" for k, n in sorted(counter.items(), key=lambda x: -x[1]))
        return f"| {name} | {items} |"

    return [
        "| Dimension | Values exercised (variant count) |",
        "| --- | --- |",
        row("Containers", containers),
        row("Video codecs", vcodecs),
        row("Audio codecs", acodecs),
        row("Channel layouts", layouts),
        row("Geometry", geometries),
    ], flags


def _flag_index(cfg: dict) -> list[str]:
    """List which variants carry each flag, the cross-cutting view of the matrix."""
    by_flag = defaultdict(list)
    for _section, v in iter_variants(cfg):
        for flag in v.get("flags") or []:
            by_flag[flag].append(v["name"])
    lines = ["| Flag | Count | Variants |", "| --- | --- | --- |"]
    for flag in sorted(by_flag):
        names = by_flag[flag]
        shown = ", ".join(f"`{n}`" for n in names[:6])
        if len(names) > 6:
            shown += f", +{len(names) - 6} more"
        lines.append(f"| `{flag}` | {len(names)} | {shown} |")
    return lines


def build(cfg: dict, manifest: dict | None) -> str:
    """Render the whole catalogue as Markdown."""
    defaults = cfg.get("defaults", {})
    built = {e["name"]: e for e in (manifest or {}).get("entries", []) or []}

    grouped: dict[str, list] = defaultdict(list)
    for section, variant in iter_variants(cfg):
        grouped[section].append(variant)

    untitled = sorted(set(grouped) - set(SECTION_TITLES))
    if untitled:
        raise ValueError(f"no SECTION_TITLES entry for {', '.join(untitled)} -- "
                         f"add one or those variants are counted but never listed")

    total = sum(len(v) for v in grouped.values())
    status_counts = Counter(e.get("status") for e in built.values()) if built else Counter()

    out = [
        "# Catalogue",
        "",
        "Every declared variant, its identifier and its build status.",
        "",
        "**Generated by `python3 -m scripts.catalog`. Do not edit by hand.**",
        "",
        f"**{total} variants** across {len(grouped)} sections"
        + (f" — {status_counts.get('built', 0)} built, "
           f"{status_counts.get('pending_external', 0)} awaiting a user-supplied file."
           if status_counts else "."),
        "",
        "Section names match the keys in `config/media_matrix.json`. Chapters, HDR and the",
        "other cross-cutting properties are **flags**, not sections — see"
        " [Flag index](#flag-index).",
        "",
        "## Identity",
        "",
        "Each variant owns a UUIDv5, minted once from its name and then frozen in the matrix,",
        "so renaming a variant does not change it. The **ID** column is the first 8 hex digits,",
        "which is what appears in library filenames and is the join key back to the manifest:",
        "",
        "```",
        "config/media_matrix.json   \"uuid\": \"57b6f4a7-fef6-5f37-a86b-a0b4847fb989\"",
        "library/flat/              aspect_scope_239 [57b6f4a7].mp4",
        "```",
        "",
        "Full UUIDs are in [the index at the end](#full-uuid-index).",
        "",
        "---",
        "",
    ]

    for section, (title, blurb) in SECTION_TITLES.items():
        variants = grouped.get(section) or []
        if not variants:
            continue
        out += [f"## {title} ({len(variants)})", "", blurb, "",
                f"Matrix key: `{section}`", ""]
        out += _section_table(variants, built, defaults)
        out += ["", "---", ""]

    out += _source_assets(cfg)

    coverage, _flag_counts = _coverage(cfg)
    out += ["## Coverage", "",
            "What the matrix collectively exercises. Computed from the matrix, so it cannot",
            "disagree with the tables above.", ""]
    out += coverage
    out += ["", "---", "", "## Flag index", "",
            "Flags are how cross-cutting properties are expressed. A variant carrying",
            "`chapters` may live in any section.", ""]
    out += _flag_index(cfg)
    out += ["", "---", "", "## Full UUID index", "",
            "| ID | UUID | Name |", "| --- | --- | --- |"]
    for _section, v in iter_variants(cfg):
        out.append(f"| `{short(v['uuid'])}` | `{v['uuid']}` | `{v['name']}` |")

    out += ["", "---", "",
            f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} from "
            f"`config/media_matrix.json`._", ""]
    return "\n".join(out)


def _source_assets(cfg: dict) -> list[str]:
    """Render the source_downloads table: every byte this build did not author."""
    entries = cfg.get("source_downloads") or []
    if not entries:
        return []

    public = [e for e in entries if e.get("url")]
    local = [e for e in entries if not e.get("url")]

    out = [
        "## Source assets", "",
        "What this generator cannot author, and where each piece comes from. "
        f"{len(public)} of {len(entries)} are public downloads; the rest need a local",
        "media library. Acquire them with:", "",
        "```bash",
        "make fetch                                      # the public ones",
        "make fetch-local MEDIA_ROOT=/path/to/library    # the rest",
        "make fetch-check                                # what is still missing",
        "```", "",
        "Each asset is checksum-pinned, so an upstream file that changes under us is a "
        "hard failure rather than a silent change to the corpus.", "",
        "| Asset | Source | Feeds | Path |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        source = f"[download]({entry['url']})" if entry.get("url") else "local library"
        feeds = ", ".join(f"`{n}`" for n in entry.get("used_by", [])) or "—"
        out.append(f"| `{entry['name']}` | {source} | {feeds} | `{entry['path']}` |")

    if local:
        out += ["", "The assets marked *local library* have no canonical public URL. Each "
                    "declares a `local_hint` — the path it sits at inside a media library — "
                    "which is what `make fetch-local` resolves.", ""]
    out += ["", "---", ""]
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate docs/CATALOG.md from the matrix.")
    p.add_argument("--output", default=str(CATALOG_PATH))
    p.add_argument("--check", action="store_true",
                   help="Verify the catalogue is current; write nothing.")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    cfg = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    problems = check_uuids(cfg)
    if problems:
        for x in problems:
            log.error("%s", x)
        return 2

    manifest_path = GENERATED_DIR / "manifest.json"
    manifest = None
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        log.warning("no manifest; status column will read 'not built'")

    body = build(cfg, manifest)
    out = Path(a.output)

    if a.check:
        if not out.is_file():
            log.error("%s does not exist -- run `python3 -m scripts.catalog`", out)
            return 1
        # The trailing date changes daily and is not a content difference.
        if out.read_text(encoding="utf-8").rsplit("_Generated", 1)[0] != body.rsplit("_Generated", 1)[0]:
            log.error("%s is stale -- run `python3 -m scripts.catalog`", out)
            return 1
        log.info("catalogue is current (%d variants)", sum(1 for _ in iter_variants(cfg)))
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    log.info("wrote %s (%d variants)", out, sum(1 for _ in iter_variants(cfg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
