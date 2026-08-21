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
├── ffutil.py                  # ffmpeg/ffprobe subprocess wrapper (timeout, structured errors, JSON parsing);
│                              #   binary resolution (FFMPEG_BIN / defaults.ffmpeg_bin) + filter capability checks
├── overlay.py                 # drawtext fragment builders for the on-screen overlay: escaping, geometry,
│                              #   per-frame source block (PTS/frame/timecode) and static per-variant block
├── chapters.py                # pure FFMETADATA chapter builder; single source of truth for what a
│                              #   chapters spec means, shared by the encoder and the verifier
├── library.py                 # projects manifest.json into library/ as faceted TV shows (hardlinks)
├── bootstrap.py               # synthesizes missing sources with ffmpeg lavfi or user images; duration- and
│                              #   signature-aware regeneration tracked via sources/.synthetic_sources.json
│                              #   sentinel (never touches user files)
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
├── images/                    # optional user-supplied stills; when present they drive the master's picture
├── source_master.mov          # test-pattern or slideshow video (synthesized if absent; ProRes, matches duration)
├── source_audio.wav           # test-tone audio (synthesized if absent; 440+880 Hz sine mix)
├── source_sub.ass             # ASS subtitle with one cue per second, each labeled [HH:MM:SS] Cue N of TOTAL
├── .synthetic_sources.json    # sentinel — marks which source files were produced by bootstrap
└── external/                  # user-supplied licensed samples (DV, HDR10+, DTS-HD, TrueHD, …)

library/                       # faceted, scraper-facing view built by library.py (hardlinks into generated/)
├── <Facet>/tvshow.nfo         #   one show per capability
├── <Facet>/Season 01/         #   episodes, each a hardlink plus an <episodedetails> NFO
├── .episode_map.json          #   persisted episode numbers so they never shift
└── .library_state.json        #   what this tool created, so pruning never touches anything else

Makefile                       # convenience targets; all wrap python3 -m scripts.<module>
```

Every script is directly runnable as `python -m scripts.<name>`. Nothing writes to the repo outside `generated/`, `sources/` (on `--bootstrap`), or `config/` (only when you edit it).

---

## Pipeline flow

### Generate (`scripts/generate.py`)

```
1. require_binaries()                                     # preconditions: ffmpeg + ffprobe present
2. _load_config(config/media_matrix.json)                 # parse matrix; configure_binaries() applies
                                                          #   defaults.ffmpeg_bin / ffprobe_bin
3. apply --duration / --duration-minutes if passed        # override defaults.duration_seconds
3b. _overlay_settings(cfg, --no-burn-in)                  # resolve overlay; require_filters("drawtext")
                                                          #   fails fast when the build cannot draw text
4. ensure_sources(sources/, duration, overlay, images)    # synthesize/refresh any synthetic source whose
                                                          #   duration or build signature is stale;
                                                          #   builds from sources/images/ when present;
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
     NFO <overlay> record matches what the matrix declares → checks.overlay.*
     (metadata only — that the text truly rendered into the pixels is confirmed
      visually through the thumbnails and contact sheet)
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

**Source-picture changes are tracked separately.** Duration is not enough to tell two masters apart — changing the overlay settings or swapping a file in `sources/images/` produces a master of identical length but different picture. `bootstrap.master_signature()` hashes the overlay config plus an image-set fingerprint (name and size per file) and stores it in `sources/.synthetic_sources.json`. A signature mismatch regenerates the master, whose new mtime then invalidates every downstream output through the normal skip check.

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

    <overlay enabled="true"
             source_fields="pts,frame,timecode" source_position="top-left"
             variant_fields="name,codec,container,resolution,fps,audio"
             variant_position="bottom-left"/>
    <!-- Variants that cannot carry an overlay record why:
         <overlay enabled="false" reason="no video stream"/>
         <overlay enabled="false" reason="external copy"/>
         <overlay enabled="false" reason="burn_in disabled for this variant"/> -->

    <edge_case_flags>
      <flag>fragmented_mp4</flag>
      <!-- one <flag> per entry in the variant's `flags` array -->
    </edge_case_flags>

    <sources>
      <source kind="source_video" file="source_master.mov" sha256="3c3fc44a..."/>
      <source kind="source_audio" file="source_audio.wav" sha256="03534880..."/>
      <source kind="source_subtitle" file="source_sub.ass" sha256="8c1702db..."/>
      <!-- one entry per file in sources/images/ when custom imagery drove the master -->
      <source kind="source_image" file="01_chart.png" sha256="9f2a71c0..."/>
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
    "source_images_sha256": "7d41ba90c2e15f38",
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
      "overlay": {
        "enabled": "true",
        "source_fields": "pts,frame,timecode",
        "variant_fields": "name,codec,container,resolution,fps,audio"
      },
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

