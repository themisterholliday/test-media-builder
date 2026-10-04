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
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .plexsafety import (UnsafePlexTarget, plex_token, read_pin, require_test_server, save_token,
                         server_identity, write_pin)

log = logging.getLogger("plexsetup")

DOCKER = os.environ.get("DOCKER", "/Applications/Docker.app/Contents/Resources/bin/docker")
DEFAULT_PLEX = "http://localhost:32402"
# Deliberately unmistakable: a claimed test server sits in the account's server
# list next to the person's real one.
DEFAULT_SERVER_NAME = "TESTLIB (disposable test library)"

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
    """Call the Plex API, authenticating when a token is configured.

    Every request in this module goes through here, so a claimed server works
    without granting the LAN unauthenticated access through ALLOWED_NETWORKS.
    """
    token = plex_token()
    headers = {"X-Plex-Token": token} if token else {}
    request = urllib.request.Request(url, method=method, headers=headers)
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


def host_lan_address() -> str | None:
    """Return this machine's LAN IP, or None when it cannot be determined.

    Opens a UDP socket to a routable address and reads back the local end. No
    packet is sent; it just asks the routing table which interface would be used,
    which beats parsing `ifconfig` across platforms.
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return address if not address.startswith("127.") else None


def advertise_lan_address(base: str) -> None:
    """Tell Plex which address to advertise to apps.

    Inside a container Plex only sees the Docker NAT address, so it advertises
    something like `172.22.0.2:32400` that no device on the LAN can reach, and apps
    show the server as offline. `customConnections` adds the host address and the
    published port alongside it. The bogus NAT entry remains but fails immediately,
    so clients fall through to this one.
    """
    address = host_lan_address()
    if not address:
        log.warning("could not determine a LAN address; apps may show the server offline")
        return
    port = urllib.parse.urlsplit(base).port or 32400
    uri = f"http://{address}:{port}"
    try:
        current = ET.fromstring(_request(f"{base}/:/prefs"))
    except RuntimeError as e:
        log.warning("could not read prefs to set the advertised address: %s", e)
        return
    existing = next((x.get("value") for x in current.findall(".//Setting")
                     if x.get("id") == "customConnections"), "")
    if existing == uri:
        log.info("advertised address already %s", uri)
        return
    try:
        _request(f"{base}/:/prefs?{urllib.parse.urlencode({'customConnections': uri})}",
                 method="PUT")
        log.info("advertising %s to Plex apps", uri)
    except RuntimeError as e:
        log.warning("could not set the advertised address: %s", e)


def save_server_token(base: str) -> int:
    """Copy the claimed server's own token into docker/.env.

    A claimed server rejects unauthenticated calls, which is what allows
    ALLOWED_NETWORKS to stay empty instead of opening the API to the whole LAN.
    """
    token = ET.fromstring(_request(f"{base}/")).get("machineIdentifier")
    import subprocess
    pref = ("/config/Library/Application Support/Plex Media Server/Preferences.xml")
    try:
        out = subprocess.run(
            [DOCKER, "exec", "testlib-plex", "sh", "-c", f'cat "{pref}"'],
            capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.error("could not read the server config: %s", e)
        return 2
    m = re.search(r'PlexOnlineToken="([^"]+)"', out)
    if not m:
        log.error("no PlexOnlineToken in the server config -- claim it first: "
                  "make plex-claim TOKEN=claim-xxxx")
        return 2
    save_token(m.group(1))
    return 0


def report_status(base: str) -> int:
    """Print which server answers and whether it is this project's test container."""
    try:
        identity = server_identity(base)
    except UnsafePlexTarget as e:
        print(f"  {e}")
        return 2
    root = ET.fromstring(_request(f"{base}/"))
    pin = read_pin()
    machine = identity["machine_identifier"]
    claimed = identity["claimed"] == "1"

    print(f"  url        : {base}")
    print(f"  name       : {root.get('friendlyName')}")
    print(f"  machine id : {machine[:12]}...")
    print(f"  claimed    : {'yes' if claimed else 'no'}"
          f"{'' if claimed else '   (run: make plex-claim TOKEN=claim-xxxx)'}")
    print(f"  plex pass  : {'yes' if root.get('myPlexSubscription') == '1' else 'no'}")
    if pin and machine == pin:
        print("  pinned     : yes — writes to this server are allowed")
    elif pin:
        print(f"  pinned     : NO — pin is {pin[:12]}..., writes here are refused")
    else:
        print("  pinned     : not set — run 'make plex-setup' to pin this server")
    sections = ET.fromstring(_request(f"{base}/library/sections")).findall(".//Directory")
    print(f"  libraries  : {len(sections)}")
    for d in sections:
        print(f"      [{d.get('key')}] {d.get('title')}")
    return 0


def set_friendly_name(base: str, name: str) -> None:
    """Name the server so it cannot be mistaken for a real one in a Plex account.

    Once the container is claimed it appears in the account's server list beside
    the person's own server. A blunt name is the cheapest way to keep them apart.
    """
    try:
        _request(f"{base}/:/prefs?{urllib.parse.urlencode({'FriendlyName': name})}", method="PUT")
        log.info("server name set to %r", name)
    except RuntimeError as e:
        log.warning("could not set the server name: %s", e)


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
    p.add_argument("--allow-claimed-server", action="store_true",
                   help="Permit library creation on a claimed (real) Plex server.")
    p.add_argument("--name", default=DEFAULT_SERVER_NAME,
                   help="Friendly name for the test server.")
    p.add_argument("--save-token", action="store_true",
                   help="Read the server's own token from its config into docker/.env.")
    p.add_argument("--status", action="store_true",
                   help="Report the target server and exit without changing anything.")
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

    if ns.save_token:
        return save_server_token(ns.plex)

    if ns.status:
        return report_status(ns.plex)

    # Creating sections on the wrong server leaves libraries a person must delete by
    # hand, so confirm the target is the disposable container first. A claim is a
    # deliberate act and stays exempt.
    if not ns.claim:
        try:
            # plexsetup writes the pin, so a stale pin must not lock it out of a
            # freshly recreated container. An unclaimed server is accepted here.
            require_test_server(ns.plex, allow_claimed=ns.allow_claimed_server,
                                unclaimed_is_enough=True)
        except UnsafePlexTarget as e:
            log.error("%s", e)
            return 2

    # Pin this server as the test container before anything is created on it. The
    # pin is what lets the write guard keep working after the server is claimed,
    # when `claimed="0"` no longer distinguishes it from a real library.
    identity = server_identity(ns.plex)
    write_pin(identity["machine_identifier"])
    set_friendly_name(ns.plex, ns.name)
    advertise_lan_address(ns.plex)

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
