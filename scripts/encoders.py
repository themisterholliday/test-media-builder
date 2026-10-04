"""Build ffmpeg argument lists for each matrix variant.

Each builder is a pure function: (variant_config, defaults, sources_dir,
output_path) -> list[str] of ffmpeg args (excluding the leading 'ffmpeg'
binary — ffutil.run_ffmpeg prepends it).
"""
from __future__ import annotations

import logging
from pathlib import Path

from .ids import short
from .overlay import is_enabled as overlay_enabled, variant_overlay_filters

log = logging.getLogger("encoders")

# Encoder name -> the codec name ffprobe reports for it. Used for the contract
# check in generate.py and for the codec label drawn on screen.
_CODEC_FAMILIES: dict[str, str] = {
    "libx264": "h264",
    "libx265": "hevc",
    "libaom-av1": "av1",
    "libsvtav1": "av1",
    "librav1e": "av1",
    "libvpx-vp9": "vp9",
    "libopus": "opus",
    "libmp3lame": "mp3",
    "mpeg2video": "mpeg2video",
    "mpeg4": "mpeg4",
    "wmv1": "wmv1",
    "wmv2": "wmv2",
}

# Per-codec capability map. Governs which flags _common_video_args emits so
# we don't pass x264-style options to codecs that reject them.
CODEC_CAPABILITIES: dict[str, set[str]] = {
    "libx264":       {"preset", "profile", "level", "vbv", "keyint_min"},
    "libx265":       {"preset", "profile", "level", "vbv", "keyint_min"},
    "libsvtav1":     {"preset"},
    "libvpx-vp9":    {"vbv", "keyint_min"},
    "libaom-av1":    {"vbv", "keyint_min"},
    "mpeg2video":    {"vbv"},
    "mpeg4":         set(),
    "wmv1":          set(),
    "wmv2":          set(),
}

# Default for unlisted codecs — behave like a modern encoder (x264-style).
_DEFAULT_CAPS = {"preset", "profile", "level", "vbv", "keyint_min"}


def _codec_caps(codec: str) -> set[str]:
    return CODEC_CAPABILITIES.get(codec, _DEFAULT_CAPS)


def effective_duration(variant: dict | None, defaults: dict) -> int:
    """Return the output length for a variant in seconds.

    A variant may declare its own `duration_seconds` — a geometry or chroma test does
    not need the full library duration. Falls back to the global default.
    """
    if variant:
        own = variant.get("duration_seconds")
        if own is not None:
            return int(own)
    return int(defaults["duration_seconds"])


def _duration_flag(defaults: dict, variant: dict | None = None) -> list[str]:
    return ["-t", str(effective_duration(variant, defaults))]


def _scale_filter(width: int, height: int) -> str:
    return f"scale={width}:{height}:flags=lanczos"


def codec_family(codec: str) -> str:
    """Map an ffmpeg encoder name to the codec family ffprobe reports."""
    return _CODEC_FAMILIES.get(codec, codec)


def overlay_lines_for(variant: dict, *, width: int, height: int, framerate) -> list[str]:
    """Compose the static per-file text shown on screen.

    Identifies the file from a single screenshot: name on the first line, then the
    structural facts a playback bug most often turns on.
    """
    video = variant.get("video") or {}
    fps = framerate if framerate is not None else ""
    if isinstance(fps, float) and fps.is_integer():
        fps = int(fps)
    parts = [
        codec_family(video.get("codec", "")) or "?",
        variant.get("container", "?"),
        f"{width}x{height}@{fps}",
    ]

    # Multi-track variants declare `audio_tracks` instead of a single `audio` block.
    audio = variant.get("audio") or {}
    audio_tracks = variant.get("audio_tracks") or []
    if audio:
        parts.append(f"{audio.get('codec', '?')} {audio.get('channels', '?')}ch")
    elif audio_tracks:
        codecs = ",".join(dict.fromkeys(t.get("codec", "?") for t in audio_tracks))
        parts.append(f"{len(audio_tracks)} audio ({codecs})")
    else:
        parts.append("no audio")

    subtitle_tracks = variant.get("subtitle_tracks") or []
    if subtitle_tracks:
        parts.append(f"{len(subtitle_tracks)} subs")
    elif variant.get("embed") and variant.get("format"):
        parts.append(f"sub {variant['format']}")

    return [variant.get("name", "?"), " / ".join(parts)]


