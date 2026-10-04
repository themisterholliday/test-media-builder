"""Validate that a media server scanned the faceted library correctly.

generated/manifest.json is ground truth: it records what every file actually is,
measured by ffprobe at build time. A media server's API reports what the server
*thinks* each file is. This module diffs the two.

Disagreements fall into three buckets, and the matrix is what makes the third one
possible to express:

  server_gap   the server could not parse or would not accept the file
  metadata     the server accepted it but reports different technical detail
  expected     a variant that is deliberately broken, so a failure is the point

Run the servers with docker/compose.yml first. Neither server is configured here
beyond what a scan needs, and both mount the library read-only.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from .generate import GENERATED_DIR, ROOT
from .ids import SHORT_RE, short, short_from_path
from .plexsafety import plex_token

log = logging.getLogger("servercheck")

DEFAULT_PLEX = "http://localhost:32402"
DEFAULT_JELLYFIN = "http://localhost:8096"
LIBRARY_DIR = ROOT / "library"


@dataclass
class ServerItem:
    """One item as a media server reports it."""
    variant: str
    title: str
    path: str
    container: str | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    aspect: float | None = None
    fps: float | None = None
    audio: list[dict] = field(default_factory=list)
    subtitles: list[dict] = field(default_factory=list)
    chapters: int = 0


def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    """GET a URL and return the body, raising a clear error on failure."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise RuntimeError(f"request failed: {url}: {e}") from e


# Maps a short id to its variant name. Populated from the manifest by
# `index_variant_ids`, so the join works even when a server rewrites a title.
_SHORT_TO_NAME: dict[str, str] = {}


def index_variant_ids(manifest: dict) -> int:
    """Teach `variant_from_path` the short id of every variant. Returns how many."""
    _SHORT_TO_NAME.clear()
    for entry in manifest.get("entries", []) or []:
        value = entry.get("uuid")
        if value:
            _SHORT_TO_NAME[short(value)] = entry["name"]
    return len(_SHORT_TO_NAME)


def variant_from_path(path: str) -> str | None:
    """Recover the variant name from a library path.

    Prefers the short id the library writes into every filename, because that
    survives a rename and is unambiguous. Falls back to the older convention,
    "<Show> - S01E01 - <variant>.<ext>", so a library built before ids still joins.
    """
    stem = Path(path).stem
    short_id = short_from_path(stem)
    if short_id:
        name = _SHORT_TO_NAME.get(short_id)
        if name:
            return name
        log.debug("path carries unknown short id %s: %s", short_id, path)
    stem = SHORT_RE.sub("", stem).strip()
    if " - " not in stem:
        return stem or None
    return stem.rsplit(" - ", 1)[-1]


# --------------------------------------------------------------------------- Plex

def _plex_headers() -> dict:
    """Auth header for Plex, empty when no token is configured."""
    token = plex_token()
    return {"X-Plex-Token": token} if token else {}


def plex_inventory(base: str) -> dict[str, ServerItem]:
    """Return every episode Plex holds, keyed by variant name."""
    sections = ET.fromstring(_get(f"{base}/library/sections", _plex_headers()))
    items: dict[str, ServerItem] = {}
    for directory in sections.findall(".//Directory"):
        key = directory.get("key")
        body = _get(f"{base}/library/sections/{key}/all?type=4", _plex_headers())
        for video in ET.fromstring(body).findall(".//Video"):
            # Chapters are omitted unless explicitly requested, so without this the
            # report claims Plex found none when it read every one of them.
            detail = ET.fromstring(_get(f"{base}{video.get('key')}?includeChapters=1", _plex_headers()))
            for node in detail.findall(".//Video"):
                item = _plex_item(node)
                if item:
                    items[item.variant] = item
    return items


def _plex_item(node: ET.Element) -> ServerItem | None:
    media = node.find("Media")
    part = media.find("Part") if media is not None else None
    if part is None:
        return None
    path = part.get("file", "")
    variant = variant_from_path(path)
    if not variant:
        return None

    audio, subtitles = [], []
    for stream in part.findall("Stream"):
        kind = stream.get("streamType")
        if kind == "2":
            audio.append({"codec": stream.get("codec"),
                          "channels": int(stream.get("channels") or 0) or None})
        elif kind == "3":
            subtitles.append({"codec": stream.get("codec")})

    def _f(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return ServerItem(
        variant=variant,
        title=node.get("title", ""),
        path=path,
        container=media.get("container"),
        video_codec=media.get("videoCodec"),
        width=int(media.get("width")) if media.get("width") else None,
        height=int(media.get("height")) if media.get("height") else None,
        aspect=_f(media.get("aspectRatio")),
        fps=_f(media.get("videoFrameRate", "").rstrip("p") or None),
        audio=audio,
        subtitles=subtitles,
        chapters=len(node.findall("Chapter")),
    )


# ----------------------------------------------------------------------- Jellyfin

def _post_json(url: str, payload: dict | None = None, headers: dict | None = None) -> None:
    """POST JSON and ignore the (empty) body. Raises on failure."""
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=60):
        return


