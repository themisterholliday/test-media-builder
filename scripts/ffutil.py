"""FFmpeg / ffprobe subprocess helpers with timeout, logging, and structured errors."""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger("ffutil")

FFMPEG_TIMEOUT_DEFAULT = 600  # 10 minutes per invocation

DEFAULT_FFMPEG = "ffmpeg"
DEFAULT_FFPROBE = "ffprobe"

# Matches a filter row in `ffmpeg -filters`: flags, name, then an "A->A" / "V->V" column.
# The arrow column is what separates real filter rows from the legend above them.
_FILTER_ROW = re.compile(r"^\s*[A-Z.]{1,3}\s+(\S+)\s+\S+->\S+")


class FFBinaryMissing(RuntimeError):
    """Raised when ffmpeg or ffprobe is not on PATH."""


class FFCapabilityMissing(RuntimeError):
    """Raised when the configured ffmpeg build lacks a filter the pipeline needs."""


class FFExecError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""

    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"ffmpeg/ffprobe failed (rc={returncode})\n"
            f"cmd: {' '.join(self.cmd)}\n"
            f"stderr tail:\n{stderr[-2000:]}"
        )


@dataclass
class FFResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str


# Set from the matrix config at startup. Environment variables still win, so a
# one-off run can point at a different build without editing the config.
_CONFIGURED: dict[str, str] = {}
# Paths already warned about, so the fallback notice appears once per run.
_WARNED_BINARIES: set[str] = set()


def configure_binaries(ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
    """Record the ffmpeg/ffprobe paths declared in the matrix config.

    Precedence is environment override, then this config value, then bare PATH lookup.
    """
    if ffmpeg:
        _CONFIGURED["ffmpeg"] = ffmpeg
    if ffprobe:
        _CONFIGURED["ffprobe"] = ffprobe


def _pick_binary(env_var: str, configured_key: str, default: str) -> str:
    """Choose a binary: environment override, then matrix config, then PATH.

    The matrix ships an absolute path to a specific ffmpeg build. That path is
    correct on the machine that wrote it and absent everywhere else, so an
    unusable configured path falls through to PATH instead of failing the run.
    A machine-specific value in shared config must not stop anyone else building.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    configured = _CONFIGURED.get(configured_key)
    if configured:
        if resolve_binary(configured):
            return configured
        if configured not in _WARNED_BINARIES:
            _WARNED_BINARIES.add(configured)
            log.warning("configured %s is not executable here (%s); falling back to %r on PATH",
                        configured_key, configured, default)
    return default


def ffmpeg_bin() -> str:
    """Return the ffmpeg command to use. FFMPEG_BIN wins over the configured path."""
    return _pick_binary("FFMPEG_BIN", "ffmpeg", DEFAULT_FFMPEG)


def ffprobe_bin() -> str:
    """Return the ffprobe command to use. FFPROBE_BIN wins over the configured path."""
    return _pick_binary("FFPROBE_BIN", "ffprobe", DEFAULT_FFPROBE)


def resolve_binary(name: str) -> str | None:
    """Return the absolute path of an executable, or None if it cannot be run.

    Accepts a bare command name to look up on PATH, or an explicit path to a binary.
    """
    if os.sep in name:
        candidate = Path(name).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which(name)


def require_binaries() -> None:
    """Verify ffmpeg and ffprobe can be executed. Raises FFBinaryMissing otherwise."""
    for name in (ffmpeg_bin(), ffprobe_bin()):
        if resolve_binary(name) is None:
            raise FFBinaryMissing(
                f"required binary not found or not executable: {name}. "
                "Install ffmpeg, or set FFMPEG_BIN / FFPROBE_BIN to an explicit path."
            )


def available_filters(binary: str | None = None) -> frozenset[str]:
    """Return the filter names the configured ffmpeg build provides.

    Cached per binary, so switching FFMPEG_BIN mid-process re-reads the new build.
    """
    return _filters_for(binary or ffmpeg_bin())


@functools.lru_cache(maxsize=8)
def _filters_for(binary: str) -> frozenset[str]:
    """Read and parse `ffmpeg -filters` for one binary.

    An unreadable filter list yields an empty set, which makes require_filters
    report every requested filter as missing rather than silently passing.
    """
    cmd = [binary, "-hide_banner", "-filters"]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("could not list filters for %s", cmd[0])
        return frozenset()
    if proc.returncode != 0:
        log.warning("filter listing failed for %s (rc=%d)", cmd[0], proc.returncode)
        return frozenset()
    names = {m.group(1) for m in (_FILTER_ROW.match(line) for line in proc.stdout.splitlines()) if m}
    return frozenset(names)


def require_filters(*names: str) -> None:
    """Verify the ffmpeg build provides each named filter. Raises FFCapabilityMissing otherwise.

    Fails before any encode starts, so a build missing drawtext reports one clear
    message instead of failing part-way through the matrix.
    """
    have = available_filters()
    missing = [n for n in names if n not in have]
    if not missing:
        return
    raise FFCapabilityMissing(
        f"ffmpeg build at '{resolve_binary(ffmpeg_bin()) or ffmpeg_bin()}' lacks required "
        f"filter(s): {', '.join(missing)}.\n"
        "The stock Homebrew 'ffmpeg' formula omits libfreetype/libass. Install a fuller build:\n"
        "    brew install ffmpeg-full\n"
        "then point this tool at it:\n"
        "    export FFMPEG_BIN=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg\n"
        "    export FFPROBE_BIN=/opt/homebrew/opt/ffmpeg-full/bin/ffprobe\n"
        "Or disable the text overlay with --no-burn-in."
    )


def run_ffmpeg(args: Sequence[str], *, timeout: int = FFMPEG_TIMEOUT_DEFAULT, check: bool = True) -> FFResult:
    """Run ffmpeg with the given args (without the leading 'ffmpeg').

    Always uses -y (overwrite) and -hide_banner for deterministic, quiet output.
    Enforces a timeout and raises FFExecError on non-zero exit when check=True.
    Converts a timeout into FFExecError so callers handle every failure uniformly.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-y", *args]
    log.debug("exec: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        partial_stderr = (e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        raise FFExecError(
            cmd,
            returncode=-1,
            stderr=f"ffmpeg timed out after {timeout}s\n{partial_stderr}",
        ) from e
    if check and proc.returncode != 0:
        raise FFExecError(cmd, proc.returncode, proc.stderr)
    return FFResult(cmd=cmd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def bit_depth_for(stream: dict) -> str | None:
    """Return a video stream's bit depth, falling back to the pixel format.

    ffprobe fills in bits_per_raw_sample for some codecs but leaves it absent for
    others — HEVC Main 10 among them — so a 10-bit file can report no depth at all.
    The pixel format always encodes the depth, so parse that when the field is missing.
    """
    raw = stream.get("bits_per_raw_sample")
    if raw not in (None, "", "0", 0):
        return str(raw)
    pix_fmt = stream.get("pix_fmt") or ""
    match = re.search(r"(\d{1,2})(?:le|be)$", pix_fmt)
    if match:
        return match.group(1)
    return "8" if pix_fmt else None


def ffprobe(path: Path, *, timeout: int = 60) -> dict:
    """Run ffprobe on a file and return parsed JSON with format + streams."""
    if not path.exists():
        raise FileNotFoundError(f"cannot probe missing file: {path}")
    cmd = [
        ffprobe_bin(),
        "-hide_banner",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise FFExecError(cmd, proc.returncode, proc.stderr)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise FFExecError(cmd, proc.returncode, f"invalid probe JSON: {e}") from e