## Chapters

Chapters are declared per variant and written as an FFMETADATA sidecar, then attached at mux time.

```
config: {"flags": ["chapters"], "chapters": {"mode": "even", "count": 10}}
   │
   ▼
generate._chapters_path_for(plan, defaults)      # writes generated/_chapters/<name>.ffmeta
   │                                             # via chapters.write_ffmetadata
   ▼
encoders.build_video(..., chapters_path)         # appends -i <sidecar> LAST, then
   │                                             # -map_metadata N -map_chapters N
   ▼
verify._chapter_assertions(plan, probe, report)  # re-resolves the same spec and compares
```

Three constraints shaped this design:

- **`encoders.py` stays pure.** The builders are documented as pure arg-builders, so the sidecar
  is written by `generate._execute` and only its path is passed in.
- **The metadata input must be appended last.** Every builder hardcodes `-map 0:v:0` / `-map 1:a:0`,
  and `build_edge` computes `-map {idx+1}:a:0` and `-map {idx+2}:s:0`. Inserting an input anywhere
  earlier would silently shift all of them. `_chapter_input` counts the `-i` occurrences already
  present rather than assuming an index.
- **`chapters.resolve()` is the single source of truth.** The encoder and the verifier both call
  it, so an assertion can never drift from what was authored.

`resolve()` deliberately does *not* validate ordering or bounds — the malformed variants exist to
produce chapters that overlap, invert, or run past the end of the file. Verification is therefore
split: well-formed variants are held to exact count, titles, monotonic starts and in-bounds ends,
while malformed ones record what the muxer did as a warning. Matroska drops a zero-length chapter
and discards out-of-order ones; MP4 clamps an end past EOF. Those are findings, not build failures.

---

## The faceted library (`scripts/library.py`)

A file is simultaneously h264, MP4, AAC and chaptered, so a tree of real files cannot express
capability without duplication. `library.py` reads `generated/manifest.json` — never the media —
and projects each entry into every facet it belongs to.

```
for entry in manifest.entries:            # built, non-stream entries only
    for facet in FACETS:                  # codec / container / audio / subtitles / chapters
        for (show_id, show_title) in facet(entry):
            shows[show_id].append(entry)
assign_episodes(shows, episode_map)       # stable numbers, never renumbered
for show: write tvshow.nfo, poster.jpg
    for episode: link media, write <episodedetails> NFO, link thumb
prune(previous_state - created)           # only paths this tool created
```

Key properties:

- **Hardlinks by default.** Same inode, so the tree adds no media bytes (`generated/` alone and
  `generated/` + `library/` both measure 7.6 GB), and each path is a real file that resolves
  through a container bind mount or network share where a symlink to a host path would not.
- **Stale-link detection.** ffmpeg normally truncates in place, which keeps links live, but
  anything that replaces the inode leaves a link serving the previous build. `_is_current()`
  compares inodes and relinks on mismatch.
- **Titles are qualified per dimension** — "AC3 Audio" and "AC3 Container", not two shows both
  called "AC3". `collect_shows` raises if two facets ever resolve to the same directory, because
  they would merge episodes and overwrite each other's `tvshow.nfo`.
- **Episode NFOs reuse the movie schema.** `build_episode_nfo` delegates to `build_nfo` with a
  different root tag and head, so stream details and the `<testlib>` provenance block are
  identical across both. Stream details are reconstructed from the manifest by
  `probe_from_entry()` rather than re-probing 190-odd files.
- **Pruning is conservative.** Only paths listed in `.library_state.json` are removed, so a file
  a person placed in `library/` is never deleted.


---

## Geometry model

Display aspect is a product of the coded frame *and* the pixel aspect, so the pipeline treats them
separately:

```
variant.width x variant.height     the coded frame
video.sar                          opt-in non-square pixels (anamorphic)
video.pad_to                       a larger frame around the picture (baked letterbox/pillarbox)
video.aspect                       container-level display aspect, can deliberately contradict
video.rotate                       display matrix, applied post-encode
```

`_build_vf_chain` composes them in a fixed order — `scale` → `pad` → overlay → `telecine` →
interlace → `setsar` — and always terminates in a SAR, defaulting to `1` so square pixels are the
norm and anamorphic is a deliberate declaration.

`verify._geometry_assertions` then closes the loop: a variant declaring `sar` must probe with that
SAR; a variant declaring only dimensions must probe with a display aspect matching them; a variant
declaring `rotate` must carry the corresponding display matrix. Without those checks the entire
dimension can be inert without anyone noticing, which is exactly what happened.

## Per-variant duration