def _build_vf_chain(
    video: dict,
    width: int,
    height: int,
    overlay_filters: list[str] | None = None,
) -> str:
    """Compose the -vf chain: scale, then overlay, then interlace and SAR.

    The overlay is drawn before interlacing so it is not combed, and before setsar so
    it is not stretched by an anamorphic display aspect.
    """
    parts: list[str] = [_scale_filter(width, height)]
    # Pad the picture out to a larger frame, baking in letterbox or pillarbox bars.
    # width/height are the picture; pad_to is the coded frame around it.
    pad = video.get("pad_to")
    if pad:
        pw, ph = int(pad["width"]), int(pad["height"])
        parts.append(f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color=black")
    if overlay_filters:
        parts.extend(overlay_filters)
    if video.get("telecine"):
        # 3:2 pulldown — 23.976p carried inside a 29.97i stream.
        parts.append(f"telecine=pattern={video['telecine']}")
    if video.get("interlace"):
        fps = video.get("interlace_fps", "59.94")
        parts.append(f"fps={fps}")
        parts.append("tinterlace=interleave_top")
        parts.append("setfield=tff")
    # Without this, ffmpeg's scale filter rewrites SAR to preserve the *source* display
    # aspect, so scaling a 16:9 master to 720x540 silently yields DAR 16:9 rather than
    # the 4:3 the dimensions imply. Declare a SAR to opt into anamorphic on purpose.
    parts.append(f"setsar={video['sar']}" if video.get("sar") else "setsar=1")
    # Colour signalling must be set here, not as an output option: ffmpeg 9 silently
    # ignores -color_primaries and -color_trc on the output (it still honours
    # -colorspace), so a variant whose codec does not carry the signalling itself
    # would be tagged "unknown" with no warning. setparams works for every encoder.
    params = _setparams_filter(video)
    if params:
        parts.append(params)
    return ",".join(parts)


def _setparams_filter(video: dict) -> str:
    """Build a setparams filter for the declared colour signalling, or "" if none."""
    hdr = video.get("hdr") or {}
    fields = {
        "color_primaries": hdr.get("color_primaries"),
        "color_trc": hdr.get("color_trc"),
        "colorspace": hdr.get("colorspace") or (video.get("colorspace") if not hdr else None),
    }
    declared = [f"{k}={v}" for k, v in fields.items() if v]
    return f"setparams={':'.join(declared)}" if declared else ""


def _variant_overlay(variant: dict | None, defaults: dict, *, width: int, height: int, framerate) -> list[str]:
    """Return drawtext filters for a variant, honoring a per-variant burn_in opt-out."""
    if variant is None or variant.get("burn_in") is False:
        return []
    overlay = defaults.get("overlay") or {}
    if not overlay_enabled(overlay):
        return []
    lines = overlay_lines_for(variant, width=width, height=height, framerate=framerate)
    return variant_overlay_filters(overlay, height, lines)


def _common_video_args(
    video: dict,
    defaults: dict,
    *,
    width: int,
    height: int,
    framerate: int | float | None = None,
    variant: dict | None = None,
) -> list[str]:
    """Produce the `-c:v ... -b:v ...` style args for a video stream.

    Pass `variant` to draw the per-file information overlay. Omitting it produces the
    same arguments as before, so a caller that has no variant context stays valid.
    """
    codec = video["codec"]
    caps = _codec_caps(codec)

    args: list[str] = [
        "-c:v", codec,
        "-pix_fmt", video["pix_fmt"],
    ]
    if "profile" in caps and video.get("profile"):
        args += ["-profile:v", video["profile"]]
    if "level" in caps and video.get("level"):
        args += ["-level:v", video["level"]]
    if video.get("bitrate"):
        args += ["-b:v", video["bitrate"]]
        if "vbv" in caps:
            args += ["-maxrate", video["bitrate"], "-bufsize", _double_bitrate(video["bitrate"])]
    if "preset" in caps and video.get("preset"):
        args += ["-preset", video["preset"]]
    if video.get("qscale") is not None:
        args += ["-q:v", str(video["qscale"])]
    if video.get("vtag"):
        args += ["-vtag", video["vtag"]]
    if video.get("cpu_used") is not None:
        args += ["-cpu-used", str(video["cpu_used"])]
    if video.get("row_mt") is not None:
        args += ["-row-mt", str(video["row_mt"])]
    if video.get("interlace"):
        args += ["-flags", "+ilme+ildct", "-top", "1"]
        if codec == "libx264":
            args += ["-x264-params", "interlaced=1:tff=1"]
        elif codec == "libx265":
            args += ["-x265-params", "interlace=1"]
    if video.get("aspect"):
        args += ["-aspect", str(video["aspect"])]

    gop = defaults.get("gop", 48)
    args += ["-g", str(gop)]
    if "keyint_min" in caps:
        args += ["-keyint_min", str(gop)]

    effective_fps = framerate if framerate is not None else defaults["framerate"]
    overlay_filters = _variant_overlay(
        variant, defaults, width=width, height=height, framerate=effective_fps
    )
    args += ["-vf", _build_vf_chain(video, width, height, overlay_filters)]
    args += ["-r", str(effective_fps)]

    # Colour signalling is applied by the setparams filter in _build_vf_chain.
    # x265_params stays here: it writes the signalling into the bitstream VUI, which
    # is a different layer from the container tags and worth keeping in step.
    hdr = video.get("hdr") or {}
    if hdr.get("x265_params"):
        args += ["-x265-params", hdr["x265_params"]]

    if video.get("color_range"):
        args += ["-color_range", str(video["color_range"])]

    if video.get("tag"):
        args += ["-tag:v", video["tag"]]

    return args


# Containers that both accept these tags and have a reader that surfaces them.
# ISOBMFF is the set Plex parses; Matroska and WebM are read by Jellyfin. MPEG-TS
# has no metadata container at all, and the remaining raw formats are not worth the
# risk of perturbing an edge case that exists to test malformed structure.
IDENTITY_CONTAINERS = frozenset({"mp4", "m4v", "mov", "m4a", "mkv", "webm"})

# Prefix that makes the identifier greppable in whatever field a server exposes it in.
UUID_TAG_PREFIX = "testlib-uuid="


def identity_metadata_args(variant: dict) -> list[str]:
    """Return -metadata args stamping the variant identity into the container.

    Plex reads `synopsis` in preference to `description` and `comment`, and shows it
    as the item summary; it takes `title` over the filename. Both are ISOBMFF-only in
    Plex, so this is enrichment on top of the filename, never a replacement for it.
    Returns an empty list for containers with no reader, which keeps those files
    byte-for-byte what they were before.
    """
    container = (variant.get("container") or "").lower()
    if container not in IDENTITY_CONTAINERS:
        return []
    variant_uuid = variant.get("uuid")
    if not variant_uuid:
        return []

    stamp = f"{UUID_TAG_PREFIX}{variant_uuid}"
    title = variant.get("title") or variant["name"]
    description = variant.get("description") or ""
    # The identifier leads so a truncated summary still carries it.
    summary = f"{stamp} | {variant['name']}"
    if description:
        summary = f"{summary} | {description}"

    return [
        "-metadata", f"title={title} [{short(variant_uuid)}]",
        "-metadata", f"synopsis={summary}",
        "-metadata", f"description={summary}",
        "-metadata", f"comment={stamp}",
    ]


def _common_audio_args(audio: dict) -> list[str]:
    args: list[str] = ["-c:a", audio["codec"]]
    # dca and truehd are flagged experimental by ffmpeg and refuse to run without this.
    if audio.get("strict"):
        args += ["-strict", str(audio["strict"])]
    if audio.get("bitrate"):
        args += ["-b:a", audio["bitrate"]]
    if audio.get("channels"):
        args += ["-ac", str(audio["channels"])]
    if audio.get("sample_rate"):
        args += ["-ar", str(audio["sample_rate"])]
    if audio.get("sample_fmt"):
        args += ["-sample_fmt", audio["sample_fmt"]]
    return args


def _double_bitrate(bitrate: str) -> str:
    """Return a bufsize recommendation = 2 * bitrate."""
    b = bitrate.strip().lower()
    if b.endswith("k"):
        return f"{int(b[:-1]) * 2}k"
    if b.endswith("m"):
        return f"{int(b[:-1]) * 2}M"
    try:
        return str(int(b) * 2)
    except ValueError:
        return bitrate


def _audio_src_for_channels(sources_dir: Path, channels: int,
                            defaults: dict | None = None) -> tuple[list[str], int]:
    """Return ffmpeg input args plus the channel count of the synthesized source.

    Mono and stereo use the master stereo source (ffmpeg down-mixes to mono as needed).
    Multi-channel targets synthesize a layout-correct bed with lavfi aevalsrc so the
    encoder actually has N channels to compress.
    """
    if channels <= 2:
        name = (defaults or {}).get("source_audio", "source_audio.wav")
        return ["-i", str(sources_dir / name)], channels
    layout = {
        3: "2.1",
        4: "quad",
        5: "5.0",
        6: "5.1",
        7: "6.1",
        8: "7.1",
        10: "5.1.4",
        12: "7.1.4",
        14: "9.1.4",
        16: "9.1.6",
    }.get(channels, "5.1")
    lavfi = f"aevalsrc=0.1*sin(440*2*PI*t):channel_layout={layout}:sample_rate=48000:duration=999"
    return ["-f", "lavfi", "-i", lavfi], channels


def _chapter_input(chapters_path: Path | None, args_so_far: list[str]) -> tuple[list[str], list[str]]:
    """Return (input_args, mapping_args) for a chapter metadata sidecar.

    The sidecar is appended after every media input so that the hardcoded stream
    maps (`-map 0:v:0`, `-map 1:a:0`, and the computed `idx+1` / `idx+2` offsets)
    keep referring to the same inputs. Its own index is counted from the args
    built so far rather than assumed.
    """
    if chapters_path is None:
        return [], []
    index = sum(1 for a in args_so_far if a == "-i")
    return (
        ["-i", str(chapters_path)],
        ["-map_metadata", str(index), "-map_chapters", str(index)],
    )


def build_video(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                chapters_path: Path | None = None) -> list[str]:
    """Construct ffmpeg args for a standard video+audio variant."""
    width = variant.get("width", defaults["width"])
    height = variant.get("height", defaults["height"])
    framerate = variant.get("framerate")
    video = variant["video"]
    audio = variant["audio"]

    audio_input_args, _channels = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)

    pre_input: list[str] = []
    if video.get("itsoffset") is not None:
        pre_input += ["-itsoffset", str(video["itsoffset"])]

    # An offset on the audio input desynchronises A/V on purpose; video.itsoffset
    # shifts the picture instead.
    audio_offset = ["-itsoffset", str(audio["itsoffset"])] if audio.get("itsoffset") is not None else []
    inputs: list[str] = [
        *pre_input,
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_offset,
        *audio_input_args,
    ]
    chapter_input, chapter_maps = _chapter_input(chapters_path, inputs)

    args: list[str] = [
        *inputs,
        *chapter_input,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *chapter_maps,
        *_duration_flag(defaults, variant),
        *_common_video_args(video, defaults, width=width, height=height, framerate=framerate, variant=variant),
        *_common_audio_args(audio),
        str(output_path),
    ]
    return args


