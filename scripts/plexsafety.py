"""Refuse to write to a Plex server that is not this project's test container.

The tools here default to a fixed localhost port, and a port is not proof of which
server answers. A native Plex install binds both 32400 and 32401, so an earlier
default silently pointed at a real library. Writing `testlib-uuid:` labels or
creating sections there is not undone by a rescan.

Identity comes from two signals, in order:

1. A pinned machine identifier in `docker/.testlib-server-id`, written by
   `plexsetup` once it has built the libraries. This is the reliable signal and it
   keeps working after the container is claimed to a Plex account.
2. Failing a pin, `claimed="0"`. An unclaimed server owns no real library, so it is
   safe by construction. This covers a first run, before any pin exists.

A claimed server with no matching pin is refused.
"""
from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger("plexsafety")

PIN_PATH = Path(__file__).resolve().parent.parent / "docker" / ".testlib-server-id"


ENV_PATH = Path(__file__).resolve().parent.parent / "docker" / ".env"


def plex_token() -> str | None:
    """Return the Plex auth token, from the environment or `docker/.env`.

    A claimed server rejects an unauthenticated request, which is the point: it is
    what lets `ALLOWED_NETWORKS` stay empty instead of granting the whole LAN
    unauthenticated access to the API. Returns None when no token is configured, so
    an unclaimed server still works with no setup.
    """
    token = os.environ.get("PLEX_TOKEN")
    if token:
        return token.strip() or None
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PLEX_TOKEN=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"\'') or None
    except OSError:
        pass
    return None


class UnsafePlexTarget(RuntimeError):
    """Raised when a write was aimed at a server that is not the test container."""


def server_identity(base: str, token: str | None = None, timeout: int = 20) -> dict:
    """Return identifying fields for a Plex server, or raise if it cannot be read."""
    headers = {"Accept": "application/xml"}
    token = token or plex_token()
    if token:
        headers["X-Plex-Token"] = token
    request = urllib.request.Request(f"{base}/identity", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            root = ET.fromstring(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ET.ParseError) as e:
        raise UnsafePlexTarget(
            f"cannot read the identity of {base}: {e}. "
            f"Start the test container with 'make servers' and retry.") from e
    return {
        "machine_identifier": root.get("machineIdentifier") or "",
        "claimed": root.get("claimed") or "",
        "version": root.get("version") or "",
    }


def read_pin(path: Path = PIN_PATH) -> str | None:
    """Return the pinned test-server machine identifier, or None when unset."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_pin(machine_identifier: str, path: Path = PIN_PATH) -> None:
    """Record which server is this project's test container."""
    if not machine_identifier:
        raise ValueError("refusing to pin an empty machine identifier")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(machine_identifier + "\n", encoding="utf-8")
    log.info("pinned test server %s in %s", machine_identifier[:12], path)


def save_token(token: str, path: Path = ENV_PATH) -> None:
    """Store a Plex token in docker/.env, replacing any previous value.

    The file is gitignored. The token is never logged: it authenticates every API
    call this project makes and is equivalent to the account on that server.
    """
    token = token.strip()
    if not token:
        raise ValueError("refusing to store an empty token")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith("PLEX_TOKEN=")]
    lines.append(f"PLEX_TOKEN={token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("stored PLEX_TOKEN in %s (gitignored)", path)


def is_test_server(identity: dict, pin: str | None = None,
                   *, unclaimed_is_enough: bool = False) -> bool:
    """Return True when the identity is the pinned container.

    With `unclaimed_is_enough`, any unclaimed server passes. That is for the tool
    that establishes the pin: recreating the container gives it a new machine
    identifier, and a stale pin must not lock the project out of its own server.
    Tools that only read and label keep the strict rule.
    """
    if pin and identity.get("machine_identifier") == pin:
        return True
    # Only an explicit "0" counts. An absent attribute is unknown, not safe:
    # treating it as unclaimed would pass a real server that simply does not
    # report the field.
    unclaimed = identity.get("claimed") == "0"
    if pin and not unclaimed_is_enough:
        # A pin exists and this is not it. Being unclaimed is not enough: some
        # other disposable server is not the one holding this project's data.
        return False
    return unclaimed


def require_test_server(base: str, token: str | None = None,
                        *, allow_claimed: bool = False,
                        unclaimed_is_enough: bool = False,
                        pin_path: Path = PIN_PATH) -> dict:
    """Verify `base` is this project's test container before a write.

    Returns the identity. Set `allow_claimed` only when the person has explicitly
    chosen to write to a server this check rejects.
    """
    identity = server_identity(base, token)
    pin = read_pin(pin_path)
    if is_test_server(identity, pin, unclaimed_is_enough=unclaimed_is_enough):
        return identity
    if allow_claimed:
        log.warning("writing to %s (%s, claimed=%s) because --allow-claimed-server was given",
                    base, identity["machine_identifier"][:12], identity["claimed"])
        return identity

    if pin:
        detail = (f"its machineIdentifier {identity['machine_identifier'][:12]}... does not match "
                  f"the pinned test server {pin[:12]}...")
    else:
        detail = (f"it reports claimed=\"{identity['claimed']}\", so it is a real Plex server, "
                  f"and no test server is pinned yet")
    raise UnsafePlexTarget(
        f"refusing to write to {base}: {detail}. "
        f"Run 'python3 -m scripts.plexsetup' against the test container to pin it, "
        f"or pass --allow-claimed-server to override.")
