"""Join the variants in `generated/manifest.json` to the items in a Plex library.

Plex will not index a UUID for searching and drops embedded tags outside the MP4
family, so no single Plex field carries our identifier everywhere. This walks four
channels in order of trust and reports which one produced each match, so a partial
result is diagnosable rather than mysterious:

  path     the short id the library writes into every filename  -- every container
  summary  `testlib-uuid=` from the embedded tags               -- ISOBMFF only
  label    a `testlib-uuid:` label written by a previous run    -- every container
  name     the bare variant name in the filename                -- pre-id libraries

With --write-labels it stamps `testlib-uuid:<uuid>` onto every matched item, which
turns the identifier into something Plex can filter on exactly:

    GET /library/sections/<id>/all?type=1&label=testlib-uuid:<uuid>
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from .generate import GENERATED_DIR
from .ids import short, short_from_path
from .plexsafety import (UnsafePlexTarget, plex_token, require_test_server,
                         server_identity)

log = logging.getLogger("plexmatch")

DEFAULT_PLEX = "http://localhost:32402"
# Written on every run. Downstream tooling joins on this file, so it must not
# depend on remembering a flag.
DEFAULT_JSON = GENERATED_DIR / "plex_match.json"
LABEL_PREFIX = "testlib-uuid:"
SUMMARY_RE = re.compile(r"testlib-uuid=([0-9a-fA-F-]{36})")

# Plex library type -> the `type=` value that lists its leaf items.
LEAF_TYPE = {"movie": 1, "show": 4, "artist": 10, "photo": 13}


@dataclass
class PlexItem:
    """One leaf item as Plex reports it."""
    rating_key: str
    guid: str
    title: str
    summary: str
    path: str
    section: str
    section_key: str
    labels: list[str] = field(default_factory=list)


@dataclass
class Match:
    """One variant and every Plex item representing it.

    The faceted library hardlinks a variant into each show whose facet it satisfies,
    so one variant legitimately appears many times. `item` is the first occurrence,
    kept for a compact report; `items` is the full set, which is what a label write
    has to walk.
    """
    name: str
    uuid: str
    title: str
    items: list[PlexItem]
    via: str

    @property
    def item(self) -> PlexItem:
        return self.items[0]


# Section key -> the `type=` value a write to that section must carry. Filled by
# `plex_items`, because a label PUT is rejected when the type does not match.
_SECTION_LEAF: dict[str, int] = {}


def _leaf_type_for(item: PlexItem) -> int:
    """Return the Plex item type to use when writing to this item's section."""
    return _SECTION_LEAF.get(item.section_key, 1)


def _get(url: str, token: str | None = None, timeout: int = 60) -> bytes:
    """GET a URL and return the body, raising a clear error on failure."""
    headers = {"Accept": "application/xml"}
    if token:
        headers["X-Plex-Token"] = token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise RuntimeError(f"Plex request failed: {url}: {e}") from e


