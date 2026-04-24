"""Build ffmpeg argument lists for each matrix variant.

Each builder is a pure function: (variant_config, defaults, sources_dir,
output_path) -> list[str] of ffmpeg args (excluding the leading 'ffmpeg'
binary — ffutil.run_ffmpeg prepends it).
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("encoders")

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


def _duration_flag(defaults: dict) -> list[str]:
    return ["-t", str(defaults["duration_seconds"])]


def _scale_filter(width: int, height: int) -> str:
    return f"scale={width}:{height}:flags=lanczos"


def _build_vf_chain(video: dict, width: int, height: int) -> str:
    """Compose the -vf filter chain from scale + optional interlace + optional SAR."""
    parts: list[str] = [_scale_filter(width, height)]
    if video.get("interlace"):
        fps = video.get("interlace_fps", "59.94")
        parts.append(f"fps={fps}")
        parts.append("tinterlace=interleave_top")
        parts.append("setfield=tff")
    if video.get("sar"):
        parts.append(f"setsar={video['sar']}")
    return ",".join(parts)


def _common_video_args(video: dict, defaults: dict, *, width: int, height: int, framerate: int | float | None = None) -> list[str]:
    """Produce the `-c:v ... -b:v ...` style args for a video stream."""
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

    args += ["-vf", _build_vf_chain(video, width, height)]
    args += ["-r", str(framerate if framerate is not None else defaults["framerate"])]

    hdr = video.get("hdr") or {}
    if hdr:
        if hdr.get("color_primaries"):
            args += ["-color_primaries", hdr["color_primaries"]]
        if hdr.get("color_trc"):
            args += ["-color_trc", hdr["color_trc"]]
        if hdr.get("colorspace"):
            args += ["-colorspace", hdr["colorspace"]]
        if hdr.get("x265_params"):
            args += ["-x265-params", hdr["x265_params"]]

    if video.get("tag"):
        args += ["-tag:v", video["tag"]]

    return args


def _common_audio_args(audio: dict) -> list[str]:
    args: list[str] = ["-c:a", audio["codec"]]
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


def _audio_src_for_channels(sources_dir: Path, channels: int) -> tuple[list[str], int]:
    """Return ffmpeg input args plus the channel count of the synthesized source.

    Mono and stereo use the master stereo source (ffmpeg down-mixes to mono as needed).
    Multi-channel targets synthesize a layout-correct bed with lavfi aevalsrc so the
    encoder actually has N channels to compress.
    """
    if channels <= 2:
        return ["-i", str(sources_dir / "source_audio.wav")], channels
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


def build_video(variant: dict, defaults: dict, sources_dir: Path, output_path: Path) -> list[str]:
    """Construct ffmpeg args for a standard video+audio variant."""
    width = variant.get("width", defaults["width"])
    height = variant.get("height", defaults["height"])
    framerate = variant.get("framerate")
    video = variant["video"]
    audio = variant["audio"]

    audio_input_args, _channels = _audio_src_for_channels(sources_dir, audio.get("channels", 2))

    pre_input: list[str] = []
    if video.get("itsoffset") is not None:
        pre_input += ["-itsoffset", str(video["itsoffset"])]

    args: list[str] = [
        *pre_input,
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults),
        *_common_video_args(video, defaults, width=width, height=height, framerate=framerate),
        *_common_audio_args(audio),
        str(output_path),
    ]
    return args


def build_audio_only(variant: dict, defaults: dict, sources_dir: Path, output_path: Path) -> list[str]:
    """Construct ffmpeg args for an audio-only variant."""
    audio = variant["audio"]
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))

    args: list[str] = [
        *audio_input_args,
        *_duration_flag(defaults),
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

    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))

    args: list[str] = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-i", str(sources_dir / defaults["source_subtitle"]),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-map", "2:s:0",
        *_duration_flag(defaults),
        *_common_video_args(video, defaults, width=width, height=height),
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
    audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))

    common_inputs = [
        "-i", str(sources_dir / defaults["source_video"]),
        *audio_input_args,
        "-map", "0:v:0",
        "-map", "1:a:0",
        *_duration_flag(defaults),
        *_common_video_args(video, defaults, width=width, height=height),
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


def build_edge(variant: dict, defaults: dict, sources_dir: Path, output_path: Path) -> list[str]:
    """Dispatch edge-case variants to the right construction path."""
    flags = set(variant.get("flags", []))
    name = variant["name"]
    width = variant.get("width", defaults["width"])
    height = variant.get("height", defaults["height"])

    if "no_audio" in flags:
        video = variant["video"]
        return [
            "-i", str(sources_dir / defaults["source_video"]),
            *_duration_flag(defaults),
            "-map", "0:v:0",
            "-an",
            *_common_video_args(video, defaults, width=width, height=height),
            str(output_path),
        ]

    if "audio_only" in flags:
        audio = variant["audio"]
        audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))
        return [
            *audio_input_args,
            *_duration_flag(defaults),
            "-vn",
            *_common_audio_args(audio),
            str(output_path),
        ]

    if "multi_audio" in flags:
        video = variant["video"]
        tracks = variant["audio_tracks"]
        args: list[str] = [
            "-i", str(sources_dir / defaults["source_video"]),
        ]
        for t in tracks:
            a_in, _ = _audio_src_for_channels(sources_dir, t.get("channels", 2))
            args += a_in
        args += [*_duration_flag(defaults), "-map", "0:v:0"]
        for idx, _t in enumerate(tracks):
            args += ["-map", f"{idx + 1}:a:0"]
        args += _common_video_args(video, defaults, width=width, height=height)
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

    if "multi_subtitle" in flags:
        video = variant["video"]
        audio = variant["audio"]
        tracks = variant["subtitle_tracks"]
        args = [
            "-i", str(sources_dir / defaults["source_video"]),
        ]
        a_in, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))
        args += a_in
        for _t in tracks:
            args += ["-i", str(sources_dir / defaults["source_subtitle"])]
        args += [
            *_duration_flag(defaults),
            "-map", "0:v:0",
            "-map", "1:a:0",
        ]
        for idx, _t in enumerate(tracks):
            args += ["-map", f"{idx + 2}:s:0"]
        args += _common_video_args(video, defaults, width=width, height=height)
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

    if "variable_fps" in flags:
        video = variant["video"]
        audio = variant["audio"]
        audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))
        vf = (
            f"{_scale_filter(width, height)},"
            "setpts='if(lt(N,72),PTS,if(lt(N,144),PTS*1.5,PTS*2))'"
        )
        args = [
            "-i", str(sources_dir / defaults["source_video"]),
            *audio_input_args,
            "-map", "0:v:0",
            "-map", "1:a:0",
            *_duration_flag(defaults),
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

    if "broken_timestamps" in flags:
        video = variant["video"]
        audio = variant["audio"]
        audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))
        vf = (
            f"{_scale_filter(width, height)},"
            "setpts='PTS+if(gt(N,120),5/TB,0)'"
        )
        args = [
            "-i", str(sources_dir / defaults["source_video"]),
            *audio_input_args,
            "-map", "0:v:0",
            "-map", "1:a:0",
            *_duration_flag(defaults),
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

    if "annexb" in flags:
        video = variant["video"]
        audio = variant["audio"]
        audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))
        args = [
            "-i", str(sources_dir / defaults["source_video"]),
            *audio_input_args,
            "-map", "0:v:0",
            "-map", "1:a:0",
            *_duration_flag(defaults),
            *_common_video_args(video, defaults, width=width, height=height),
            "-bsf:v", "h264_mp4toannexb",
            *_common_audio_args(audio),
            str(output_path),
        ]
        return args

    if "fragmented_mp4" in flags:
        video = variant["video"]
        audio = variant["audio"]
        audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))
        args = [
            "-i", str(sources_dir / defaults["source_video"]),
            *audio_input_args,
            "-map", "0:v:0",
            "-map", "1:a:0",
            *_duration_flag(defaults),
            *_common_video_args(video, defaults, width=width, height=height),
            *_common_audio_args(audio),
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            str(output_path),
        ]
        return args

    if "missing_metadata" in flags:
        video = variant["video"]
        audio = variant["audio"]
        audio_input_args, _ = _audio_src_for_channels(sources_dir, audio.get("channels", 2))
        args = [
            "-i", str(sources_dir / defaults["source_video"]),
            *audio_input_args,
            "-map", "0:v:0",
            "-map", "1:a:0",
            *_duration_flag(defaults),
            *_common_video_args(video, defaults, width=width, height=height),
            *_common_audio_args(audio),
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-fflags", "+bitexact",
            "-flags:v", "+bitexact",
            "-flags:a", "+bitexact",
            str(output_path),
        ]
        return args

    if "low_bitrate" in flags or "high_bitrate" in flags or "avcc" in flags or "edit_list" in flags:
        return build_video(variant, defaults, sources_dir, output_path)

    if "truncated" in flags:
        args = build_video(variant, defaults, sources_dir, output_path)
        # faststart puts moov at the front so the header survives the later truncation
        return args[:-1] + ["-movflags", "+faststart"] + args[-1:]

    raise ValueError(f"no edge handler matched variant {name} with flags {flags}")
