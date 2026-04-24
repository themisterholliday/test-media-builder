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
```

**Default duration is 5 minutes** (300 s). Use `--duration SECONDS` or `--duration-minutes MINUTES` to override, e.g. `python3 -m scripts.generate --duration 30` for a fast CI/smoke build. Changing the duration regenerates the synthesized sources and invalidates any existing outputs (via mtime comparison) so the whole library stays consistent.

The full 5-minute build takes a few minutes with `JOBS=4`. A 30-second build completes in ~30 seconds. Incremental re-runs at the same duration skip everything that already exists.

## Matrix at a glance

| Section   | Count | What it covers                                                                                                                                                                                                                                                    |
| --------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| video     | 22    | H.264 / HEVC / AV1 / VP9 / **MPEG-2** / **MPEG-4 ASP (Xvid)** / **WMV2** · in MP4/MKV/TS/WebM/**AVI**/**WMV** · 8-bit + 10-bit · SDR / HDR10 / HLG · 24/25/29.97/30/60 fps · 16:9 / 4:3 / 9:16 / **anamorphic 64:45** · **progressive + 1080i + 480i interlaced** |
| audio     | 14    | AAC (mono/stereo/5.1/**7.1**), AC-3 5.1, E-AC-3 5.1, MP3, Opus, FLAC (16-bit + **24-bit 96k / 192k**), PCM (s16le stereo → **s24le stereo** → **7.1.4** → **9.1.4 Atmos-style bed**), **MP2**, **WMA2**                                                           |
| subtitles | 6     | External SRT / WebVTT / ASS · embedded SRT / ASS / mov_text                                                                                                                                                                                                       |
| streams   | 2     | HLS (m3u8 + TS segments) · DASH (mpd + fMP4 chunks)                                                                                                                                                                                                               |
| edge      | 14    | No audio, audio-only, multi-audio, multi-subtitle (with forced flag), VFR, broken PTS, low/high bitrate, Annex B, AVCC, fragmented MP4, missing metadata, edit list, truncated file                                                                               |
| external  | 8     | Slots for Dolby Vision P5, DV P8.1, HDR10+, DTS-HD MA, TrueHD/Atmos, E-AC-3 JOC, real-world 4K HEVC, **VC-1 Blu-ray** — must be dropped in manually                                                                                                               |

**Total: 66 declared variants · 58 ffmpeg-synthesizable · 8 external slots awaiting user-supplied files.**

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
│   └── …                            (17 video variants total)
├── audio/                           (7 audio-only variants)
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
├── edge/                            (14 edge-case variants)
├── external/                        (0–7 variants, depending on which external samples you've dropped in)
├── _thumbs/
│   ├── h264_mp4_baseline.jpg        ← one thumbnail per video at t=5s
│   ├── …                            (34 thumbnails total for the full matrix)
│   └── contact_sheet.jpg            ← 4-col grid of all thumbnails
├── manifest.json                    ← flat machine-readable index of every entry
├── verify_report.json               ← pass/fail per variant with specific check results
└── build_manifest.json              ← reproducibility: ffmpeg version, matrix SHA, built-at, platform
```

Every media file has a **sibling `.nfo`** with the same basename. Kodi/Plex/Jellyfin scrapers read the top-half `<movie>` schema; custom test tooling reads the `<testlib:testlib>` extension for edge flags, the ffmpeg command used, source/output SHAs, and HDR details. See [docs/ARCHITECTURE.md#nfo-schema](docs/ARCHITECTURE.md#nfo-schema) for the full schema.

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
