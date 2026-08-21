"""Extract a t=5s thumbnail per video and composite a contact sheet.

Walks every plan that has a video stream, writes an individual JPEG into
generated/_thumbs/, then tiles them into generated/_thumbs/contact_sheet.jpg.
Purely visual aid — catches HDR tonemapping, wrong color range, flipped
orientation, and other things ffprobe can't see.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

from .ffutil import FFExecError, run_ffmpeg
from .generate import (
    CONFIG_PATH,
    GENERATED_DIR,
    SECTIONS,
    _all_plans,
    _filter_plans,
    _load_config,
    _probe_target,
)

log = logging.getLogger("thumbnails")

THUMB_DIR = GENERATED_DIR / "_thumbs"
THUMB_WIDTH = 320
CONTACT_COLS = 4


def _is_video_plan(plan) -> bool:
    if plan.section == "audio":
        return False
    if plan.kind == "subtitle_external":
        return False
    flags = set(plan.variant.get("flags", []))
    if "audio_only" in flags:
        return False
    return True


def _thumb_path_for(plan) -> Path:
    return THUMB_DIR / f"{plan.variant['name']}.jpg"


def _extract_thumb(plan, at: float = 5.0) -> tuple[bool, str]:
    target = _probe_target(plan)
    if not target.exists():
        return False, f"missing: {target}"
    out_path = _thumb_path_for(plan)
    # Correct for non-square pixels first. Without this an anamorphic file (DAR 16:9,
    # SAR 64:45) thumbnails as 320x256 rather than 320x180 — the contact sheet would
    # misrepresent exactly the variants that exist to test geometry.
    vf = f"scale=iw*sar:ih,scale={THUMB_WIDTH}:-1:flags=lanczos,setsar=1"

    def _try(seek: float, extra_vf: str = "") -> bool:
        full_vf = f"{extra_vf},{vf}" if extra_vf else vf
        args = [
            "-ss", str(seek),
            "-i", str(target),
            "-frames:v", "1",
            "-vf", full_vf,
            "-qscale:v", "3",
            str(out_path),
        ]
        try:
            run_ffmpeg(args, timeout=60)
        except FFExecError:
            return False
        return out_path.exists() and out_path.stat().st_size > 0

    # First attempt at requested seek. For HDR-tagged content, also apply a colorspace-aware
    # conversion to BT.709 so the thumbnail isn't washed out. zscale is preferred when available.
    is_hdr = bool((plan.variant.get("video") or {}).get("hdr")) or ("hdr" in set(plan.variant.get("flags", [])))
    if is_hdr:
        hdr_vf = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
        if _try(at, hdr_vf):
            return True, ""
    if _try(at):
        return True, ""
    # Fallback for truncated/broken files: take frame 0.
    if _try(0):
        return True, "used t=0 fallback"
    return False, "all seek attempts failed"


def tile_images(images: list[Path], out_path: Path, *, cols: int = CONTACT_COLS,
                cell_width: int = THUMB_WIDTH, timeout: int = 180) -> None:
    """Tile images into a single sheet, padding each to a uniform cell.

    Each image is opened as its own input and padded before tiling. The concat
    demuxer cannot be used: thumbnails keep their source aspect, so heights differ
    (16:9, 4:3, 9:16), and concat silently drops every entry after the first size
    change — which produced a half-empty sheet.
    """
    if not images:
        return
    rows = math.ceil(len(images) / cols)
    cell_h = round(cell_width * 9 / 16)

    args: list[str] = []
    for image in images:
        args += ["-i", str(image)]
    chains = [
        f"[{i}:v]scale={cell_width}:{cell_h}:force_original_aspect_ratio=decrease,"
        f"pad={cell_width}:{cell_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[t{i}]"
        for i in range(len(images))
    ]
    labels = "".join(f"[t{i}]" for i in range(len(images)))
    chains.append(f"{labels}concat=n={len(images)}:v=1:a=0[cat]")
    chains.append(f"[cat]tile={cols}x{rows}:margin=4:padding=4:color=black[out]")

    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[out]",
        "-frames:v", "1",
        "-qscale:v", "3",
        str(out_path),
    ]
    run_ffmpeg(args, timeout=timeout)


def _contact_sheet(thumbs: list[Path], out_path: Path) -> None:
    """Tile every variant thumbnail into one contact sheet."""
    tile_images(thumbs, out_path)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Thumbnail + contact sheet for generated videos.")
    p.add_argument("--only", default="", help=f"Comma-separated sections. Valid: {','.join(SECTIONS)}")
    p.add_argument("--variant", default="")
    p.add_argument("--at", type=float, default=5.0, help="Seek time in seconds for thumbnail.")
    p.add_argument("--no-contact-sheet", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = _load_config(CONFIG_PATH)
    plans = _all_plans(cfg)
    sections = {s.strip() for s in ns.only.split(",") if s.strip()} or None
    variant_names = {s.strip() for s in ns.variant.split(",") if s.strip()} or None
    plans = _filter_plans(plans, sections=sections, variant_names=variant_names)
    plans = [p for p in plans if _is_video_plan(p)]

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []
    for plan in plans:
        ok, err = _extract_thumb(plan, at=ns.at)
        if ok:
            log.info("thumb: %s", plan.variant["name"])
            produced.append(_thumb_path_for(plan))
        else:
            log.warning("skip %s: %s", plan.variant["name"], err)

    if produced and not ns.no_contact_sheet:
        sheet = THUMB_DIR / "contact_sheet.jpg"
        try:
            _contact_sheet(produced, sheet)
            log.info("contact sheet: %s (%d tiles)", sheet, len(produced))
        except FFExecError as e:
            log.error("contact sheet failed: %s", e)

    sys.stdout.write(f"wrote {len(produced)} thumbnails to {THUMB_DIR}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
