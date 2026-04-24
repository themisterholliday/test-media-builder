"""FFmpeg / ffprobe subprocess helpers with timeout, logging, and structured errors."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger("ffutil")

FFMPEG_TIMEOUT_DEFAULT = 600  # 10 minutes per invocation


class FFBinaryMissing(RuntimeError):
    """Raised when ffmpeg or ffprobe is not on PATH."""


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


def require_binaries() -> None:
    """Verify ffmpeg and ffprobe are available on PATH. Raises FFBinaryMissing otherwise."""
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name) is None:
            raise FFBinaryMissing(f"required binary not found on PATH: {name}")


def run_ffmpeg(args: Sequence[str], *, timeout: int = FFMPEG_TIMEOUT_DEFAULT, check: bool = True) -> FFResult:
    """Run ffmpeg with the given args (without the leading 'ffmpeg').

    Always uses -y (overwrite) and -hide_banner for deterministic, quiet output.
    Enforces a timeout and raises FFExecError on non-zero exit when check=True.
    Converts a timeout into FFExecError so callers handle every failure uniformly.
    """
    cmd = ["ffmpeg", "-hide_banner", "-y", *args]
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


def ffprobe(path: Path, *, timeout: int = 60) -> dict:
    """Run ffprobe on a file and return parsed JSON with format + streams."""
    if not path.exists():
        raise FileNotFoundError(f"cannot probe missing file: {path}")
    cmd = [
        "ffprobe",
        "-hide_banner",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
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
