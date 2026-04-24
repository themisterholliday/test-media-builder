# Architecture

How the media test library is built, what each component does, and how to extend it. This doc is deeper than the README — if you just want to run the thing, start there.

---

## Table of contents

- [Table of contents](#table-of-contents)
- [Component map](#component-map)
- [Pipeline flow](#pipeline-flow)
  - [Generate (`scripts/generate.py`)](#generate-scriptsgeneratepy)
  - [Verify (`scripts/verify.py`)](#verify-scriptsverifypy)
  - [Manifest (`scripts/manifest.py`)](#manifest-scriptsmanifestpy)
  - [Thumbnails (`scripts/thumbnails.py`)](#thumbnails-scriptsthumbnailspy)
- [Determinism model](#determinism-model)
- [NFO schema](#nfo-schema)
  - [Top half — Kodi-compatible `<movie>` schema](#top-half--kodi-compatible-movie-schema)
  - [Bottom half — `<testlib>` custom extension](#bottom-half--testlib-custom-extension)
- [manifest.json schema](#manifestjson-schema)
- [verify_report.json schema](#verify_reportjson-schema)
- [build_manifest.json schema](#build_manifestjson-schema)
- [Extending the matrix](#extending-the-matrix)
  - [Add a synthesizable video variant](#add-a-synthesizable-video-variant)
  - [Add an audio-only variant](#add-an-audio-only-variant)
  - [Add a subtitle variant](#add-a-subtitle-variant)
  - [Add a stream variant](#add-a-stream-variant)
- [Adding a new edge-case handler](#adding-a-new-edge-case-handler)
- [External-sample workflow](#external-sample-workflow)
- [FFmpeg quirks handled by the pipeline](#ffmpeg-quirks-handled-by-the-pipeline)

---

## Component map

```
config/
└── media_matrix.json          # single source of truth — every variant is declared here

scripts/
├── ffutil.py                  # ffmpeg/ffprobe subprocess wrapper (timeout, structured errors, JSON parsing)
├── bootstrap.py               # synthesizes missing sources with ffmpeg lavfi; duration-aware regeneration
│                              #   tracked via sources/.synthetic_sources.json sentinel (never touches user files)
├── encoders.py                # pure ffmpeg-arg builders: build_video / build_audio_only /
│                              #   build_subtitle_external / build_subtitle_embedded /
│                              #   build_stream / build_edge
├── nfo_writer.py              # builds Kodi-compatible <movie> XML + <testlib> extension from probe data
├── generate.py                # orchestrator — CLI, plan resolution, execute loop, probe-verify,
│                              #   NFO write, parallel workers, build_manifest
├── verify.py                  # post-creation validator — decode smoke + contract + NFO + bitstream assertions
├── manifest.py                # emits generated/manifest.json (flat library index)
└── thumbnails.py              # per-variant JPEG + contact-sheet composite

sources/
├── source_master.mov          # test-pattern video (synthesized if absent; ProRes, matches configured duration)
├── source_audio.wav           # test-tone audio (synthesized if absent; 440+880 Hz sine mix)
├── source_sub.ass             # ASS subtitle with one cue per second, each labeled [HH:MM:SS] Cue N of TOTAL
├── .synthetic_sources.json    # sentinel — marks which source files were produced by bootstrap
└── external/                  # user-supplied licensed samples (DV, HDR10+, DTS-HD, TrueHD, …)

Makefile                       # convenience targets; all wrap python3 -m scripts.<module>
```

Every script is directly runnable as `python -m scripts.<name>`. Nothing writes to the repo outside `generated/`, `sources/` (on `--bootstrap`), or `config/` (only when you edit it).

---

## Pipeline flow

### Generate (`scripts/generate.py`)

```
1. require_binaries()                                     # preconditions: ffmpeg + ffprobe present
2. _load_config(config/media_matrix.json)                 # parse matrix
3. apply --duration / --duration-minutes if passed        # override defaults.duration_seconds
4. ensure_sources(sources/, duration)                     # synthesize/refresh any synthetic source
                                                          #   that doesn't match target duration;
                                                          #   user-supplied files are never overwritten
5. _all_plans(cfg)                                        # build one Plan dataclass per matrix entry
6. _filter_plans(plans, sections, variant_names)          # honor --only / --variant
7. for each Plan (serial or parallel pool):
       if external and source missing:   → pending_external
       elif output + NFO present AND no source is newer than output:  → skipped
       else:
           args, probe_target = _build_command(plan, defaults)      # encoders.py
           run_ffmpeg(args)                                          # ffutil.py
           (for truncated-flag variants: post-process byte-truncate) # generate.py
           probe = _probe_for(plan)                                  # ffprobe, with DASH special-case
           _verify_contract(plan, probe)                             # structural determinism check
           build_nfo(variant, probe, ...)  +  write_nfo(path)        # nfo_writer.py
8. _write_build_manifest(cfg, ffmpeg_version, outcomes, duration)    # reproducibility block
```

The skip check compares source mtimes against output mtime — any source file newer than the output triggers a rebuild. So changing `--duration` regenerates the synthetic sources (touching mtime), which automatically invalidates downstream outputs. No separate `--force` needed when only the duration changed.

Each `Plan` carries the matrix entry, the resolved output path, the NFO path, the `kind` (`video`/`audio`/`subtitle_external`/`subtitle_embedded`/`stream`/`edge`/`external`), and — for streams — the manifest path inside the output directory. Builders dispatch on `kind`.

### Verify (`scripts/verify.py`)

Runs the same plan-resolution as `generate.py` (imported directly) but performs _checks_ instead of encodes:

```
for each plan:
  1. target file exists and is non-empty                  → checks.exists
  2. decode smoke: ffmpeg -v error -i X -f null -          → checks.decode
     (skipped for subtitle_external + truncated variants)
     (DASH gets a custom pipe-through-cat path since ffmpeg has no DASH demuxer)
  3. ffprobe + contract re-check (same logic as build time) → checks.contract
  4. NFO exists, parses, uniqueid matches filename         → checks.nfo.*
  5. per-flag bitstream assertions:
       fragmented_mp4   → moof box present in first 4 MiB
       annexb           → 0x000001 start codes in first 32 bytes
       missing_metadata → no 'encoder' tag in format.tags
       no_audio         → zero audio streams in probe
       audio_only       → zero video streams in probe
       multi_audio      → audio-stream count == declared audio_tracks length
       multi_subtitle   → subtitle-stream count == declared subtitle_tracks length
       hdr              → probe color_primaries / color_transfer / color_space match declared
       subtitle_embedded → probe's subtitle-stream language == variant.language
```

Writes `generated/verify_report.json` with a per-variant status (`ok` / `failed` / `pending_external`) plus the specific check results and any error / warning messages. Exits non-zero if anything failed.

### Manifest (`scripts/manifest.py`)

Walks the same plan set, but for _each_ plan:

1. ffprobe the target (if present)
2. Build a normalized entry: `{name, section, title, description, path, container, flags, status, size, sha256, format, duration_sec, bitrate_bps, video: {...}, audio_tracks: [...], subtitle_tracks: [...], hdr: {...}}`
3. For missing externals, emit `{status: "pending_external", source_path, license_note}` so consumers can see what's declared-but-unavailable.

Writes `generated/manifest.json` with a `_meta` block (ffmpeg version, matrix SHA-256, built-at) and the `entries` array.

### Thumbnails (`scripts/thumbnails.py`)

For every plan that has a video stream (skipping audio-only / subtitle_external / missing externals):

1. `ffmpeg -ss 5 -i <target> -frames:v 1 -vf "scale=320:-1:flags=lanczos"` → `generated/_thumbs/<name>.jpg`.
2. For HDR variants, prepend a simple colorspace conversion `scale=out_color_matrix=bt709:out_range=tv,format=yuv420p` so the thumbnail isn't washed out. (If ffmpeg had `zscale`, a full tone-map chain could be used; the current code falls back gracefully.)
3. If seek to t=5 fails (truncated files, broken PTS), retry at t=0.

After all thumbs are extracted, builds a contact sheet via `-f concat -i <list> -vf tile=4xN`.

---

## Determinism model

**Structural, not bit-identical.** Re-running the generator against the same matrix, same ffmpeg version, and same sources produces:

- The same _set_ of files (same names, same directory layout)
- Each file with the same _declared structural properties_: codec, container, pix_fmt, profile/level, resolution, frame rate, bit depth, HDR metadata tags, track count, subtitle disposition, edge-case flag behavior
- `build_manifest.json` containing the same `matrix_sha256` (canonicalized JSON of the matrix)

**Explicitly not guaranteed**: byte-for-byte identical encoded outputs. x264 / x265 / SVT-AV1 are multi-threaded by default; muxer timestamps include write time; `encoder` tags leak ffmpeg build strings. Forcing bit-identity would roughly triple build time (`-threads 1` + `sliced-threads=0`) and still break across ffmpeg versions, for no downstream benefit.

**Enforcement** — after every encode, `_verify_contract(plan, probe)` in `generate.py` asserts:

- `probe_target.exists()` and is non-empty
- video pix_fmt matches declared `video.pix_fmt` (when declared)
- video codec family (from probe) matches the codec family implied by the encoder name (`libx264` → `h264`, `libsvtav1` → `av1`, etc.)
- video-only variants have no audio streams; audio-only variants have no video streams
- multi-audio / multi-subtitle variants have the correct track counts

Any mismatch fails the build with a specific error. `verify.py` re-runs the same contract post-hoc, plus the decode pass and bitstream assertions, so drift introduced _after_ the build (manual edits, partial copies) is caught too.

---

## NFO schema

Sibling to every media file (`foo.mp4` → `foo.nfo`). UTF-8 XML. Two halves:

### Top half — Kodi-compatible `<movie>` schema

Scrapers (Plex, Kodi, Jellyfin, Emu) read these fields directly and recognize the library without any adapter:

```xml
<?xml version="1.0" encoding="utf-8"?>
<movie xmlns:testlib="https://plexapp.com/testlib/v1">
  <title>H.264 MP4 Baseline Profile</title>
  <plot>H.264 baseline profile in MP4 with AAC stereo...</plot>
  <outline>...</outline>
  <uniqueid type="testlib" default="true">h264_mp4_baseline</uniqueid>
  <tag>h264_mp4_baseline</tag>
  <tag>mp4</tag>
  <runtime>1</runtime>
  <fileinfo>
    <streamdetails>
      <video>
        <codec>h264</codec>
        <aspect>1.7778</aspect>
        <width>1280</width>
        <height>720</height>
        <durationinseconds>10</durationinseconds>
        <framerate>24.000</framerate>
      </video>
      <audio>
        <codec>aac</codec>
        <language>und</language>
        <channels>2</channels>
      </audio>
      <!-- <subtitle> entries when present -->
    </streamdetails>
  </fileinfo>
  ...
```

### Bottom half — `<testlib>` custom extension

Test-library metadata that standard scrapers ignore but your own tooling reads:

```xml
  <testlib:testlib>
    <id>h264_mp4_baseline</id>
    <generator>ffmpeg</generator>             <!-- "external" for external samples -->
    <ffmpeg_version>8.1</ffmpeg_version>
    <command>-i /abs/path/source_master.mov -i /abs/path/source_audio.wav ...</command>

    <container>
      <format>mov,mp4,m4a,3gp,3g2,mj2</format>
      <duration>10.000000</duration>
      <size>2130759</size>
      <bitrate>1704607</bitrate>
    </container>

    <video_details codec="h264" profile="Constrained Baseline" level="31"
                   pix_fmt="yuv420p" bit_depth="8"
                   color_primaries="" color_transfer="" color_space=""
                   fps="24.000" width="1280" height="720"/>

    <audio_tracks>
      <audio index="0" codec="aac" channels="2" sample_rate="48000" bit_rate="127811"
             language="und" default="true"/>
    </audio_tracks>

    <subtitle_tracks>
      <!-- for embedded-subtitle variants:
      <subtitle index="0" codec="subrip" language="eng" default="true"/>
      -->
    </subtitle_tracks>

    <hdr kind="hdr10" color_primaries="bt2020" color_trc="smpte2084" colorspace="bt2020nc"/>

    <edge_case_flags>
      <flag>fragmented_mp4</flag>
      <!-- one <flag> per entry in the variant's `flags` array -->
    </edge_case_flags>

    <sources>
      <source kind="source_video" file="source_master.mov" sha256="3c3fc44a..."/>
      <source kind="source_audio" file="source_audio.wav" sha256="03534880..."/>
      <source kind="source_subtitle" file="source_sub.ass" sha256="8c1702db..."/>
      <!-- for external variants, a single <source kind="external" ...> with license_note -->
    </sources>

    <output sha256="2ac4a00d..." size="2130759"/>
  </testlib:testlib>
</movie>
```

Key properties:

- `uniqueid` always matches the filename basename — `verify.py` asserts this.
- `<command>` captures the exact ffmpeg args (minus the leading `ffmpeg`) — reproducing an individual file is just paste-and-run.
- `<sources>` holds SHA-256 of every source input, so if someone swaps a source, you can detect it by comparing the NFO against the actual file.
- `<output>` SHA lets you detect post-build tampering / partial copies.
- `<hdr>` is omitted when `video.hdr` is absent from the matrix entry.

---

## manifest.json schema

Flat, machine-readable library index. Consumed by downstream test automation that wants to pick variants by tag without parsing XML.

```json
{
  "_meta": {
    "generated_at": "2026-04-23T21:39:04.621+00:00",
    "ffmpeg_version": "8.1",
    "matrix_sha256": "a1b2c3...",
    "entry_count": 53
  },
  "entries": [
    {
      "name": "h264_mp4_baseline",
      "section": "video",
      "title": "H.264 MP4 Baseline Profile",
      "description": "...",
      "path": "generated/video/h264_mp4_baseline.mp4",
      "container": "mp4",
      "flags": [],
      "status": "built",
      "size": 2130759,
      "sha256": "2ac4a00d...",
      "format": "mov,mp4,m4a,3gp,3g2,mj2",
      "duration_sec": 10.0,
      "bitrate_bps": 1704607,
      "video": {
        "codec": "h264",
        "profile": "Constrained Baseline",
        "pix_fmt": "yuv420p",
        "width": 1280,
        "height": 720,
        "fps": 24.0,
        "bit_depth": "8",
        "color_primaries": null,
        "color_transfer": null,
        "color_space": null
      },
      "audio_tracks": [
        {
          "codec": "aac",
          "channels": 2,
          "sample_rate": 48000,
          "bitrate_bps": 127811,
          "language": "und",
          "title": null,
          "default": true,
          "forced": false
        }
      ],
      "subtitle_tracks": [],
      "hdr": null
    },
    {
      "name": "external_dolby_vision_p5",
      "section": "external",
      "status": "pending_external",
      "source_path": "sources/external/dolby_vision_p5.mp4",
      "license_note": "Cannot be synthesized with stock ffmpeg..."
    }
  ]
}
```

Selecting all variants with a tag:

```python
import json
m = json.load(open("generated/manifest.json"))
hdr_variants = [e for e in m["entries"] if "hdr" in (e.get("flags") or []) or e.get("hdr")]
```

---

## verify_report.json schema

```json
{
  "summary": { "ok": 46, "pending_external": 7 },
  "total": 53,
  "variants": [
    {
      "name": "h264_mp4_baseline",
      "section": "video",
      "path": "generated/video/h264_mp4_baseline.mp4",
      "status": "ok",
      "checks": {
        "exists": true,
        "decode": true,
        "probe": true,
        "contract": true,
        "nfo.exists": true,
        "nfo.parses": true,
        "nfo.uniqueid_match": true
      },
      "errors": [],
      "warnings": []
    },
    {
      "name": "edge_fragmented_mp4",
      "section": "edge",
      "path": "generated/edge/edge_fragmented_mp4.mp4",
      "status": "ok",
      "checks": {
        "exists": true,
        "decode": true,
        "probe": true,
        "contract": true,
        "nfo.exists": true,
        "nfo.parses": true,
        "nfo.uniqueid_match": true,
        "fragmented_mp4.has_moof": true
      }
    }
  ]
}
```

`status` is `"failed"` if any error was recorded, `"pending_external"` for unfilled external slots, `"ok"` otherwise. `checks` keys are dynamic — the presence of a key indicates that check ran; its value indicates pass (`true`) or fail (`false`).

---

## build_manifest.json schema

Reproducibility metadata written at the end of every `generate.py` run.

```json
{
  "built_at": "2026-04-23T21:39:02.123+00:00",
  "ffmpeg_version": "8.1",
  "python_version": "3.13.1",
  "platform": "Darwin 25.3.0 arm64",
  "matrix_sha256": "a1b2c3...",
  "entry_counts": {
    "generated": 0,
    "skipped": 46,
    "pending_external": 7
  }
}
```

`matrix_sha256` is the SHA-256 of the canonical (sort-keys, no whitespace) JSON of `config/media_matrix.json`. If two builds share the same `matrix_sha256` + `ffmpeg_version`, the declared library shape is guaranteed identical.

---

## Extending the matrix

### Add a synthesizable video variant

1. Open `config/media_matrix.json`, append to `video_variants`:
   ```json
   {
     "name": "hevc_mkv_aac_1080p",
     "title": "HEVC MKV 1080p with AAC stereo",
     "description": "HEVC main 8-bit 1080p in Matroska with AAC stereo.",
     "container": "mkv",
     "width": 1920,
     "height": 1080,
     "video": { "codec": "libx265", "profile": "main", "pix_fmt": "yuv420p", "bitrate": "4000k", "preset": "medium" },
     "audio": { "codec": "aac", "bitrate": "192k", "channels": 2, "sample_rate": 48000 }
   }
   ```
2. `make check` — confirm the variant resolves and appears in the "video: 18" count.
3. `make generate VARIANT=hevc_mkv_aac_1080p` — build just this one.
4. `make verify VARIANT=hevc_mkv_aac_1080p` — confirm it passes decode + contract + NFO + bitstream assertions.
5. If everything's green, `make all` to refresh the thumbnail + manifest.

Supported per-variant fields:

| Field                                                                 | Required?   | Notes                                                                                                           |
| --------------------------------------------------------------------- | ----------- | --------------------------------------------------------------------------------------------------------------- |
| `name`                                                                | yes         | Unique identifier; must be a valid filename stem.                                                               |
| `title`, `description`                                                | recommended | Shown in NFO `<title>` / `<plot>`.                                                                              |
| `container`                                                           | yes         | Maps to output extension via `CONTAINER_EXT` in `generate.py`.                                                  |
| `width`, `height`                                                     | no          | Override default 1280×720.                                                                                      |
| `framerate`                                                           | no          | Override default 24.                                                                                            |
| `video.codec`                                                         | yes         | ffmpeg encoder name. `_common_video_args` handles SVT-AV1 idiosyncrasies.                                       |
| `video.profile`, `video.level`                                        | no          | Passed as `-profile:v` / `-level:v`.                                                                            |
| `video.pix_fmt`                                                       | yes         | e.g. `yuv420p`, `yuv420p10le`.                                                                                  |
| `video.bitrate`                                                       | recommended | Passed as `-b:v`; also used for `-maxrate` / `-bufsize` on codecs that support VBV.                             |
| `video.preset`                                                        | no          | Passed as `-preset`.                                                                                            |
| `video.tag`                                                           | no          | MP4 codec tag (`hvc1` for HEVC is a common choice).                                                             |
| `video.hdr`                                                           | no          | Dict with `color_primaries` / `color_trc` / `colorspace` / optional `x265_params` for HDR10 / HLG / SDR 10-bit. |
| `video.itsoffset`                                                     | no          | Input time offset in seconds (used for `edge_edit_list`).                                                       |
| `audio.codec`, `audio.bitrate`, `audio.channels`, `audio.sample_rate` | yes         | Passed as `-c:a` / `-b:a` / `-ac` / `-ar`.                                                                      |

### Add an audio-only variant

Same shape as above, in `audio_only_variants`, with no `video` block and the `container` set to a format that carries audio natively (`m4a`, `ogg`, `wav`, `flac`, `mp3`, `ac3`, `eac3`).

### Add a subtitle variant

In `subtitle_variants`:

- External (sidecar): `{name, format: "srt"|"webvtt"|"ass", embed: false, language}` — pipeline calls `build_subtitle_external` which converts the `source_sub.ass` to the target format.
- Embedded: `{name, format: "srt"|"ass"|"mov_text", embed: true, language, container, video: {...}, audio: {...}}` — pipeline calls `build_subtitle_embedded` which muxes a single subtitle track with language metadata alongside video + audio.

### Add a stream variant

In `stream_variants`:

```json
{
  "name": "hls_hevc_ac3",
  "type": "hls",
  "segment_duration": 4,
  "video": { "codec": "libx265", "pix_fmt": "yuv420p", "bitrate": "3000k", "preset": "medium" },
  "audio": { "codec": "ac3", "bitrate": "448k", "channels": 6, "sample_rate": 48000 }
}
```

`type` is `"hls"` or `"dash"`. The output goes into `generated/streams/<name>/` with a sibling NFO.

---

## Adding a new edge-case handler

Edge cases often need a special ffmpeg invocation that doesn't fit `build_video`. The flow:

1. Pick a _flag_ name for the new edge — e.g. `"negative_pts"`.
2. Add the entry to `edge_cases` in `config/media_matrix.json`:
   ```json
   {
     "name": "edge_negative_pts",
     "title": "Edge: Negative PTS",
     "description": "...",
     "container": "mkv",
     "flags": ["negative_pts"],
     "video": {...},
     "audio": {...}
   }
   ```
3. In `scripts/encoders.py`, add a branch to `build_edge`:
   ```python
   if "negative_pts" in flags:
       # construct the special ffmpeg invocation and return the arg list
       ...
   ```
4. In `scripts/verify.py`, extend `_bitstream_assertions` with a specific check:
   ```python
   if "negative_pts" in flags:
       vs = video_streams[0] if video_streams else {}
       start_pts = float(vs.get("start_time", 0))
       ok = start_pts < 0
       report.checks["negative_pts.start_time_negative"] = ok
       if not ok:
           report.errors.append(f"negative_pts: expected start_time < 0, got {start_pts}")
   ```
5. If the edge needs a _post_-encode transform (like truncation), extend `_execute` in `generate.py` with a post-process hook gated on the flag.

---

## External-sample workflow

External variants live in `config/media_matrix.json` under `external_variants`:

```json
{
  "name": "external_my_sample",
  "title": "...",
  "description": "...",
  "source_path": "sources/external/my_sample.mkv",
  "container": "mkv",
  "flags": ["external", "my_tag"],
  "license_note": "Where it came from; redistribution constraints."
}
```

The pipeline:

1. Looks for the file at `source_path`.
2. If missing: reports `pending_external` status, writes nothing, continues.
3. If present: copies to `generated/external/<name>.<ext>`, probes, verifies the contract, writes a sibling NFO whose `<source>` block records `sha256` of the external file + the `license_note`.

Contract verification for externals is looser — we don't typically declare exact codec fields for them (their whole point is that ffmpeg doesn't know what to expect). The probe output still drives the NFO so metadata _is_ recorded, just not asserted.

To add a new external slot: drop the file in `sources/external/`, append an `external_variants` entry, run `make check` to confirm it appears, then `make generate --only external`. Document the slot in `sources/external/EXTERNAL_SAMPLES.md`.

---

## FFmpeg quirks handled by the pipeline

Non-obvious things the code accounts for.

| Quirk                                                                                              | Where handled                                                                                                          | Why                                                                                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| User-supplied source files accidentally overwritten by duration-aware regen                        | `bootstrap._is_synthetic` + `sources/.synthetic_sources.json`                                                          | The sentinel records which files were produced by our bootstrap. Only those are refreshed when duration changes; real user assets get a warning if their duration mismatches the target but are never touched.                                        |
| Default 600 s ffmpeg timeout kills libx265 / libvpx-vp9 encodes at long durations                  | `generate._ffmpeg_timeout_for(defaults)` and `verify._decode_timeout_for(dur)`                                         | Both scale linearly with `defaults.duration_seconds`. Generator uses `max(600, dur * 10)` (50 min ceiling for 300 s content), verifier uses `max(120, dur * 2)`. Timeouts also raise a clean `FFExecError` now (was bubbling up as `TimeoutExpired`). |
| `libsvtav1` rejects `-maxrate` / `-bufsize` / `-keyint_min`                                        | `encoders._common_video_args`                                                                                          | Emits VBV flags only for codecs that support them.                                                                                                                                                                                                    |
| ffmpeg 8.1 has no DASH demuxer — ffprobe can't parse `.mpd`                                        | `generate._probe_dash` + `verify._decode_smoke_dash`                                                                   | Probes each `init-*.m4s` segment and merges streams; decode smoke pipes `cat init … chunks` into ffmpeg.                                                                                                                                              |
| Multi-channel (5.1) audio source synthesis                                                         | `encoders._audio_src_for_channels`                                                                                     | Stereo master doesn't satisfy a 6-channel encode — uses `aevalsrc` with `channel_layout=5.1` when channels > 2.                                                                                                                                       |
| HEVC in MP4 needs `hvc1` tag for Apple-stack playback                                              | `video.tag` in config, `_common_video_args`                                                                            | Opt-in via `-tag:v hvc1`.                                                                                                                                                                                                                             |
| HDR10 static metadata must be encoded into the bitstream, not just tagged                          | `video.hdr.x265_params` in config                                                                                      | `master-display` + `max-cll` go through libx265 params; ffmpeg container tags alone aren't sufficient.                                                                                                                                                |
| `edge_truncated` with default MP4 moov-at-end would render the file unprobable after truncation    | `encoders.build_edge` adds `-movflags +faststart`                                                                      | Header survives truncation; decode still fails partway.                                                                                                                                                                                               |
| Contract verify for truncated files would fail because ffprobe on a truncated file may drop fields | `generate._execute` relaxes contract when `truncated` flag is set                                                      | Expected: the file is intentionally broken.                                                                                                                                                                                                           |
| Thumbnail seek to t=5 fails on truncated/broken-PTS files                                          | `thumbnails._extract_thumb` has a t=0 fallback                                                                         | Ensures contact sheet still includes edge variants.                                                                                                                                                                                                   |
| HDR thumbnails washed out without tone-mapping                                                     | `thumbnails._extract_thumb` does a colorspace convert via `scale=out_color_matrix=bt709` when variant has HDR metadata | `zscale` (for a proper HDR→SDR tonemap) isn't always compiled in; the simpler path works everywhere.                                                                                                                                                  |
