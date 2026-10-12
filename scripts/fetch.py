"""Acquire the source assets this generator cannot author itself.

Three kinds of media exist in the matrix, and only the third needs this module:

1. **Generated.** ffmpeg builds it from `sources/`. Most of the corpus.
2. **Seeded.** ffmpeg builds the file, but one stream is copied from a small asset
   ffmpeg has no encoder for — a Blu-ray PGS bitstream, a VobSub pair. The video is
   ours; only the bitmap subtitle is acquired.
3. **Ingested.** The whole file comes from elsewhere, because no encoder can produce
   it: DTS-HD MA, TrueHD Atmos, a Dolby Vision reference clip.

Assets for 2 and 3 are declared in `config/media_matrix.json` under `source_downloads`.
Each entry says where the bytes come from:

    "url"          fetch over HTTPS; public and re-fetchable
    "local_hint"   a path inside a private media library; symlinked, never copied

An entry may carry both. A URL that is slow, large or offline is skipped when the
local copy is already linked, so having the file locally always wins.

    python3 -m scripts.fetch --check              # report what is missing
    python3 -m scripts.fetch                      # download everything missing
    python3 -m scripts.fetch --link-from DIR      # symlink from a local media library
    python3 -m scripts.fetch --pin                # record each digest in the matrix

Downloads are checksum-pinned. The first fetch of an unpinned asset prints its digest
and asks for `--pin`; afterwards a mismatch is a hard failure, so an upstream file
that changes under us cannot silently alter the corpus.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .ffutil import sha256_file

log = logging.getLogger("fetch")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "media_matrix.json"

SECTION = "source_downloads"

CONNECT_TIMEOUT = 30
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0
CHUNK = 1 << 20

# A download that claims to be media but arrives as a web page is an error page, a
# login wall or a redirect notice. Writing it would poison the checksum pin.
_HTML_TYPES = ("text/html", "application/xhtml+xml")


class FetchError(RuntimeError):
    """Raised when an asset cannot be acquired. The message names the remedy."""


@dataclass
class Asset:
    """One declared source asset and the state of its local copy."""
    name: str
    path: Path
    url: str | None
    local_hint: str | None
    sha256: str | None
    note: str
    raw: dict

    @property
    def present(self) -> bool:
        """True when the asset resolves to a readable file, symlink included."""
        return self.path.exists() and self.path.is_file()

    @property
    def how(self) -> str:
        """Describe where this asset is meant to come from."""
        if self.url and self.local_hint:
            return "download or local library"
        if self.url:
            return "download"
        if self.local_hint:
            return "local library"
        return "undeclared"


def load_config() -> dict:
    """Read the matrix. Raises FetchError with a remedy when it cannot be parsed."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        raise FetchError(f"matrix not found at {CONFIG_PATH}. Run from the repository root.")
    except json.JSONDecodeError as e:
        raise FetchError(f"matrix is not valid JSON ({e}). Fix {CONFIG_PATH} and retry.")


def assets(cfg: dict) -> list[Asset]:
    """Build the asset list from the matrix, rejecting entries that cannot be acted on."""
    out: list[Asset] = []
    for entry in cfg.get(SECTION, []) or []:
        name = entry.get("name")
        rel = entry.get("path")
        if not name or not rel:
            raise FetchError(f"{SECTION} entry needs both 'name' and 'path': {entry!r}")
        if not entry.get("url") and not entry.get("local_hint"):
            raise FetchError(
                f"source download {name!r} declares neither 'url' nor 'local_hint', "
                f"so nothing can acquire it. Add one to {CONFIG_PATH}."
            )
        out.append(Asset(
            name=name,
            path=ROOT / rel,
            url=entry.get("url"),
            local_hint=entry.get("local_hint"),
            sha256=entry.get("sha256"),
            note=entry.get("license_note", ""),
            raw=entry,
        ))
    return out


def _safe_destination(path: Path) -> None:
    """Refuse to write outside the repository, and refuse to clobber a real directory.

    Resolves the parent directory rather than the file. An asset already linked into
    place is a symlink pointing at a media library outside the repository, and
    resolving the file itself would follow that link and reject its own handiwork.
    """
    parent = path.parent.resolve()
    if ROOT.resolve() not in (parent, *parent.parents):
        raise FetchError(f"refusing to write outside the repository: {path}")
    if path.is_dir() and not path.is_symlink():
        raise FetchError(f"{path} is a directory; expected a file. Remove it and retry.")


def _check_digest(asset: Asset, *, pin: bool, cfg_entry: dict) -> bool:
    """Verify or record the asset's digest. Returns True when the matrix was changed."""
    observed = sha256_file(asset.path)
    if asset.sha256:
        if observed != asset.sha256:
            raise FetchError(
                f"{asset.name}: checksum mismatch.\n"
                f"  expected {asset.sha256}\n"
                f"  observed {observed}\n"
                f"  The upstream file changed, or the local file is not the declared one.\n"
                f"  Delete {asset.path} and re-fetch, or update 'sha256' if the change is wanted."
            )
        log.info("%s: checksum ok", asset.name)
        return False
    if pin:
        cfg_entry["sha256"] = observed
        asset.sha256 = observed  # keep the closing report honest
        log.info("%s: pinned sha256=%s", asset.name, observed)
        return True
    log.warning("%s: no checksum pinned (observed %s). Run with --pin to record it.",
                asset.name, observed)
    return False