`encoders.effective_duration(variant, defaults)` is the single source of truth, consumed by
`_duration_flag`, by `generate._chapters_path_for` (so a short chaptered file gets chapters spanning
*its* duration) and by the chapter assertions in `verify`. A variant may not exceed the synthesized
source duration — `generate._execute` fails the build rather than emitting a short file.

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
| `burn_in`                                                             | no          | Set to `false` to skip the on-screen overlay for this variant. The NFO records the opt-out and the reason.      |

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
| `scale` silently rewrites SAR to preserve the source display aspect, so coded dimensions do not mean what they look like | `encoders._build_vf_chain` now ends in `setsar=1` unless `video.sar` is declared | Three variants shipped claiming 4:3 / 9:16 while displaying 16:9. Square pixels are now the default and anamorphic is opt-in. |
| `-metadata:s:v rotate=` writes no side data in ffmpeg 8                                            | `generate._apply_rotation` remuxes with input-side `-display_rotation` after the encode                                | It is the only route that produces a real display matrix. The temp file must keep the original extension or ffmpeg cannot select a muxer. |
| ffprobe normalises rotation into -180..180                                                         | `verify._geometry_assertions` compares against `((deg + 180) % 360) - 180`                                             | A declared 270 probes as -90 and 180 as -180; a naive equality check would fail on correct files. |
| `-color_range pc` changes the pixel format to `yuvj420p`                                           | the matrix declares `yuvj420p` for the full-range variant                                                              | The contract check compares declared against probed pix_fmt, so declaring `yuv420p` failed the build. |
| Thumbnails ignored pixel aspect, so anamorphic tiles were geometrically wrong                      | `thumbnails._extract_thumb` prepends `scale=iw*sar:ih`                                                                 | The contact sheet misrepresented exactly the variants that exist to test geometry. |
| Reading the head of the container to detect Annex B framing                                        | `verify._is_annexb` checks for an `avcC` box, then scans 64 KiB for start codes                                        | In MPEG-TS the first bytes are packet framing (sync 0x47), so a correct Annex B file looked like a failure. Demuxing to raw h264 is equally wrong: ffmpeg applies `h264_mp4toannexb` on the way out, so even an AVCC source comes back Annex B. |
| `verify` ran its decode smoke against whatever `ffmpeg` was on PATH                                | `_decode_smoke` and `_decode_smoke_dash` now use `ffutil.ffmpeg_bin()`                                                 | Every other entry point honoured `defaults.ffmpeg_bin`; verify silently did not, so it could test a different build than the one that produced the files. |
| `defaults.source_audio` was declared but ignored — the filename was hardcoded in five places       | `generate._source_names()` plus a `defaults` argument to `encoders._audio_src_for_channels`                            | `source_video` and `source_subtitle` were honoured, so the inconsistency was invisible until someone changed the audio value and nothing happened. |
| User-supplied source files accidentally overwritten by duration-aware regen                        | `bootstrap._is_synthetic` + `sources/.synthetic_sources.json`                                                          | The sentinel records which files were produced by our bootstrap. Only those are refreshed when duration changes; real user assets get a warning if their duration mismatches the target but are never touched.                                        |
| Stock Homebrew `ffmpeg` has no `drawtext` — the formula was split, text/subtitle libs moved to `ffmpeg-full` | `ffutil.require_filters` preflight; `defaults.ffmpeg_bin` in the matrix                                       | Without a capability check the build dies part-way through the matrix on an opaque filter error. The preflight names the missing filter and the exact install command before any encode starts.                                                       |
| Overlay text combed by interlacing, or stretched by anamorphic SAR                                 | `encoders._build_vf_chain` inserts drawtext after `scale`, before `fps`/`tinterlace`/`setfield`/`setsar`               | Filter order is the entire fix. Drawn any later, the text is field-interleaved or squeezed to the display aspect.                                                                                                                                     |
| Two edge builders hand-roll their filter chain and bypass `_build_vf_chain`                        | `encoders.build_edge` patches `variable_fps` and `broken_timestamps` separately                                        | Both put the overlay before their `setpts`, so the burned value is the true pre-remap source time — which is what those variants exist to expose.                                                                                                     |
| Changing the overlay settings or the image set yields a file of identical duration                 | `bootstrap.master_signature` + the `signature` field in `.synthetic_sources.json`                                      | The duration check cannot see the difference, so the master would never regenerate and the change would silently not apply.                                                                                                                          |
| `_meta.matrix_sha256` hashes the config, so a swapped source image leaves provenance unchanged      | `manifest._meta.source_images_sha256`                                                                                  | Images live outside the matrix. Without a separate fingerprint two visually different libraries would claim identical provenance.                                                                                                                    |
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