def jellyfin_needs_setup(base: str) -> bool:
    """True when the startup wizard has not been completed."""
    info = json.loads(_get(f"{base}/System/Info/Public"))
    return not info.get("StartupWizardCompleted", False)


def jellyfin_bootstrap(base: str, user: str, password: str) -> None:
    """Complete the startup wizard and create the library over the API.

    Without this the check is only usable on a server someone already configured by
    hand, and a `make servers-reset` would silently break it.

    Trickplay and chapter-image extraction are switched off at creation: left on,
    Jellyfin generates sprite sheets for every file, including the 100-chapter one.
    """
    log.info("jellyfin: running first-time setup")
    _post_json(f"{base}/Startup/Configuration",
               {"UICulture": "en-US", "MetadataCountryCode": "US",
                "PreferredMetadataLanguage": "en"})
    _get(f"{base}/Startup/User")
    _post_json(f"{base}/Startup/User", {"Name": user, "Password": password})
    _post_json(f"{base}/Startup/RemoteAccess",
               {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
    _post_json(f"{base}/Startup/Complete")

    token = jellyfin_token(base, user, password)
    headers = {"Authorization": f'MediaBrowser Token="{token}"'}
    for name, collection, path in (("Testlib Shows", "tvshows", "/media/shows"),
                                   ("Testlib Audio", "music", "/media/audio")):
        query = urllib.parse.urlencode(
            {"name": name, "collectionType": collection, "refreshLibrary": "false"})
        _post_json(
            f"{base}/Library/VirtualFolders?{query}",
            {"LibraryOptions": {
                "PathInfos": [{"Path": path}],
                "EnableTrickplayImageExtraction": False,
                "ExtractTrickplayImagesDuringLibraryScan": False,
                "EnableChapterImageExtraction": False,
                "ExtractChapterImagesDuringLibraryScan": False,
                "EnableInternetProviders": False,
                "SaveLocalMetadata": False,
                "EnableRealtimeMonitor": False,
                "MetadataSavers": [],
                "LocalMetadataReaderOrder": ["Nfo"],
            }},
            headers)
        log.info("jellyfin: created %s -> %s", name, path)
    _post_json(f"{base}/Library/Refresh", None, headers)
    log.info("jellyfin: scanning")


def jellyfin_token(base: str, user: str, password: str) -> str:
    """Authenticate and return an access token."""
    payload = json.dumps({"Username": user, "Pw": password}).encode()
    request = urllib.request.Request(
        f"{base}/Users/AuthenticateByName", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": 'MediaBrowser Client="testlib", Device="servercheck", '
                                  'DeviceId="testlib-servercheck", Version="1.0.0"'},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["AccessToken"]


def jellyfin_inventory(base: str, token: str) -> dict[str, ServerItem]:
    """Return every episode Jellyfin holds, keyed by variant name."""
    headers = {"Authorization": f'MediaBrowser Token="{token}"'}
    query = urllib.parse.urlencode({
        "Recursive": "true",
        "IncludeItemTypes": "Episode,Video,Audio",
        "Fields": "MediaSources,MediaStreams,Chapters,Path",
        "Limit": "10000",
    })
    body = json.loads(_get(f"{base}/Items?{query}", headers))
    items: dict[str, ServerItem] = {}
    for entry in body.get("Items", []):
        variant = variant_from_path(entry.get("Path") or "")
        if not variant:
            continue
        source = (entry.get("MediaSources") or [{}])[0]
        streams = source.get("MediaStreams") or entry.get("MediaStreams") or []
        video = next((s for s in streams if s.get("Type") == "Video"), {})
        items[variant] = ServerItem(
            variant=variant,
            title=entry.get("Name", ""),
            path=entry.get("Path", ""),
            container=source.get("Container"),
            video_codec=video.get("Codec"),
            width=video.get("Width"),
            height=video.get("Height"),
            aspect=_parse_aspect(video.get("AspectRatio")),
            fps=video.get("RealFrameRate") or video.get("AverageFrameRate"),
            audio=[{"codec": s.get("Codec"), "channels": s.get("Channels")}
                   for s in streams if s.get("Type") == "Audio"],
            subtitles=[{"codec": s.get("Codec")}
                       for s in streams if s.get("Type") == "Subtitle"],
            chapters=len(entry.get("Chapters") or []),
        )
    return items


def _parse_aspect(value: str | None) -> float | None:
    """Parse an aspect string like '16:9' into a float."""
    if not value:
        return None
    try:
        if ":" in value:
            num, den = value.split(":")
            return float(num) / float(den)
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------- diff

# Variants whose whole purpose is to be malformed. A server refusing them, or
# reporting something odd, is the correct outcome rather than a defect.
# Plex reports aspectRatio from a fixed bucket set -- only 1.33, 1.78, 1.85 and 2.35
# were observed across the whole library -- so it is a coarse classification rather
# than the true display aspect. Its width and height are exact, so those are what get
# compared; the bucket is recorded as a note instead of a mismatch.
BUCKETED_ASPECT = {"plex"}

EXPECTED_TROUBLE = {
    "truncated", "corrupt_tail", "broken_timestamps", "missing_metadata",
    "chapters_stripped", "chapters_overlap", "chapters_zero_length",
    "chapters_past_eof", "chapters_out_of_order",
}


def expected_on_disk() -> dict[str, dict]:
    """Return the manifest entries that are actually present in library/.

    Only linked files can be scanned, so pending externals and the stream
    directories are not part of what a server is expected to find.
    """
    manifest = json.loads((GENERATED_DIR / "manifest.json").read_text(encoding="utf-8"))
    # Must happen before any path is resolved, or the id lookup finds an empty index.
    index_variant_ids(manifest)
    linked = {variant_from_path(str(p)) for p in LIBRARY_DIR.rglob("*")
              if p.is_file() and p.suffix not in (".nfo", ".jpg")}
    return {e["name"]: e for e in manifest["entries"]
            if e.get("status") == "built" and e["name"] in linked}


def _close(a, b, tolerance: float) -> bool:
    if a is None or b is None:
        return True  # not reported by one side; absence is checked separately
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False


def compare(entry: dict, item: ServerItem, server: str = "") -> tuple[list[str], list[str]]:
    """Return (issues, notes) describing how a server's view differs from the manifest.

    Notes are differences that are the server's documented behaviour rather than a
    defect, so they are worth reporting but must not fail the run.
    """
    issues: list[str] = []
    notes: list[str] = []
    video = entry.get("video") or {}

    if video.get("codec") and item.video_codec:
        if video["codec"].lower() != item.video_codec.lower():
            issues.append(f"video codec: manifest {video['codec']}, server {item.video_codec}")

    for field_name, ours, theirs in (("width", video.get("width"), item.width),
                                     ("height", video.get("height"), item.height)):
        if ours and theirs and int(ours) != int(theirs):
            issues.append(f"{field_name}: manifest {ours}, server {theirs}")

    if video.get("display_aspect") and item.aspect:
        if not _close(video["display_aspect"], item.aspect, 0.02):
            message = f"aspect: manifest {video['display_aspect']}, server {item.aspect:.3f}"
            (notes if server in BUCKETED_ASPECT else issues).append(message)

    if video.get("fps") and item.fps:
        if not _close(video["fps"], item.fps, 0.05):
            issues.append(f"fps: manifest {video['fps']}, server {item.fps}")

    ours_audio = len(entry.get("audio_tracks") or [])
    if ours_audio and len(item.audio) != ours_audio:
        issues.append(f"audio tracks: manifest {ours_audio}, server {len(item.audio)}")

    ours_subs = len(entry.get("subtitle_tracks") or [])
    if ours_subs and len(item.subtitles) != ours_subs:
        issues.append(f"subtitle tracks: manifest {ours_subs}, server {len(item.subtitles)}")

    ours_chapters = len(entry.get("chapters") or [])
    if ours_chapters and item.chapters and item.chapters != ours_chapters:
        issues.append(f"chapters: manifest {ours_chapters}, server {item.chapters}")

    return issues, notes


def build_report(expected: dict[str, dict], inventories: dict[str, dict[str, ServerItem]],
                 targets: dict[str, str] | None = None) -> dict:
    """Diff every server inventory against the manifest.

    `_meta` records when the scan ran and which servers answered. A report read
    without that cannot be distinguished from a stale one, and a stale report
    claiming everything matched is worse than no report.
    """
    report: dict = {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "targets": targets or {},
        },
        "servers": {},
        "expected_items": len(expected),
    }
    for server, items in inventories.items():
        missing, mismatched, tolerated, structural, notes = [], [], [], [], []
        for name, entry in sorted(expected.items()):
            flags = set(entry.get("flags") or [])
            deliberate = bool(flags & EXPECTED_TROUBLE)
            item = items.get(name)
            if item is None:
                record = {
                    "variant": name,
                    "reason": "not scanned",
                    "section": entry.get("section"),
                    "container": entry.get("container"),
                    "has_video": bool(entry.get("video")),
                }
                if not entry.get("video"):
                    # A TV library indexes video. Both servers skip audio-only files
                    # here, so this is the shape of the library rather than a defect.
                    record["reason"] = "audio-only file in a TV library"
                    structural.append(record)
                elif deliberate:
                    tolerated.append(record)
                else:
                    missing.append(record)
                continue
            issues, item_notes = compare(entry, item, server)
            if item_notes:
                notes.append({"variant": name, "notes": item_notes})
            if issues:
                (tolerated if deliberate else mismatched).append({
                    "variant": name, "issues": issues})
        extra = sorted(set(items) - set(expected))
        report["servers"][server] = {
            "scanned": len(items),
            "missing": missing,
            "mismatched": mismatched,
            "tolerated": tolerated,
            "structural": structural,
            "notes": notes,
            "unexpected": extra,
            "ok": len(expected) - len(missing) - len(mismatched) - len(structural),
        }
    return report


def summarise_missing(missing: list[dict]) -> dict:
    """Group missing items so a pattern is visible rather than a list of names."""
    groups: dict[str, int] = {}
    for entry in missing:
        key = "audio-only" if not entry["has_video"] else f"container:{entry['container']}"
        groups[key] = groups.get(key, 0) + 1
    return dict(sorted(groups.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- cli

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check a media server scanned the library correctly.")
    p.add_argument("--plex", default=DEFAULT_PLEX, help="Plex base URL ('' to skip).")
    p.add_argument("--jellyfin", default=DEFAULT_JELLYFIN, help="Jellyfin base URL ('' to skip).")
    p.add_argument("--jellyfin-user", default="testlib")
    p.add_argument("--jellyfin-password", default="testlib-validate")
    p.add_argument("--output", default=str(GENERATED_DIR / "server_report.json"))
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    expected = expected_on_disk()
    if not expected:
        log.error("nothing to check: build the library first with 'make library'")
        return 2
    log.info("expecting %d linked variants", len(expected))

    inventories: dict[str, dict[str, ServerItem]] = {}
    if ns.plex:
        try:
            inventories["plex"] = plex_inventory(ns.plex)
            log.info("plex: %d items", len(inventories["plex"]))
        except RuntimeError as e:
            log.error("plex unreachable: %s", e)
    if ns.jellyfin:
        try:
            if jellyfin_needs_setup(ns.jellyfin):
                jellyfin_bootstrap(ns.jellyfin, ns.jellyfin_user, ns.jellyfin_password)
                log.info("jellyfin: waiting for the first scan")
                time.sleep(90)
            token = jellyfin_token(ns.jellyfin, ns.jellyfin_user, ns.jellyfin_password)
            inventories["jellyfin"] = jellyfin_inventory(ns.jellyfin, token)
            log.info("jellyfin: %d items", len(inventories["jellyfin"]))
        except (RuntimeError, urllib.error.URLError, KeyError) as e:
            log.error("jellyfin unreachable: %s", e)

    if not inventories:
        log.error("no server could be reached")
        return 2

    targets = {k: v for k, v in (("plex", ns.plex), ("jellyfin", ns.jellyfin))
               if v and k in inventories}
    report = build_report(expected, inventories, targets)
    out_path = Path(ns.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    sys.stdout.write("\n=== server scan report ===\n")
    sys.stdout.write(f"expected (linked, built): {report['expected_items']}\n")
    failed = False
    for server, result in report["servers"].items():
        sys.stdout.write(
            f"\n{server}: scanned {result['scanned']}, ok {result['ok']}, "
            f"missing {len(result['missing'])}, mismatched {len(result['mismatched'])}, "
            f"structural {len(result['structural'])}, notes {len(result['notes'])}\n")
        if result["structural"]:
            sys.stdout.write(
                f"    {len(result['structural'])} audio-only files not indexed "
                f"(expected: a TV library indexes video)\n")
        if result["notes"]:
            sample = result["notes"][0]["notes"][0]
            sys.stdout.write(
                f"    {len(result['notes'])} server-behaviour notes, e.g. {sample}\n")
        if result["missing"]:
            for group, count in summarise_missing(result["missing"]).items():
                sys.stdout.write(f"    missing by {group}: {count}\n")
            failed = True
        for entry in result["mismatched"][:10]:
            sys.stdout.write(f"    {entry['variant']}: {'; '.join(entry['issues'])}\n")
        if len(result["mismatched"]) > 10:
            sys.stdout.write(f"    ... and {len(result['mismatched']) - 10} more\n")
        if result["mismatched"]:
            failed = True
    sys.stdout.write(f"\nreport: {out_path}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
