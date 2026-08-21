# Media Test Library Generator

Generates a deterministic media test library for validating a professional playback pipeline (Plex-style server ingest / transcode / direct-play). Takes a small set of high-quality source assets and fans them out into a wide matrix of codec / container / edge-case combinations, each with a sibling Kodi-compatible `.nfo` carrying test metadata.

Everything is declared in one config file (`config/media_matrix.json`) and produced by one set of scripts. Re-running skips entries that already exist; pending-external slots are reported but never block the build.

## Documents

- **This file** — overview, requirements, quickstart, CLI cheat sheet, output layout.
- **[docs/MATRIX.md](docs/MATRIX.md)** — full catalog of every variant, what it is, what it tests, how it's constructed.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — pipeline flow, NFO / manifest / verify schemas, determinism model, extending the matrix.
- **[sources/external/EXTERNAL_SAMPLES.md](sources/external/EXTERNAL_SAMPLES.md)** — the licensed / proprietary sample slots (DV, HDR10+, DTS-HD MA, TrueHD/Atmos, …).

## Requirements

- **ffmpeg 6.0+** (tested on 8.1) with these encoders: `libx264`, `libx265`, `libsvtav1`, `libvpx-vp9`, `libopus`, `libmp3lame`, `aac`, `ac3`, `eac3`, `flac`, `pcm_s16le`. Check with `ffmpeg -encoders | grep -E 'libx264|libx265|libsvtav1'`.
- **An ffmpeg built with `libfreetype`** — required for the on-screen overlay, which uses the `drawtext` filter. The stock Homebrew `ffmpeg` formula **omits it**; install `ffmpeg-full` instead:

  ```bash
  brew install ffmpeg-full          # bottled, keg-only, does not replace your ffmpeg
  ffmpeg -h filter=drawtext         # "Unknown filter" means you have the wrong build
  ```

  `config/media_matrix.json` points at it via `defaults.ffmpeg_bin` / `ffprobe_bin`. Override per run with the `FFMPEG_BIN` / `FFPROBE_BIN` environment variables, or turn the overlay off entirely with `--no-burn-in`. The generator checks for `drawtext` before it starts and fails with a clear message rather than part-way through the matrix.
- **Python 3.9+** — standard library only, no third-party deps.
- **~8 GB** of disk space for the default 5-minute full build. (At `--duration 60` it's ~1.5 GB; at `--duration 10` it's ~200 MB.)
- Optional: **GNU Make** for the convenience targets in `Makefile`.

## Quickstart

```bash
# Bootstrap synthetic sources if you don't have real ones, then build everything
make all

# Equivalent explicit invocation
python3 -m scripts.generate --bootstrap --jobs 4
python3 -m scripts.verify
python3 -m scripts.manifest
python3 -m scripts.thumbnails
python3 -m scripts.library
```

**Default duration is 5 minutes** (300 s). Use `--duration SECONDS` or `--duration-minutes MINUTES` to override, e.g. `python3 -m scripts.generate --duration 30` for a fast CI/smoke build. Changing the duration regenerates the synthesized sources and invalidates any existing outputs (via mtime comparison) so the whole library stays consistent.

The full 5-minute build takes a few minutes with `JOBS=4`. A 30-second build completes in ~30 seconds. Incremental re-runs at the same duration skip everything that already exists.

## Matrix at a glance

