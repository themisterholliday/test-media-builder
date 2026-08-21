"""Configure a fresh Plex container so the test library is usable from Plex apps.

The validation harness (scripts/servercheck.py) only needs an unclaimed server and
the API. Using the library from a phone, TV or browser needs more: the server has to
belong to an account so apps can find it, and the media has to be split across three
library types, because one TV library cannot reach all of it.

    /media/shows   TV Shows       faceted browsing by capability
    /media/flat    Other Videos   one item per variant, named exactly
    /media/audio   Music          the audio-only variants

The flat library exists because Plex's TV scanner rewrites show names that look like
they contain technical tokens: "Widescreen 16x9" is filed as "Widescreen". Other Videos
applies no such naming rules, so every variant appears once under its own name.

Claiming is optional and never automatic. Pass a token from https://plex.tv/claim to
attach the server to your own account; without one the server stays unclaimed and is
reachable only over the local API.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("plexsetup")

DEFAULT_PLEX = "http://localhost:32401"

# (title, container path, plex type, agent, scanner)
#
# Agent and scanner generations must match or the server answers 400 with "new scanner
# needs to be paired with new agent". Confusingly, the modern tv.plex.agents.none pairs
# with the plainly-named scanners -- "Plex Video Files", not "Plex Video Files Scanner".
LIBRARIES = [
    ("Testlib Shows", "/media/shows", "show", "tv.plex.agents.none", "Plex TV Series"),
    ("Testlib Videos", "/media/flat", "movie", "tv.plex.agents.none", "Plex Video Files"),
    ("Testlib Audio", "/media/audio", "artist", "tv.plex.agents.none", "Plex Music"),
]


def _request(url: str, method: str = "GET", timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"{method} {url} failed: {e}") from e


def wait_for_server(base: str, timeout: int = 300) -> None:
    """Block until the server is ready to accept configuration.

    Answering /identity is not enough: a freshly created container replies while it is
    still starting plugins, and section creation fails during that window. The
    startState attribute disappears once startup finishes.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            root = ET.fromstring(_request(f"{base}/identity", timeout=10))
            if not root.get("startState"):
                return
            log.debug("waiting: startState=%s", root.get("startState"))
        except (RuntimeError, ET.ParseError):
            pass
        time.sleep(5)
    raise RuntimeError(f"plex did not become ready within {timeout}s: {base}")


def is_claimed(base: str) -> bool:
    root = ET.fromstring(_request(f"{base}/identity"))
    return root.get("claimed") == "1"


def claim(base: str, token: str) -> None:
    """Attach the server to the account that owns the claim token.

    Tokens come from https://plex.tv/claim and expire after about four minutes.
    """
    _request(f"{base}/myplex/claim?token={urllib.parse.quote(token)}", method="POST")
    log.info("server claimed")


def existing_sections(base: str) -> dict[str, str]:
    """Return {title: key} for the sections already present."""
    root = ET.fromstring(_request(f"{base}/library/sections"))
    return {d.get("title"): d.get("key") for d in root.findall(".//Directory")}


def create_section(base: str, title: str, path: str, kind: str, agent: str, scanner: str) -> None:
    query = urllib.parse.urlencode({
        "name": title, "type": kind, "agent": agent,
        "scanner": scanner, "language": "en-US", "location": path,
    })
    _request(f"{base}/library/sections?{query}", method="POST")
    log.info("created %-16s %-6s -> %s", title, kind, path)


def refresh(base: str, key: str) -> None:
    _request(f"{base}/library/sections/{key}/refresh")


def _wait_for_scans(base: str, timeout: int = 900) -> None:
    """Block until no section reports itself as refreshing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        root = ET.fromstring(_request(f"{base}/library/sections"))
        if not any(d.get("refreshing") == "1" for d in root.findall(".//Directory")):
            return
        time.sleep(10)
    log.warning("sections still refreshing after %ds; counts below may be incomplete", timeout)


def section_counts(base: str) -> dict[str, int]:
    """Return {title: playable item count} using the leaf type for each library kind."""
    leaf = {"show": 4, "movie": 1, "artist": 10}
    counts: dict[str, int] = {}
    for directory in ET.fromstring(_request(f"{base}/library/sections")).findall(".//Directory"):
        kind = directory.get("type")
        if kind not in leaf:
            continue
        body = _request(f"{base}/library/sections/{directory.get('key')}/all?type={leaf[kind]}")
        counts[directory.get("title")] = int(ET.fromstring(body).get("size") or 0)
    return counts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Set up Plex libraries for the test media library.")
    p.add_argument("--plex", default=DEFAULT_PLEX, help="Plex base URL.")
    p.add_argument("--claim", default="", metavar="TOKEN",
                   help="Claim token from https://plex.tv/claim, to use the server from Plex apps.")
    p.add_argument("--no-scan", action="store_true", help="Create the libraries but do not scan.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        wait_for_server(ns.plex)
    except RuntimeError as e:
        log.error("%s — is the stack up? try 'make servers'", e)
        return 2

    if ns.claim:
        if is_claimed(ns.plex):
            log.info("already claimed, ignoring the supplied token")
        else:
            try:
                claim(ns.plex, ns.claim)
            except RuntimeError as e:
                log.error("claim failed (tokens expire after ~4 minutes): %s", e)
                return 2
    elif not is_claimed(ns.plex):
        log.info("server is unclaimed: reachable over the local API, but Plex apps will not "
                 "find it. Pass --claim with a token from https://plex.tv/claim to change that.")

    present = existing_sections(ns.plex)
    for title, path, kind, agent, scanner in LIBRARIES:
        if title in present:
            log.info("exists   %-16s", title)
            continue
        try:
            create_section(ns.plex, title, path, kind, agent, scanner)
        except RuntimeError as e:
            log.error("could not create %s: %s", title, e)

    if not ns.no_scan:
        # A refresh issued in the same breath as the create is silently dropped: the
        # section reports scannedAt unset and stays empty. Give the server a moment,
        # then refresh and wait for every section to go idle.
        time.sleep(5)
        for title, key in existing_sections(ns.plex).items():
            if title.startswith("Testlib"):
                refresh(ns.plex, key)
                log.info("scanning %s", title)
        _wait_for_scans(ns.plex)

    sys.stdout.write("\nlibraries:\n")
    counts = section_counts(ns.plex) if not ns.no_scan else {}
    for title, key in sorted(existing_sections(ns.plex).items()):
        suffix = f"  {counts[title]} playable items" if title in counts else ""
        sys.stdout.write(f"  {title} (section {key}){suffix}\n")
    sys.stdout.write(f"\nweb: {ns.plex}/web\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