def build_audio_only(variant: dict, defaults: dict, sources_dir: Path, output_path: Path) -> list[str]:
    """Construct ffmpeg args for an audio-only variant."""
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)

    args: list[str] = [
        *audio_input_args,
        *_duration_flag(defaults, variant),
        "-vn",
        *_common_audio_args(audio),
        str(output_path),
    ]
    return args


def build_subtitle_external(variant: dict, defaults: dict, sources_dir: Path, output_path: Path) -> list[str]:
    """Convert the ASS source to the declared external subtitle format."""
    fmt = variant["format"]
    codec = {"srt": "srt", "webvtt": "webvtt", "ass": "ass"}[fmt]
    args: list[str] = [
        "-i", str(sources_dir / defaults["source_subtitle"]),
        "-c:s", codec,
        str(output_path),
    ]
    return args


def build_subtitle_embedded(variant: dict, defaults: dict, sources_dir: Path, output_path: Path) -> list[str]:
    """Embed a single subtitle track alongside video + audio."""
    width = variant.get("width", defaults["width"])
    height = variant.get("height", defaults["height"])
    video = variant["video"]
    audio = variant["audio"]
    sub_codec = {"srt": "srt", "ass": "ass", "mov_text": "mov_text", "webvtt": "webvtt"}[variant["format"]]

    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)

    args: list[str] = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-i", str(sources_dir / defaults["source_subtitle"]),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-map", "2:s:0",
        *_duration_flag(defaults, variant),
        *_common_video_args(video, defaults, width=width, height=height, variant=variant),
        *_common_audio_args(audio),
        "-c:s", sub_codec,
        "-metadata:s:s:0", f"language={variant['language']}",
        str(output_path),
    ]
    return args


