"""Stable identity for every variant: a UUID, a short form, and a display name.

Each variant owns one UUIDv5 derived from its matrix name under a fixed namespace.
Derivation makes the first mint reproducible from nothing, which the rest of this
generator already guarantees for media. The value is then written back into
`config/media_matrix.json` and never recomputed, so renaming a variant later keeps
the identifier that external systems have already recorded.

The short form is the first 8 hex digits. It is what goes in filenames, because a
full UUID costs 36 characters in every path and 8 digits separate 109 variants with
room to spare. `SHORT_RE` finds one in a path; `resolve_short` maps it back.
"""
from __future__ import annotations

import logging
import re
import uuid

log = logging.getLogger("ids")

# Frozen. Changing this re-mints every identifier in the corpus.
NAMESPACE = uuid.UUID("6f1d4a2e-8c33-5b7a-9e04-2d18f7a6c5b1")

SHORT_LEN = 8
# Matches the bracketed short id that `decorate_filename` writes.
SHORT_RE = re.compile(r"\[([0-9a-f]{%d})\]" % SHORT_LEN)

# Variant lists in the matrix. `defaults` is config, not a variant.
VARIANT_SECTIONS = (
    "video_variants",
    "master_variants",
    "audio_only_variants",
    "subtitle_variants",
    "stream_variants",
    "edge_cases",
    "external_variants",
)


def mint(name: str) -> str:
    """Derive the canonical UUID for a variant name. Pure, so it is reproducible."""
    if not name:
        raise ValueError("variant name must be a non-empty string")
    return str(uuid.uuid5(NAMESPACE, name))


def short(variant_uuid: str) -> str:
    """Return the filename-sized form of a UUID."""
    cleaned = str(variant_uuid).replace("-", "")
    if len(cleaned) < SHORT_LEN:
        raise ValueError(f"not a UUID: {variant_uuid!r}")
    return cleaned[:SHORT_LEN]


def decorate_filename(stem: str, variant_uuid: str) -> str:
    """Append the short id to a filename stem, without duplicating one already there."""
    if SHORT_RE.search(stem):
        return stem
    return f"{stem} [{short(variant_uuid)}]"


def short_from_path(path: str) -> str | None:
    """Pull the short id out of a path, or None when the path carries no id."""
    match = SHORT_RE.search(str(path))
    return match.group(1) if match else None


def iter_variants(cfg: dict):
    """Yield (section, variant) for every variant in a loaded matrix."""
    for section in VARIANT_SECTIONS:
        for variant in cfg.get(section, []) or []:
            if isinstance(variant, dict) and variant.get("name"):
                yield section, variant


def ensure_uuids(cfg: dict) -> int:
    """Fill in any missing `uuid` field in a loaded matrix. Returns how many were added.

    An existing value is never overwritten. That is the whole point: once an
    identifier is published it outlives the name it was derived from.
    """
    added = 0
    for _section, variant in iter_variants(cfg):
        if not variant.get("uuid"):
            variant["uuid"] = mint(variant["name"])
            added += 1
    return added


def check_uuids(cfg: dict) -> list[str]:
    """Report identity problems in a loaded matrix: missing, malformed or duplicated."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for _section, variant in iter_variants(cfg):
        name = variant["name"]
        value = variant.get("uuid")
        if not value:
            problems.append(f"{name}: no uuid")
            continue
        try:
            uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            problems.append(f"{name}: malformed uuid {value!r}")
            continue
        if value in seen:
            problems.append(f"{name}: uuid collides with {seen[value]}")
        seen[value] = name
        key = short(value)
        for other, other_name in list(seen.items()):
            if other != value and short(other) == key:
                problems.append(f"{name}: short id {key} collides with {other_name}")
    return problems


def resolve_short(short_id: str, entries) -> dict | None:
    """Find the manifest entry owning a short id. `entries` is any iterable of dicts."""
    for entry in entries:
        value = entry.get("uuid")
        if value and short(value) == short_id:
            return entry
    return None


def main(argv: list[str] | None = None) -> int:
    """Mint a UUID for any variant that lacks one, or report identity problems.

    Adding a variant to the matrix by hand leaves it without an identifier, and
    everything downstream refuses to run until it has one. This fills the gap in
    place; existing values are never touched.
    """
    import argparse
    import json
    import logging
    from pathlib import Path

    from .generate import CONFIG_PATH

    p = argparse.ArgumentParser(description="Mint missing variant UUIDs in the matrix.")
    p.add_argument("--check", action="store_true",
                   help="Report problems and exit non-zero; change nothing.")
    p.add_argument("--config", default=str(CONFIG_PATH))
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    path = Path(a.config)
    cfg = json.loads(path.read_text(encoding="utf-8"))

    if a.check:
        problems = check_uuids(cfg)
        for problem in problems:
            log.error("%s", problem)
        if not problems:
            log.info("all %d variants have a valid, unique id",
                     sum(1 for _ in iter_variants(cfg)))
        return 1 if problems else 0

    added = ensure_uuids(cfg)
    if added:
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        for _section, variant in iter_variants(cfg):
            log.info("minted %s for %s", short(variant["uuid"]), variant["name"])
    problems = check_uuids(cfg)
    for problem in problems:
        log.error("%s", problem)
    if not added:
        log.info("every variant already has an id")
    return 1 if problems else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