def download(asset: Asset, *, force: bool = False) -> bool:
    """Fetch one asset over HTTPS. Returns True when bytes were written.

    Writes to a `.part` file and renames on success, so an interrupted run never
    leaves a truncated file that a later run would mistake for a complete one.
    """
    if not asset.url:
        return False
    if asset.present and not force:
        log.debug("%s: already present", asset.name)
        return False

    _safe_destination(asset.path)
    asset.path.parent.mkdir(parents=True, exist_ok=True)
    part = asset.path.with_name(asset.path.name + ".part")

    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(asset.url, headers={"User-Agent": "testlib-fetch/1"})
            with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
                ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype in _HTML_TYPES:
                    raise FetchError(
                        f"{asset.name}: {asset.url} served {ctype}, not media. "
                        f"The URL has moved or needs a login. Fetch it by hand into "
                        f"{asset.path.relative_to(ROOT)}, or update 'url' in the matrix."
                    )
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                step = max(total // 10, 8 << 20) if total else (32 << 20)
                next_mark = step
                with open(part, "wb") as fh:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        fh.write(block)
                        done += len(block)
                        if done >= next_mark:
                            pct = f" ({done * 100 // total}%)" if total else ""
                            log.info("%s: %d MB%s", asset.name, done >> 20, pct)
                            next_mark += step
            part.replace(asset.path)
            log.info("%s: fetched %d MB", asset.name, asset.path.stat().st_size >> 20)
            return True
        except FetchError:
            part.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            part.unlink(missing_ok=True)
            if attempt < MAX_ATTEMPTS:
                delay = BACKOFF_BASE ** attempt
                log.warning("%s: attempt %d/%d failed (%s); retrying in %.0fs",
                            asset.name, attempt, MAX_ATTEMPTS, e, delay)
                time.sleep(delay)

    raise FetchError(
        f"{asset.name}: download failed after {MAX_ATTEMPTS} attempts ({last}).\n"
        f"  url: {asset.url}\n"
        f"  Check the network, then retry. To supply the file by hand, place it at "
        f"{asset.path.relative_to(ROOT)}."
    )


def link_local(asset: Asset, media_root: Path, *, force: bool = False) -> bool:
    """Symlink an asset from a private media library. Returns True when a link was made.

    A symlink costs nothing and `generate.py` hardlinks through it, so a 1 GB sample
    is ingested without a second copy. The library stays the owner of the bytes.
    """
    if not asset.local_hint:
        return False
    source = (media_root / asset.local_hint).expanduser()
    if not source.is_file():
        log.warning("%s: not in this library at %s", asset.name, source)
        return False

    _safe_destination(asset.path)
    if asset.path.exists() or asset.path.is_symlink():
        if not force:
            log.debug("%s: already present", asset.name)
            return False
        asset.path.unlink()
    asset.path.parent.mkdir(parents=True, exist_ok=True)
    asset.path.symlink_to(source.resolve())
    log.info("%s: linked -> %s", asset.name, source)
    return True


def report(items: list[Asset]) -> int:
    """Print one line per asset. Returns the number still missing."""
    width = max((len(a.name) for a in items), default=10)
    missing = 0
    for asset in items:
        if asset.present:
            size = asset.path.stat().st_size
            pin = "pinned" if asset.sha256 else "unpinned"
            state = f"present  {size >> 20:>5} MB  {pin}"
        else:
            missing += 1
            state = f"MISSING  via {asset.how}"
        print(f"  {asset.name:<{width}}  {state}")
    return missing


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when every declared asset is present."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report what is present and what is missing, then exit")
    parser.add_argument("--link-from", metavar="DIR",
                        help="symlink assets from a local media library at DIR "
                             "(default: $TESTLIB_MEDIA_ROOT)")
    parser.add_argument("--only", metavar="NAME,...",
                        help="restrict to these source_downloads entries")
    parser.add_argument("--pin", action="store_true",
                        help="record the observed sha256 of every unpinned asset")
    parser.add_argument("--force", action="store_true",
                        help="re-acquire assets that are already present")
    parser.add_argument("--no-download", action="store_true",
                        help="link from the local library only; never use the network")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        cfg = load_config()
        items = assets(cfg)
    except FetchError as e:
        log.error("%s", e)
        return 2

    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = wanted - {a.name for a in items}
        if unknown:
            log.error("no such source download: %s. Known: %s",
                      ", ".join(sorted(unknown)), ", ".join(a.name for a in items))
            return 2
        items = [a for a in items if a.name in wanted]

    if not items:
        log.info("no source downloads declared")
        return 0

    if args.check:
        missing = report(items)
        print(f"\n  {len(items) - missing}/{len(items)} present")
        return 1 if missing else 0

    media_root = args.link_from or os.environ.get("TESTLIB_MEDIA_ROOT")
    changed = False
    failures: list[str] = []

    for asset in items:
        try:
            if media_root:
                link_local(asset, Path(media_root), force=args.force)
            if not asset.present and not args.no_download:
                download(asset, force=args.force)
            if asset.present:
                entry = next(e for e in cfg[SECTION] if e.get("name") == asset.name)
                changed |= _check_digest(asset, pin=args.pin, cfg_entry=entry)
            elif not asset.url:
                log.warning("%s: no URL, and not found in the library. %s",
                            asset.name, f"Expected at {asset.local_hint}"
                            if asset.local_hint else "Supply it by hand.")
        except FetchError as e:
            log.error("%s", e)
            failures.append(asset.name)

    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
        log.info("recorded new checksums in %s", CONFIG_PATH.relative_to(ROOT))

    print()
    report(items)
    if failures:
        log.error("failed: %s", ", ".join(failures))
        return 1

    # Only count what this invocation could have acquired. With --no-download a
    # URL-only asset is not this run's job, so leaving it absent is not a failure.
    unresolved = [a for a in items
                  if not a.present and not (args.no_download and not a.local_hint)]
    if unresolved:
        log.warning("still missing: %s", ", ".join(a.name for a in unresolved))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