def build_stream(variant: dict, defaults: dict, sources_dir: Path, out_dir: Path) -> tuple[list[str], Path]:
    """Construct ffmpeg args for an HLS or DASH variant. Returns (args, manifest_path)."""
    width = variant.get("width", defaults["width"])
    height = variant.get("height", defaults["height"])
    video = variant["video"]
    audio = variant["audio"]
    seg_dur = int(variant.get("segment_duration", 4))
    stream_type = variant["type"]

    out_dir.mkdir(parents=True, exist_ok=True)
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)

    common_inputs = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults, variant),
        *_common_video_args(video, defaults, width=width, height=height, variant=variant),
        *_common_audio_args(audio),
    ]

    if stream_type == "hls":
        manifest = out_dir / "playlist.m3u8"
        args = [
            *common_inputs,
            "-f", "hls",
            "-hls_time", str(seg_dur),
            "-hls_list_size", "0",
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
            str(manifest),
        ]
        return args, manifest

    if stream_type == "dash":
        manifest = out_dir / "manifest.mpd"
        args = [
            *common_inputs,
            "-f", "dash",
            "-seg_duration", str(seg_dur),
            "-use_timeline", "1",
            "-use_template", "1",
            "-init_seg_name", "init-$RepresentationID$.m4s",
            "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.m4s",
            str(manifest),
        ]
        return args, manifest

    raise ValueError(f"unknown stream type: {stream_type}")


