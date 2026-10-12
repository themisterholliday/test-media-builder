"""An HDHomeRun-compatible tuner that serves the generated library as live channels.

Plex has two ways to find a tuner. It broadcasts an HDHomeRun discovery request on
UDP/65001 and waits for a reply, or it is told an address directly through
`POST /media/grabbers/devices?uri=...`. Both are supported here; the direct route is
the reliable one, because a broadcast does not cross Docker's bridge network.

Two details are easy to get wrong and fail silently:

- The device id must pass libhdhomerun's checksum. An invalid id is dropped without
  a message, and nothing downstream ever runs.
- Registration uses `/media/grabbers/devices`, not `/media/grabbers/devices/discover`.
  The discover endpoint ignores its `uri` parameter.

Channels stream as MPEG-TS, looping, which is what a live tuner looks like.
"""
from __future__ import annotations

import binascii
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.environ.get("TUNER_HOST", "127.0.0.1")
PORT = int(os.environ.get("TUNER_PORT", "80"))
MEDIA_DIR = Path(os.environ.get("TUNER_MEDIA", "/media/flat"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
BASE = f"http://{HOST}:{PORT}"

# Must satisfy hdhomerun_discover_validate_device_id(); an invalid id is silently
# rejected by Plex's tuner service.
DEVICE_ID = 0x1010001F

TYPE_DISCOVER_REQ, TYPE_DISCOVER_RPY = 0x0002, 0x0003
TAG_DEVICE_TYPE, TAG_DEVICE_ID, TAG_TUNER_COUNT = 0x01, 0x02, 0x10
TAG_LINEUP_URL, TAG_BASE_URL = 0x27, 0x2A
DEVICE_TYPE_TUNER = 0x00000001
CHANNEL_LIMIT = int(os.environ.get("TUNER_CHANNELS", "6"))


def load_channels() -> list[dict]:
    """Build the channel list from the generated library."""
    files = sorted(p for p in MEDIA_DIR.glob("*.mp4") if p.is_file())[:CHANNEL_LIMIT]
    return [{"GuideNumber": str(i), "GuideName": p.stem.split(" [")[0][:28],
             "_file": str(p)}
            for i, p in enumerate(files, start=1)]


CHANNELS = load_channels()


def _tlv(tag: int, value: bytes) -> bytes:
    """Encode one HDHomeRun tag-length-value field."""
    return bytes([tag, len(value)]) + value


def discovery_reply() -> bytes:
    """Build the reply that tells Plex this device exists and where its JSON lives."""
    payload = (_tlv(TAG_DEVICE_TYPE, struct.pack(">I", DEVICE_TYPE_TUNER))
               + _tlv(TAG_DEVICE_ID, struct.pack(">I", DEVICE_ID))
               + _tlv(TAG_TUNER_COUNT, bytes([max(len(CHANNELS), 1)]))
               + _tlv(TAG_BASE_URL, BASE.encode())
               + _tlv(TAG_LINEUP_URL, f"{BASE}/lineup.json".encode()))
    frame = struct.pack(">HH", TYPE_DISCOVER_RPY, len(payload)) + payload
    return frame + struct.pack("<I", binascii.crc32(frame) & 0xFFFFFFFF)


def serve_discovery() -> None:
    """Answer discovery broadcasts for the life of the process."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 65001))
    reply = discovery_reply()
    while True:
        data, addr = sock.recvfrom(2048)
        if len(data) >= 4 and struct.unpack(">H", data[:2])[0] == TYPE_DISCOVER_REQ:
            for target in {addr, (addr[0], 65001)}:
                try:
                    sock.sendto(reply, target)
                except OSError:
                    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}", flush=True)

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/discover.json", "/device.json"):
            return self._json({
                "FriendlyName": "HDHomeRun TESTLIB", "Manufacturer": "Silicondust",
                "ModelNumber": "HDHR4-2US", "FirmwareName": "hdhomerun4_atsc",
                "FirmwareVersion": "20200907", "DeviceID": f"{DEVICE_ID:08X}",
                "DeviceAuth": "testlibtestlib00", "BaseURL": BASE,
                "LineupURL": f"{BASE}/lineup.json",
                "TunerCount": max(len(CHANNELS), 1)})
        if path == "/lineup_status.json":
            return self._json({"ScanInProgress": 0, "ScanPossible": 1,
                               "Source": "Antenna", "SourceList": ["Antenna"]})
        if path == "/lineup.json":
            return self._json([{"GuideNumber": c["GuideNumber"],
                                "GuideName": c["GuideName"],
                                "URL": f"{BASE}/auto/v{c['GuideNumber']}"}
                               for c in CHANNELS])
        if path.startswith("/auto/v"):
            return self._stream(path.rsplit("v", 1)[-1])
        self.send_error(404)

    def do_POST(self):
        # Plex posts here to start a channel scan. There is nothing to scan: the
        # lineup is fixed, so report completion immediately.
        if urlparse(self.path).path == "/lineup.post":
            return self._json({"ScanInProgress": 0})
        self.send_error(404)

    def _stream(self, number: str) -> None:
        """Pipe one channel as looping MPEG-TS, which is what a tuner delivers."""
        channel = next((c for c in CHANNELS if c["GuideNumber"] == number), None)
        if channel is None:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1",
             "-i", channel["_file"], "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", "-f", "mpegts", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(188 * 64)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()


if __name__ == "__main__":
    if not CHANNELS:
        print(f"  no .mp4 files under {MEDIA_DIR}; run 'make library' first", flush=True)
        sys.exit(1)
    threading.Thread(target=serve_discovery, daemon=True).start()
    print(f"  tuner on {BASE}, {len(CHANNELS)} channels:", flush=True)
    for c in CHANNELS:
        print(f"     {c['GuideNumber']}  {c['GuideName']}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