| Section   | Count | What it covers                                                                                                                                                                                                                                                    |
| --------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| video     | 48    | **Chapters (4)** · H.264 / HEVC / AV1 / VP9 / **MPEG-2** / **MPEG-4 ASP (Xvid)** / **WMV2** · in MP4/MKV/TS/WebM/**AVI**/**WMV** · 8-bit + 10-bit · SDR / HDR10 / HLG · 24/25/29.97/30/60 fps · 16:9 / 4:3 / 9:16 / **anamorphic 64:45** · **progressive + 1080i + 480i interlaced** |
| audio     | 18    | AAC (mono/stereo/5.1/**7.1**), AC-3 5.1, E-AC-3 5.1, MP3, Opus, FLAC (16-bit + **24-bit 96k / 192k**), PCM (s16le stereo → **s24le stereo** → **7.1.4** → **9.1.4 Atmos-style bed**), **MP2**, **WMA2**                                                           |
| subtitles | 6     | External SRT / WebVTT / ASS · embedded SRT / ASS / mov_text                                                                                                                                                                                                       |
| streams   | 2     | HLS (m3u8 + TS segments) · DASH (mpd + fMP4 chunks)                                                                                                                                                                                                               |
| edge      | 25    | No audio, audio-only, multi-audio, multi-subtitle (with forced flag), VFR, broken PTS, low/high bitrate, Annex B, AVCC, fragmented MP4, missing metadata, edit list, truncated file, **6 malformed-chapter cases**                                                                               |
| external  | 10    | Slots for Dolby Vision P5, DV P8.1, HDR10+, DTS-HD MA, TrueHD/Atmos, E-AC-3 JOC, real-world 4K HEVC, **VC-1 Blu-ray** — must be dropped in manually                                                                                                               |

**Total: 109 declared variants · 99 ffmpeg-synthesizable · 10 external slots awaiting user-supplied files.**

See [docs/MATRIX.md](docs/MATRIX.md) for the full catalog with rationale per entry.

## CLI cheat sheet

All commands support `--only SECTION[,SECTION…]` and `--variant NAME[,NAME…]` to restrict scope.

| Command                                           | Purpose                                                                                                                                  |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `python -m scripts.generate`                      | Build the library. Skips existing outputs.                                                                                               |
| `python -m scripts.generate --check`              | Dry-run status report — what would be built, what's pending external.                                                                    |
| `python -m scripts.generate --dry-run`            | Print every ffmpeg command without executing.                                                                                            |
| `python -m scripts.generate --force`              | Rebuild every variant from scratch (ignores existing outputs).                                                                           |
| `python -m scripts.generate --bootstrap`          | Synthesize missing source assets under `sources/`.                                                                                       |
| `python -m scripts.generate --duration SECONDS`   | Override target output length. Regenerates sources + invalidates outputs if the duration has changed since the last build.               |
| `python -m scripts.generate --duration-minutes N` | Same as `--duration`, but in minutes. `--duration-minutes 5` == `--duration 300`.                                                        |
| `python -m scripts.generate --jobs N`             | Run N ffmpeg workers in parallel (default 1).                                                                                            |
| `python -m scripts.generate --no-burn-in`         | Build without the on-screen PTS / media-info overlay, for one run.                                                                       |
| `python -m scripts.generate --refresh-nfo`        | Rewrite NFOs from existing outputs without re-encoding. For metadata-only fixes.                                                          |
| `python -m scripts.library`                       | Build the faceted show tree under `library/`. Run after every generate.                                                                   |
| `python -m scripts.servercheck`                   | Diff what Plex and Jellyfin scanned against the manifest. Needs `make servers` first.                                                     |
| `python -m scripts.plexsetup --claim TOKEN`       | Create the three Plex libraries and claim the server so Plex apps can find it.                                                            |
| `python -m scripts.verify`                        | Post-creation validation — decode smoke test, contract re-check, NFO parse, bitstream assertions. Writes `generated/verify_report.json`. |
| `python -m scripts.verify --no-decode`            | Skip the decode-only pass (fastest path if you just want structural checks).                                                             |
| `python -m scripts.manifest`                      | Emit `generated/manifest.json` — flat machine-readable index.                                                                            |
| `python -m scripts.thumbnails`                    | Extract per-variant thumbnails + composite `contact_sheet.jpg`.                                                                          |

Or via Make (adds `JOBS=N` / `ONLY=…` / `VARIANT=…` support):

```bash
make help                       # list all targets
make bootstrap                  # synthesize sources + print what would be built
make generate JOBS=4            # parallel build
make generate ONLY=video,edge   # restricted scope
make generate VARIANT=hevc_mp4_10bit_hdr10_bt2020
make verify                     # full validation
make manifest                   # write manifest.json
make thumbs                     # extract thumbnails
make all                        # generate + verify + manifest + thumbs
make rebuild                    # rm -rf generated/ then build from scratch
make clean                      # also remove synthesized source assets
```

## Output layout

After `make all`:

```
generated/
├── video/
│   ├── h264_mp4_baseline.mp4
│   ├── h264_mp4_baseline.nfo       ← sibling Kodi-compatible NFO
│   ├── hevc_mp4_10bit_hdr10_bt2020.mp4
│   ├── hevc_mp4_10bit_hdr10_bt2020.nfo
│   └── …                            (48 video variants total)
├── audio/                           (14 audio-only variants)
├── subtitles/                       (6 subtitle variants, mix of external + embedded)
├── streams/
│   ├── hls_h264_aac/
│   │   ├── playlist.m3u8
│   │   └── seg_000.ts seg_001.ts seg_002.ts
│   ├── hls_h264_aac.nfo             ← sibling to the stream directory
│   ├── dash_h264_aac/
│   │   ├── manifest.mpd
│   │   ├── init-0.m4s init-1.m4s
│   │   └── chunk-0-00001.m4s … chunk-1-00003.m4s
│   └── dash_h264_aac.nfo
├── edge/                            (25 edge-case variants)
├── external/                        (0–10 variants, depending on which external samples you've dropped in)
├── _thumbs/
│   ├── h264_mp4_baseline.jpg        ← one thumbnail per video at t=5s
│   ├── …                            (50 thumbnails total for the full matrix)
│   └── contact_sheet.jpg            ← 4-col grid of all thumbnails
├── manifest.json                    ← flat machine-readable index of every entry
├── verify_report.json               ← pass/fail per variant with specific check results
└── build_manifest.json              ← reproducibility: ffmpeg version, matrix SHA, built-at, platform
```

Every media file has a **sibling `.nfo`** with the same basename. Kodi/Plex/Jellyfin scrapers read the top-half `<movie>` schema; custom test tooling reads the `<testlib:testlib>` extension for edge flags, the ffmpeg command used, source/output SHAs, and HDR details. See [docs/ARCHITECTURE.md#nfo-schema](docs/ARCHITECTURE.md#nfo-schema) for the full schema.

## On-screen overlay

Every generated video carries two burned-in text blocks, so a single screenshot tells you which file you are looking at and exactly where in the timeline you are.

**Top-left — per-frame timing**, burned into `source_master.mov` once and inherited by every variant:

```
PTS 7.000000  FRAME 168
TC  00:00:07.000
```

**Bottom-left — per-file identity**, drawn during each variant's encode:

```
h264_mp4_baseline
h264 / mp4 / 1280x720@24 / aac 2ch
```

What this buys you: read the burned PTS off a paused frame and compare it against the player's reported position to catch seek drift; confirm from a screenshot that the server served the variant you asked for rather than a transcode of a different one.

The overlay is drawn **before** interlacing and before any `setsar`, so it is never combed by field interleaving nor stretched by an anamorphic display aspect. On the three timestamp edge cases it deliberately shows the *pre-remap source* time, which is what makes the defect visible:

| Variant | What the overlay reveals |
| --- | --- |
| `edge_edit_list` | The 0.5 s `itsoffset` shows as a gap between burned PTS and player position. |
| `edge_variable_fps` | The `setpts` ramp is visible frame by frame against a linear source clock. |
| `edge_broken_pts` | The injected 5 s jump appears as a discontinuity the burned clock does not share. |

Configure it under `defaults.overlay` in `config/media_matrix.json` (`enabled`, `font_file`, `font_size_ratio`, `font_color`, `box_color`, `margin_ratio`). Disable it for one run with `--no-burn-in`, or for a single variant with `"burn_in": false` on that matrix entry. Variants with no video stream — audio-only, external subtitles — and the verbatim `external_*` copies never receive it.

> `verify` compares each NFO's recorded overlay state against what the matrix declares, so a library built with `--no-burn-in` while the config still says `"enabled": true` is reported as a mismatch — that is the check working. To turn the overlay off permanently, set `defaults.overlay.enabled` to `false` rather than relying on the flag.

## Custom source imagery

Drop image files into `sources/images/` and the synthetic master is built from them instead of the `testsrc2` gradient, so the library exercises realistic picture detail:

```bash
cp ~/my_test_charts/*.png sources/images/
python3 -m scripts.generate --bootstrap --force
```

Each image is scaled to fit, padded to the master resolution (aspect preserved, no distortion), held for `defaults.image_hold_seconds` (default 5), and the set is cycled until the target duration is filled. Supported: PNG, JPEG, WebP, BMP, TIFF. Order is by filename, so prefix with `01_`, `02_` to control sequence.

`defaults.image_mode` controls this: `auto` (default — use images when present, otherwise the test pattern), `slideshow` (require images, warn and fall back if the directory is empty), or `none` (always the test pattern).

Changing the image set changes the master's picture without changing its duration, so the set is fingerprinted by name and size in `sources/.synthetic_sources.json`. Swap an image and the master regenerates, which cascades a rebuild of every downstream output through the existing mtime check. Each image's SHA-256 is recorded in every NFO, and the set fingerprint appears in `manifest.json` under `_meta.source_images_sha256`.

> Note: `testsrc2` draws its own small built-in counter. Custom imagery replaces that pattern, so the burned-in overlay above becomes the only on-screen clock — which is why `drawtext` is a hard requirement once you use your own images.

## Geometry and aspect ratio

Every variant's **display** aspect is asserted at verify time, because it is not the same thing as its coded shape.

> **The trap this library was caught by.** ffmpeg's `scale` filter rewrites SAR to preserve the *source* display aspect. Scaling a 16:9 master to 720×540 silently produces SAR 4:3 and **DAR 16:9** — not the 4:3 the dimensions imply. Three variants shipped for months claiming ratios they did not have. The chain now ends in `setsar=1` unless a variant declares `video.sar` deliberately, so a coded frame means what it looks like.

```
scale=720:540           ->  SAR 4:3, DAR 16:9    ← the bug
scale=720:540,setsar=1  ->  SAR 1:1, DAR 4:3     ← intended
```

Coverage now spans **2.39:1 scope, 1.85:1 flat, 2:1 Univisium, 16:9, 20:11, 11:9, 4:3, 1:1 square and 9:16 vertical**, where before 46 of 47 files displayed 16:9.

| Group | Variants |
| --- | --- |
| Cinema | `aspect_scope_239` (1920×804), `aspect_flat_185`, `aspect_univisium_200` |
| Square / vertical | `aspect_square_1_1`, `aspect_vertical_9_16` |
| **Baked-in bars** | `aspect_letterbox_239`, `aspect_pillarbox_43` — the frame reports 16:9 and only the picture is scope or 4:3, which is what auto-crop logic trips on |
| Anamorphic | `aspect_anamorphic_hdv` (SAR 4:3), `_dvcpro` (SAR 3:2), `_ntsc_wide` (SAR 40:33 → 20:11) |
| Rotation | `edge_rotate_90` / `_180` / `_270` — real display-matrix side data, as phones record portrait |
| Conflict | `edge_aspect_conflict` — coded 1280×720 but the container declares 4:3 |

Two implementation notes worth knowing. `-metadata:s:v rotate=` is inert in ffmpeg 8; rotation is stamped by a stream-copy remux with `-display_rotation` as a post-encode step. And ffprobe normalises rotation into −180…180, so 270 reports as `-90`.

## Cadence, chroma and colour

- **23.976 fps** (`h264_mp4_23976`) — the standard film rate, previously absent, plus 48 and 50 fps.
- **3:2 pulldown** (`mpeg2_ts_telecine`) — 23.976 film carried as 29.97 interlaced; needs inverse telecine to recover.
- **4:2:2 10-bit, 4:4:4 8-bit, 12-bit HEVC** — the library was otherwise entirely 4:2:0.
- **Full-range levels** (`h264_mp4_full_range`, which ffmpeg represents as `yuvj420p`) and **BT.601 matrix** (`h264_mp4_bt601`).

## Per-variant duration

A geometry or chroma test does not need five minutes. Any entry may declare `duration_seconds`, overriding `defaults.duration_seconds`:

```json
{ "name": "aspect_scope_239", "duration_seconds": 20, ... }
```

Geometry, chroma, colour and container variants build at 20 s; audio and A/V-sync ones at 60 s; everything else stays at the library default. A variant longer than the synthesized source is a hard error rather than a silently short file. Chapter specs scale to the variant's own duration, not the global one.

## Chapters

Ten variants carry chapter markers, written through an FFMETADATA sidecar kept at `generated/_chapters/<variant>.ffmeta` so the exact input is inspectable and the recorded ffmpeg command re-runs as-is.

**Well-formed** — `chapters_mp4_10`, `chapters_mkv_10` (same ten chapters in both container models), `chapters_mkv_unicode` (accented, Japanese, Arabic, Greek and emoji titles — all verified to round-trip), and `chapters_mkv_100` for chapter-list and scrubber density.

**Malformed** — the cases where playback bugs actually live. Each is a real muxer result, not a theory:

| Variant | Authored | What the muxer actually did |
| --- | --- | --- |
| `edge_chapters_nonzero_start` | 3, first at 10% | 3 kept; unchaptered head preserved |
| `edge_chapters_overlap` | 3 overlapping | 3 kept, overlaps intact |
| `edge_chapters_zero_length` | 3, one zero-length | **2 kept** — one was dropped |
| `edge_chapters_past_eof` | 2, last ends at 180% | 2 kept, end **clamped** to the duration |
| `edge_chapters_out_of_order` | 3 descending | **1 kept** — the rest discarded |
| `edge_chapters_stripped` | 5, then `-map_chapters -1` | 0 — strip path confirmed |

`verify` holds the well-formed set to exact count, titles, ordering and bounds. For the malformed set it records what happened as a *warning* rather than a failure, because a muxer is entitled to normalise nonsense — the finding is what it chose to do.

Declare chapters on any variant with a `chapters` block:

```json
{ "flags": ["chapters"],
  "chapters": { "mode": "even", "count": 10, "title_prefix": "Chapter" } }
```

`mode` is `even` (split the duration into N) or `explicit` (an `entries` list). Explicit times default to `unit: "fraction"` of the total duration, so a spec stays meaningful whether you build at 30 s or 300 s.

## Faceted library (`library/`)

`generated/` is organised by build stage, which describes how a file was made rather than what it tests. `make library` projects the same files into `library/`, where each capability is a TV show and every variant carrying it is an episode:

```
library/
├── Chapters/
│   ├── tvshow.nfo                                        <tvshow>
│   ├── poster.jpg                                        tiled from this show's episodes
│   └── Season 01/
│       ├── Chapters - S01E01 - chapters_mkv_10.mkv       hardlink
│       ├── Chapters - S01E01 - chapters_mkv_10.nfo       <episodedetails>
│       └── Chapters - S01E01 - chapters_mkv_10-thumb.jpg
├── H.264 Video/  HEVC Video/  AV1 Video/  …
├── MP4 Container/  MKV Container/  TS Container/  …
├── AAC Audio/  AC3 Audio/  FLAC Audio/  …
└── ASS Subtitles/  SRT Subtitles/  WebVTT Subtitles/
```

Currently **35 shows, 191 episodes**. Point your media server at `library/` — *not* at `generated/` as well, or every file appears twice.

**Episodes are hardlinks, so the tree is free.** One file carries several names: `chapters_mp4_10.mp4` has `nlink=5` — the original plus H.264 Video, MP4 Container, AAC Audio and Chapters. `generated/` alone is 7.6 GB; `generated/` and `library/` together are still 7.6 GB. Total non-media overhead is 52 KB of NFOs.

Hardlinks were chosen over symlinks because each path is a *real* file, so a server reading through a container bind mount or a network share resolves it — a symlink to a host path would dangle.

> **Re-run `make library` after every `make generate`.** ffmpeg usually rewrites a file in place, which keeps existing links live, but anything that replaces the inode (notably `make clean-generated` followed by a rebuild) leaves links pointing at the old content. Each run compares inodes and relinks whatever is stale, and `make all` ends with `library` so the normal path cannot drift.

Options: `--link-mode hardlink|symlink|copy`, `--facets codec,container,audio,subtitles,chapters`, `--min-episodes N` (15 shows currently have a single episode), `--no-posters`.

Episode numbers are stable. They are stored in `library/.episode_map.json`, so adding a variant that sorts alphabetically first takes the next free number rather than renumbering everything after it. The tool also records what it created in `library/.library_state.json` and prunes only those paths — it never deletes a file it did not write.

Streams are excluded: HLS and DASH outputs are directories, which cannot be hardlinked, and a `.mpd` is not a scraper-playable episode.

## Media server validation

`docker/compose.yml` runs Plex and Jellyfin against `library/`, and `scripts/servercheck.py` diffs what each one scanned against `generated/manifest.json`. The manifest is ground truth — it records what every file measurably *is* — so any disagreement is a finding rather than a guess.

```bash
make servers        # start both, library/ mounted read-only
make servercheck    # diff both inventories against the manifest
make servers-down   # stop and wipe their config volumes
```

Plex is published on **32401**, not 32400, so a real Plex Media Server already running on the host is never disturbed. Both containers mount `library/` **read-only**, so neither can write metadata back into the generated tree. Config lives in named volumes, so `servers-down` returns both servers to a clean state.

No Plex account is required: the container runs unclaimed with `ALLOWED_NETWORKS`, which permits API access from the Docker networks. Jellyfin's setup wizard is driven over its API. Jellyfin's trickplay and chapter-image extraction are switched **off** at library creation — left on, it generates sprite sheets for every file, including the 100-chapter one.

Findings are sorted into four buckets so that a real defect is not buried:

| Bucket | Meaning |
| --- | --- |
| `missing` | the server should have indexed the file and did not |
| `mismatched` | indexed, but the technical detail disagrees with the manifest |
| `structural` | a consequence of the library's shape, not a defect — audio-only files are not indexed by a TV library |
| `notes` | documented server behaviour, e.g. Plex reports a coarse aspect bucket |

### What the first run found

| | Plex | Jellyfin |
| --- | --- | --- |
| Indexed | 76 / 97 | 79 / 97 |
| Technical mismatches | 0 | 0 |
| Genuinely missing | **1** — DNxHD in MXF | 0 |
| Audio-only not indexed | 20 | 18 |

Three things worth knowing, all confirmed against the running servers:

- **Plex will not ingest DNxHD in MXF; Jellyfin will.** A real capability difference between the two.
- **Plex's `aspectRatio` is a coarse bucket.** Across the entire 109-variant library it emits only four values: `1.33`, `1.78`, `1.85`, `2.35`. A 1:1 square file reports `1.33` and a 9:16 vertical file also reports `1.33`. Its `width`/`height` are exact, so those are what the check compares.
- **Both servers read chapters correctly**, including the malformed cases — Plex agrees that the out-of-order file has one chapter and the stripped file has none. Plex omits chapters unless the request asks for them with `?includeChapters=1`.

## Using the library from Plex apps

The validation harness above runs headless. To actually *play* the test files from a phone, TV or browser, the library needs a different shape — one TV library cannot reach all of it — and the server needs to belong to your account so apps can find it.

Everything is local and self-contained: each person runs their own server against their own build. Nothing is hosted, shared, or coupled to anyone else's account.

```bash
make generate && make library      # build the media and the library tree
make servers                       # start Plex (and Jellyfin) in docker
make plex-setup CLAIM=<token>      # create the libraries and claim the server
```

Get the claim token from [plex.tv/claim](https://plex.tv/claim) — it expires after about four minutes. Omit `CLAIM=` and the server still works over the local API, but Plex apps will not discover it.

`library/` is built as three roots, because Plex needs three library types to reach everything:

| Root | Plex library | Purpose |
| --- | --- | --- |
| `library/flat` | Other Videos | **One item per variant, named exactly.** The reliable way to play a specific test file. |
| `library/shows` | TV Shows | Faceted browsing — pick a capability, see everything that has it. |
| `library/audio` | Music | Audio-only variants as Artist/Album/Track. |

All three are hardlinks into `generated/`, so the extra trees cost no disk.

**Why a flat library exists.** Plex's TV scanner rewrites show names that look like they contain technical tokens, so `Widescreen 16x9` is filed as `Widescreen` and unrelated shows merge into it. A first attempt produced 4 shows out of 52 directories. Other Videos applies no such naming rules, so every variant appears exactly once under the name the matrix gave it. Use the faceted library to browse and the flat library to test.

### Reach

**92 of 97 built variants are playable from Plex apps.** The five that are not:

| Variant | Why |
| --- | --- |
| `sub_external_srt_eng`, `_webvtt_eng`, `_ass_eng` | Subtitle sidecars are not playable media in any server. They belong beside a video, not as items. |
| `dnxhd_mxf_pcm` | Plex will not ingest DNxHD in MXF. Jellyfin will — see the validation section. |
| `audio_eac3_51` | A raw E-AC-3 elementary stream is indexed by neither the Music nor the Video Files scanner. |

Two behaviours worth knowing when scripting against this, both learned the hard way:

- **Agent and scanner generations must match**, or the API answers `400 new scanner needs to be paired with new agent`. Confusingly, the modern `tv.plex.agents.none` pairs with the plainly-named scanners — `Plex Video Files`, not `Plex Video Files Scanner`.
- **A refresh issued immediately after creating a section is silently dropped.** The section reports `scannedAt` unset and stays empty forever. `plexsetup` waits, then refreshes, then polls until every section is idle.

### Claiming, and how to avoid claiming by accident

`ALLOWED_NETWORKS` grants unauthenticated access to the whole API — including `/myplex/claim`. An unclaimed server is therefore not a stable state if anything on the host can reach it: opening the web UI while signed in to Plex is enough to attach it to your account. That is fine when it is what you want, and `make plex-setup CLAIM=` makes it deliberate. If you would rather it stayed unclaimed, narrow `ALLOWED_NETWORKS` to the container subnet and avoid the web UI.

Server config persists under `docker/config/` (gitignored), so a claimed server keeps its libraries and settings across restarts. `make servers-down` stops the containers; `make servers-reset` wipes them back to nothing.

## Sources

By default, the generator auto-creates synthetic test-pattern sources under `sources/` if real ones aren't supplied:

- `sources/source_master.mov` — ProRes 4444 HQ, 1920×1080, 24 fps, matches configured duration (default 5 min). ffmpeg `testsrc2` pattern.
- `sources/source_audio.wav` — PCM 16-bit stereo, 48 kHz, matches configured duration. 440 Hz + 880 Hz sine mix.
- `sources/source_sub.ass` — ASS subtitle with **one cue per second** for the full duration. Each cue's text is `[HH:MM:SS] Cue N of TOTAL — timestamp sync sample`, which makes A/V/subtitle alignment visually verifiable at any point in playback.

If the configured duration changes (via `--duration` or editing `config/media_matrix.json`), the bootstrap code detects the mismatch (by ffprobing existing sources and counting subtitle cues) and regenerates whatever needs refreshing. The generator then rebuilds downstream outputs whose source mtime is newer than the output.

To use real sources, drop files with those exact names into `sources/` before running the generator — it only synthesizes what's missing (and won't overwrite a real file unless its duration deviates from the target).

External samples (DV, HDR10+, DTS-HD, etc.) go under `sources/external/`. See [sources/external/EXTERNAL_SAMPLES.md](sources/external/EXTERNAL_SAMPLES.md) for the seven pre-declared slots and how to add new ones.

## What each script produces

| Script                  | Output(s)                                                                            | What it's for                                                                                                                        |
| ----------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `scripts/generate.py`   | `generated/<section>/<name>.<ext>` + sibling `.nfo`; `generated/build_manifest.json` | Build the library from the matrix config.                                                                                            |
| `scripts/verify.py`     | `generated/verify_report.json`                                                       | Post-build validation — every file decodes, matches its declared contract, has a valid NFO, satisfies per-flag bitstream assertions. |
| `scripts/manifest.py`   | `generated/manifest.json`                                                            | Machine-readable library index for downstream test automation.                                                                       |
| `scripts/thumbnails.py` | `generated/_thumbs/*.jpg`, `contact_sheet.jpg`                                       | Visual sanity check — catches HDR tone-mapping / color-range / orientation bugs that probes don't see.                               |

## Determinism model

**Structural, not bit-identical.** Re-running the generator against the same matrix + same ffmpeg produces the same _set_ of files with the same _declared structural properties_ (codec, pix_fmt, resolution, track count, HDR metadata, flags). Byte-identity isn't guaranteed — encoders are multi-threaded by default, and muxer timestamps include write-time.

After each encode, the output is ffprobed and checked against an implicit contract derived from the matrix entry. If the encoder silently produces a different codec or pix_fmt, the build fails with a clear diff. `verify.py` re-runs the same contract check post-hoc, plus decode and bitstream assertions.

See [docs/ARCHITECTURE.md#determinism-model](docs/ARCHITECTURE.md#determinism-model).

## Extending the matrix

To add a new synthesizable variant, append an entry to the relevant array in `config/media_matrix.json`, run `make check` to confirm it resolves, then `make generate` to build it. To add a new external sample slot, see [sources/external/EXTERNAL_SAMPLES.md](sources/external/EXTERNAL_SAMPLES.md). Full walkthrough: [docs/ARCHITECTURE.md#extending-the-matrix](docs/ARCHITECTURE.md#extending-the-matrix).

## Troubleshooting

**`Unknown encoder 'libaom-av1'` / `libx265`** — your ffmpeg was built without the encoder. On macOS: `brew install ffmpeg` pulls in libx264/libx265/libsvtav1/libvpx-vp9/libopus/libmp3lame by default. Check with `ffmpeg -encoders | grep <name>`.

**`Invalid data found when processing input` on a DASH manifest** — ffmpeg 6+/8.1 has a DASH _muxer_ but no DASH _demuxer_. The pipeline already works around this by probing DASH init segments directly. If you see this error from a third-party tool, probe the `init-*.m4s` files rather than the `.mpd`.

**`pending external: external_*`** — expected and informational. Those entries are declared in the matrix but their source files aren't in `sources/external/`. Drop the files in and rerun, or leave them pending indefinitely — nothing else is blocked.

**Contract mismatch** — the build failed because ffmpeg produced a structurally different output than declared (e.g. wrong pix_fmt). Check the encoder flags in `config/media_matrix.json` for that variant, or re-read the ffmpeg stderr that preceded the error.

**VFR thumbnail or probe reports `avg_frame_rate=24/1`** — the `edge_variable_fps` variant produces per-frame PTS variation via a `setpts` filter, but x264 emits CFR-style packet timing so ffprobe's average may still report the nominal rate. The file _is_ variable at the source/packet level; probe deeper with `ffprobe -select_streams v -show_entries packet=pts_time` to see actual frame spacing.

## License / redistribution

Synthetic sources and generated outputs have no encumbrances (test patterns, sine waves, sample text). External samples under `sources/external/` are presumed to carry licensing / redistribution constraints and are `.gitignore`d by default — do not commit them to public repos.