def _edge_no_audio(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Video with no audio stream at all."""
    video = variant["video"]
    return [
        "-i", str(sources_dir / defaults["source_video"]),
        *_duration_flag(defaults, variant),
        "-map", "0:v:0",
        "-an",
        *_common_video_args(video, defaults, width=width, height=height, variant=variant),
        str(output_path),
    ]


def _edge_audio_only(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Audio with no video stream."""
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)
    return [
        *audio_input_args,
        *_duration_flag(defaults, variant),
        "-vn",
        *_common_audio_args(audio),
        str(output_path),
    ]


def _edge_multi_audio(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """One video with several audio tracks."""
    video = variant["video"]
    tracks = variant["audio_tracks"]
    args: list[str] = [
        "-i", str(sources_dir / defaults["source_video"]),
    ]
    for t in tracks:
        a_in, _ = _audio_src_for_channels(sources_dir, t.get("channels", 2))
        args += a_in
    args += [*_duration_flag(defaults, variant), "-map", "0:v:0"]
    for idx, _t in enumerate(tracks):
        args += ["-map", f"{idx + 1}:a:0"]
    args += _common_video_args(video, defaults, width=width, height=height, variant=variant)
    for idx, t in enumerate(tracks):
        args += [f"-c:a:{idx}", t["codec"]]
        if t.get("bitrate"):
            args += [f"-b:a:{idx}", t["bitrate"]]
        if t.get("channels"):
            args += [f"-ac:a:{idx}", str(t["channels"])]
        if t.get("sample_rate"):
            args += [f"-ar:a:{idx}", str(t["sample_rate"])]
        args += [f"-metadata:s:a:{idx}", f"language={t.get('language', 'und')}"]
        if t.get("title"):
            args += [f"-metadata:s:a:{idx}", f"title={t['title']}"]
        if t.get("default"):
            args += [f"-disposition:a:{idx}", "default"]
    args += [str(output_path)]
    return args


def _edge_multi_subtitle(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """One video with several subtitle tracks."""
    video = variant["video"]
    audio = variant["audio"]
    tracks = variant["subtitle_tracks"]
    args = [
        "-i", str(sources_dir / defaults["source_video"]),
    ]
    a_in, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)
    args += a_in
    for _t in tracks:
        args += ["-i", str(sources_dir / defaults["source_subtitle"])]
    args += [
        *_duration_flag(defaults, variant),
        "-map", "0:v:0",
        "-map", "1:a:0",
    ]
    for idx, _t in enumerate(tracks):
        args += ["-map", f"{idx + 2}:s:0"]
    args += _common_video_args(video, defaults, width=width, height=height, variant=variant)
    args += _common_audio_args(audio)
    for idx, t in enumerate(tracks):
        codec = {"srt": "srt", "ass": "ass", "webvtt": "webvtt", "mov_text": "mov_text"}[t["format"]]
        args += [f"-c:s:{idx}", codec]
        args += [f"-metadata:s:s:{idx}", f"language={t.get('language', 'und')}"]
        if t.get("title"):
            args += [f"-metadata:s:s:{idx}", f"title={t['title']}"]
        disposition_parts = []
        if t.get("default"):
            disposition_parts.append("default")
        if t.get("forced"):
            disposition_parts.append("forced")
        if disposition_parts:
            args += [f"-disposition:s:{idx}", "+".join(disposition_parts)]
    args += [str(output_path)]
    return args


def _edge_variable_fps(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Variable frame rate via setpts."""
    video = variant["video"]
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)
    # Overlay is drawn before setpts so the burned timestamp is the true
    # pre-remap source time, which is what makes the remapping visible on screen.
    overlay_filters = _variant_overlay(
        variant, defaults, width=width, height=height,
        framerate=variant.get("framerate", defaults["framerate"]),
    )
    vf = ",".join([
        _scale_filter(width, height),
        *overlay_filters,
        "setpts='if(lt(N,72),PTS,if(lt(N,144),PTS*1.5,PTS*2))'",
    ])
    args = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults, variant),
        "-c:v", video["codec"],
        "-pix_fmt", video["pix_fmt"],
        "-profile:v", video["profile"],
        "-b:v", video["bitrate"],
        "-preset", video.get("preset", "medium"),
        "-vf", vf,
        "-vsync", "vfr",
        "-fps_mode", "vfr",
        *_common_audio_args(audio),
        str(output_path),
    ]
    return args


def _edge_broken_timestamps(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Deliberately non-monotonic PTS."""
    video = variant["video"]
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)
    # Same ordering rule as variable_fps: burn the pre-remap timestamp.
    overlay_filters = _variant_overlay(
        variant, defaults, width=width, height=height,
        framerate=variant.get("framerate", defaults["framerate"]),
    )
    vf = ",".join([
        _scale_filter(width, height),
        *overlay_filters,
        "setpts='PTS+if(gt(N,120),5/TB,0)'",
    ])
    args = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults, variant),
        "-c:v", video["codec"],
        "-pix_fmt", video["pix_fmt"],
        "-profile:v", video["profile"],
        "-b:v", video["bitrate"],
        "-preset", video.get("preset", "medium"),
        "-vf", vf,
        "-copyts",
        "-muxdelay", "0",
        "-muxpreload", "0",
        *_common_audio_args(audio),
        str(output_path),
    ]
    return args