def _put(url: str, token: str | None = None, timeout: int = 60) -> None:
    """PUT a URL, used for the label write-back."""
    headers = {}
    if token:
        headers["X-Plex-Token"] = token
    request = urllib.request.Request(url, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise RuntimeError(f"Plex write failed: {url}: {e}") from e


def plex_sections(base: str, token: str | None) -> list[tuple[str, str, str]]:
    """Return (key, title, type) for every library section on the server."""
    root = ET.fromstring(_get(f"{base}/library/sections", token))
    out = []
    for d in root.findall(".//Directory"):
        key, title, kind = d.get("key"), d.get("title") or "", d.get("type") or ""
        if key:
            out.append((key, title, kind))
    return out


def plex_items(base: str, token: str | None,
               sections: list[str] | None = None) -> list[PlexItem]:
    """Return every leaf item across the requested sections, or all sections."""
    items: list[PlexItem] = []
    for key, title, kind in plex_sections(base, token):
        if sections and key not in sections and title not in sections:
            continue
        leaf = LEAF_TYPE.get(kind)
        if leaf is None:
            log.debug("skipping section %s: unsupported type %r", title, kind)
            continue
        _SECTION_LEAF[key] = leaf
        body = _get(f"{base}/library/sections/{key}/all?type={leaf}", token)
        root = ET.fromstring(body)
        for node in list(root.findall(".//Video")) + list(root.findall(".//Track")):
            part = node.find(".//Part")
            items.append(PlexItem(
                rating_key=node.get("ratingKey") or "",
                guid=node.get("guid") or "",
                title=node.get("title") or "",
                summary=node.get("summary") or "",
                path=(part.get("file") if part is not None else "") or "",
                section=title,
                section_key=key,
                labels=[l.get("tag") or "" for l in node.findall("Label")],
            ))
    return items


def _index(items: list[PlexItem]) -> tuple[dict, dict, dict, dict]:
    """Build the four lookup tables the join walks, in order of trust.

    Every table maps a key to a *list*, because the faceted library puts the same
    variant in several shows and each copy is a separate Plex item.
    """
    by_short: dict[str, list] = {}
    by_summary: dict[str, list] = {}
    by_label: dict[str, list] = {}
    by_name: dict[str, list] = {}
    for item in items:
        sid = short_from_path(item.path)
        if sid:
            by_short.setdefault(sid, []).append(item)
        m = SUMMARY_RE.search(item.summary or "")
        if m:
            by_summary.setdefault(m.group(1).lower(), []).append(item)
        for tag in item.labels:
            if tag.lower().startswith(LABEL_PREFIX):
                by_label.setdefault(tag[len(LABEL_PREFIX):].lower(), []).append(item)
        stem = Path(item.path).stem if item.path else ""
        if stem:
            by_name.setdefault(stem.rsplit(" - ", 1)[-1].strip(), []).append(item)
    return by_short, by_summary, by_label, by_name


def match(manifest: dict, items: list[PlexItem]) -> tuple[list[Match], list[dict], list[PlexItem]]:
    """Pair manifest variants with Plex items.

    Returns (matches, unmatched_variants, unmatched_plex_items).
    """
    by_short, by_summary, by_label, by_name = _index(items)
    matches: list[Match] = []
    unmatched: list[dict] = []
    claimed: set[str] = set()

    for entry in manifest.get("entries", []) or []:
        variant_uuid = entry.get("uuid")
        if not variant_uuid:
            unmatched.append(entry)
            continue
        key = variant_uuid.lower()
        found: list[PlexItem] = []
        via = None
        for source, table, probe in (
            ("path", by_short, short(variant_uuid)),
            ("summary", by_summary, key),
            ("label", by_label, key),
            ("name", by_name, entry["name"]),
        ):
            candidate = table.get(probe)
            if candidate:
                found, via = candidate, source
                break
        if not found:
            unmatched.append(entry)
            continue
        claimed.update(i.rating_key for i in found)
        matches.append(Match(name=entry["name"], uuid=variant_uuid,
                             title=entry.get("title") or entry["name"],
                             items=found, via=via))

    stray = [i for i in items if i.rating_key not in claimed]
    return matches, unmatched, stray


def hydrate_labels(base: str, token: str | None, matches: list[Match]) -> None:
    """Fill in each matched item's current labels from its detail endpoint.

    A section listing omits <Label>, so without this every run would rewrite labels
    that are already correct. Only called before a write, because it costs one
    request per item.
    """
    for m in matches:
        for item in m.items:
            try:
                body = _get(f"{base}/library/metadata/{item.rating_key}", token)
            except RuntimeError as e:
                log.debug("could not read labels for %s: %s", item.rating_key, e)
                continue
            item.labels = [l.get("tag") or ""
                           for l in ET.fromstring(body).findall(".//Label")]


def write_labels(base: str, token: str | None, matches: list[Match],
                 *, dry_run: bool = False) -> int:
    """Stamp `testlib-uuid:<uuid>` onto each matched item. Returns how many were written.

    Skips an item that already carries the right label, so a repeat run is cheap and
    does not churn Plex's tag table.
    """
    written = 0
    for m in matches:
        want = f"{LABEL_PREFIX}{m.uuid}"
        for item in m.items:
            if any(t.lower() == want.lower() for t in item.labels):
                continue
            # Measured: `label[N].tag.tag=` appends, it does not replace the array,
            # so labels a person set by hand survive. Removal needs the `-=` form.
            query = urllib.parse.urlencode({
                "type": _leaf_type_for(item),
                "id": item.rating_key,
                "label[0].tag.tag": want,
                "label.locked": 1,
            })
            url = f"{base}/library/sections/{item.section_key}/all?{query}"
            if dry_run:
                log.info("would label %s (%s) -> %s", item.rating_key, item.section, want)
            else:
                _put(url, token)
            written += 1
    return written


def _report(matches, unmatched, stray, *, show_all: bool) -> None:
    """Print a human-readable summary of the join."""
    via_counts: dict[str, int] = {}
    for m in matches:
        via_counts[m.via] = via_counts.get(m.via, 0) + 1

    print(f"\nmatched {len(matches)}  unmatched-variants {len(unmatched)}  "
          f"plex-items-with-no-variant {len(stray)}")
    if via_counts:
        print("  matched via: " + ", ".join(f"{k}={v}" for k, v in sorted(via_counts.items())))

    if matches:
        print(f"\n{'variant':34} {'short':9} {'via':8} {'copies':7} {'ratingKey':10} plex title")
        print("-" * 112)
        for m in sorted(matches, key=lambda x: x.name)[: None if show_all else 20]:
            print(f"{m.name[:33]:34} {short(m.uuid):9} {m.via:8} {len(m.items):<7} "
                  f"{m.item.rating_key:10} {m.item.title[:34]}")
        if not show_all and len(matches) > 20:
            print(f"  ... {len(matches) - 20} more (use --all)")

    if unmatched:
        print(f"\nvariants Plex does not have ({len(unmatched)}):")
        for e in unmatched[: None if show_all else 15]:
            print(f"  {e['name']:34} {e.get('status','?')}")
        if not show_all and len(unmatched) > 15:
            print(f"  ... {len(unmatched) - 15} more (use --all)")

    if stray:
        print(f"\nPlex items with no variant ({len(stray)}):")
        for i in stray[: None if show_all else 15]:
            print(f"  [{i.section}] {i.title[:44]:45} {Path(i.path).name[:40]}")
        if not show_all and len(stray) > 15:
            print(f"  ... {len(stray) - 15} more (use --all)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Join testlib variants to a Plex library.")
    p.add_argument("--plex", default=DEFAULT_PLEX, help="Plex base URL.")
    p.add_argument("--token", default=None,
                   help="X-Plex-Token. Defaults to PLEX_TOKEN or docker/.env.")
    p.add_argument("--section", action="append", default=None,
                   help="Limit to a section key or title. Repeatable.")
    p.add_argument("--manifest", default=str(GENERATED_DIR / "manifest.json"))
    p.add_argument("--json", dest="json_out", default=str(DEFAULT_JSON),
                   help=f"Where to write the join (default: {DEFAULT_JSON.name}). "
                        f"Pass '' to skip.")
    p.add_argument("--write-labels", action="store_true",
                   help="Stamp testlib-uuid:<uuid> onto matched Plex items.")
    p.add_argument("--dry-run", action="store_true",
                   help="With --write-labels, report the writes without making them.")
    p.add_argument("--allow-claimed-server", action="store_true",
                   help="Permit label writes to a claimed (real) Plex server.")
    p.add_argument("--all", action="store_true", help="Print every row, not a sample.")
    p.add_argument("--strict", action="store_true",
                   help="Also exit non-zero when any variant is unmatched.")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    a.token = a.token or plex_token()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    manifest_path = Path(a.manifest)
    if not manifest_path.is_file():
        log.error("no manifest at %s -- run `python3 -m scripts.manifest` first", manifest_path)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    try:
        items = plex_items(a.plex, a.token, a.section)
    except RuntimeError as e:
        log.error("%s", e)
        return 2
    log.info("Plex returned %d items", len(items))

    matches, unmatched, stray = match(manifest, items)
    _report(matches, unmatched, stray, show_all=a.all)

    if a.write_labels:
        try:
            # A label write is not undone by a rescan, so confirm the target is the
            # disposable container before touching anything.
            require_test_server(a.plex, a.token, allow_claimed=a.allow_claimed_server)
        except UnsafePlexTarget as e:
            log.error("%s", e)
            return 2
        try:
            hydrate_labels(a.plex, a.token, matches)
            n = write_labels(a.plex, a.token, matches, dry_run=a.dry_run)
        except RuntimeError as e:
            log.error("%s", e)
            return 2
        verb = "would write" if a.dry_run else "wrote"
        print(f"\n{verb} {n} label(s)")

    if a.json_out:
        _write_json(Path(a.json_out), a.plex, a.token, matches, unmatched, stray)
        print(f"wrote {a.json_out}")

    # A Plex item with no variant is the failure worth reporting: the library and
    # the server disagree about something that is actually there. Unmatched
    # variants are mostly expected and are listed in the report -- pending
    # external slots, stream directories the library excludes, subtitle sidecars,
    # and containers no scanner indexes. Failing on those made `make plexmatch`
    # exit non-zero on every healthy run, which trains people to ignore it.
    if stray:
        return 1
    return 1 if (a.strict and unmatched) else 0


def _write_json(path: Path, base: str, token: str | None, matches: list[Match],
                unmatched: list[dict], stray: list[PlexItem]) -> None:
    """Write the join for downstream tooling.

    `_meta` records which server produced this and when, because a consumer joining
    against a different Plex, or against a stale file, would otherwise get silently
    wrong answers. `by_uuid` is the lookup the join actually needs: a variant UUID to
    the Plex rating keys representing it, without walking `matched`.
    """
    try:
        identity = server_identity(base, token)
    except UnsafePlexTarget:
        identity = {"machine_identifier": "", "claimed": "", "version": ""}

    payload = {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "plex_url": base,
            "plex_machine_identifier": identity.get("machine_identifier", ""),
            "plex_version": identity.get("version", ""),
            "matched": len(matches),
            "unmatched_variants": len(unmatched),
            "unmatched_plex_items": len(stray),
        },
        "by_uuid": {m.uuid: [i.rating_key for i in m.items] for m in matches},
        "matched": [{"name": m.name, "uuid": m.uuid, "uuid_short": short(m.uuid),
                     "title": m.title, "via": m.via, "copies": len(m.items),
                     "plex": [{"ratingKey": i.rating_key, "guid": i.guid,
                               "title": i.title, "path": i.path,
                               "section": i.section} for i in m.items]}
                    for m in matches],
        "unmatched_variants": [{"name": e["name"], "uuid": e.get("uuid"),
                                "status": e.get("status")} for e in unmatched],
        "unmatched_plex": [{"ratingKey": i.rating_key, "title": i.title,
                            "path": i.path, "section": i.section} for i in stray],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