def _edge_annexb(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """H.264 in Annex B framing rather than AVCC."""
    video = variant["video"]
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)
    args = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults, variant),
        *_common_video_args(video, defaults, width=width, height=height, variant=variant),
        "-bsf:v", "h264_mp4toannexb",
        *_common_audio_args(audio),
        str(output_path),
    ]
    return args


def _edge_fragmented_mp4(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Fragmented MP4 with moof boxes."""
    video = variant["video"]
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)
    args = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults, variant),
        *_common_video_args(video, defaults, width=width, height=height, variant=variant),
        *_common_audio_args(audio),
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        str(output_path),
    ]
    return args


def _edge_missing_metadata(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Container and stream metadata stripped."""
    video = variant["video"]
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2), defaults)
    inputs = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
    ]
    # Attach the chapter sidecar as an input but never map it: the -map_chapters -1
    # below then genuinely strips chapters that were present, rather than the file
    # simply never having had any.
    chapter_input, _unused_maps = _chapter_input(chapters_path, inputs)
    args = [
        *inputs,
        *chapter_input,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults, variant),
        *_common_video_args(video, defaults, width=width, height=height, variant=variant),
        *_common_audio_args(audio),
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        "-flags:a", "+bitexact",
        str(output_path),
    ]
    return args

# These differ from a standard build only in metadata or a post-encode step, so the
# encode itself is build_video. rotation is stamped afterwards by generate.py.


def _edge_plain_build(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Differs from a standard build only in metadata or a post-encode step."""
    return build_video(variant, defaults, sources_dir, output_path, chapters_path)


def _edge_truncated(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Standard build with faststart, truncated afterwards."""
    args = build_video(variant, defaults, sources_dir, output_path, chapters_path)
    # faststart puts moov at the front so the header survives the later truncation
    return args[:-1] + ["-movflags", "+faststart"] + args[-1:]


def _edge_chapters(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
                    chapters_path: Path | None, width: int, height: int) -> list[str]:
    """Malformed-chapter cases: a standard build plus a metadata sidecar."""
    # Malformed-chapter cases differ only in their metadata sidecar, so the encode
    # itself is a standard build. Placed last so a variant that also carries
    # missing_metadata is handled by the strip branch above instead.
    return build_video(variant, defaults, sources_dir, output_path, chapters_path)

# Ordered: the first flag that matches wins, and order is load-bearing. `chapters`
# sits last so a malformed-chapter variant that also carries `missing_metadata` is
# handled by the strip path instead. Add an edge case by writing a handler and
# adding a row here, not by extending a branch chain.
_EDGE_HANDLERS: tuple[tuple[str | None, object], ...] = (
    ("no_audio", _edge_no_audio),
    ("audio_only", _edge_audio_only),
    ("multi_audio", _edge_multi_audio),
    ("multi_subtitle", _edge_multi_subtitle),
    ("variable_fps", _edge_variable_fps),
    ("broken_timestamps", _edge_broken_timestamps),
    ("annexb", _edge_annexb),
    ("fragmented_mp4", _edge_fragmented_mp4),
    ("missing_metadata", _edge_missing_metadata),
    (None, _edge_plain_build),
    ("truncated", _edge_truncated),
    ("chapters", _edge_chapters),
)

# Flags whose variants are a standard build plus metadata or a post-encode step.
# `None` in the table above means "any of these".
_PLAIN_EDGE_FLAGS = {"low_bitrate", "high_bitrate", "avcc", "edit_list",
                     "rotation", "aspect_conflict", "av_desync"}


def build_edge(variant: dict, defaults: dict, sources_dir: Path, output_path: Path,
               chapters_path: Path | None = None) -> list[str]:
    """Dispatch an edge-case variant to the handler for its first matching flag."""
    flags = set(variant.get("flags", []))
    width = variant.get("width", defaults["width"])
    height = variant.get("height", defaults["height"])
    for flag, handler in _EDGE_HANDLERS:
        matched = (flags & _PLAIN_EDGE_FLAGS) if flag is None else (flag in flags)
        if matched:
            return handler(variant, defaults, sources_dir, output_path,
                           chapters_path, width, height)
    raise ValueError(
        f"no edge handler matched variant {variant['name']} with flags {sorted(flags)}")
