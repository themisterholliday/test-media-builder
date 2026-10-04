# Media Test Library Generator

Generates a deterministic media test library for validating a professional playback pipeline (Plex-style server ingest / transcode / direct-play). Takes a small set of high-quality source assets and fans them out into a wide matrix of codec / container / edge-case combinations, each with a sibling Kodi-compatible `.nfo` carrying test metadata.

Everything is declared in one config file (`config/media_matrix.json`) and produced by one set of scripts. Re-running skips entries that already exist; pending-external slots are reported but never block the build.

## Documents

- **This file** — overview, requirements, quickstart, CLI cheat sheet, output layout.
- **[docs/CATALOG.md](docs/CATALOG.md)** — every variant: id, name, container, codec, build status, coverage and the flag index. Generated; never edit by hand.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — pipeline flow, NFO / manifest / verify schemas, determinism, asset identity, ffmpeg quirks, extending the matrix.

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
make all            # build, verify, index, thumbnail, lay out the library, refresh the catalogue
```

That is the whole first run. It takes a few minutes with `JOBS=4`; a 30-second
build (`make all DURATION=30`) finishes in about 30 seconds.

```bash
make all JOBS=4           # parallel
make all ONLY=video,edge  # restrict to sections
make check                # what would be built, without building it
```

---

## Common flows

Each of these is a complete task. Run them from the repo root.

### Build the library for the first time

```bash
make all
```

Sources are synthesized if you have not supplied your own. Nothing is downloaded.

### Rebuild one variant after changing it

```bash
make generate VARIANT=h264_mp4_baseline FORCE=1
make verify   VARIANT=h264_mp4_baseline
make manifest && make library
```

`generate` skips anything already built, so `FORCE=1` is what makes it redo the
one you changed.

### Add a new variant

```bash
# 1. append an entry to config/media_matrix.json
make ids                              # mint its UUID -- nothing works until it has one
make check                            # confirm it resolves and lands in the right section
make generate VARIANT=my_new_variant  # build just it
make verify   VARIANT=my_new_variant  # decode, contract, NFO, bitstream assertions
make all                              # refresh manifest, library and catalogue
```

`make ids` is not optional and not automatic: a hand-written entry has no
identifier, and `catalog` refuses to run until it does. The full field reference
is in [ARCHITECTURE.md](docs/ARCHITECTURE.md#extending-the-matrix).

### Play the media from a Plex app

```bash
make servers                        # start Plex and Jellyfin in docker
make plex-setup                     # create the three libraries and scan
make plex-claim TOKEN=claim-xxxx    # from https://plex.tv/claim, expires in ~4 min
make plex-status                    # confirm: claimed, pinned, three libraries
```

`plex-setup` also tells Plex which address to advertise. Without it a
containerised server publishes its Docker NAT address and every app shows it
offline. Re-run it if your machine's IP changes.

### Check what the servers actually did with the files

```bash
make servercheck
```

Diffs both servers against `generated/manifest.json` and writes
`generated/server_report.json`. A healthy run reports `mismatched 0`; the
missing MXF file and the unindexed audio-only variants are expected and
explained in the output.

### Get the variant-to-Plex join for other tooling

```bash
make plexmatch                      # writes generated/plex_match.json
make plexmatch WRITE_LABELS=1       # also stamp testlib-uuid:<uuid> labels in Plex
```

The file carries `_meta` (which server, when) and `by_uuid` (variant UUID to
Plex rating keys). Schema:
[ARCHITECTURE.md](docs/ARCHITECTURE.md#plex_matchjson-schema).

### Start the Plex library over without re-claiming

```bash
make plex-reset-library && make plex-setup
```

`make servers-reset` also wipes the claim and needs a fresh token, so reach for
it only when you want a genuinely new server.

### Start the media over

```bash
make rebuild        # delete generated/ and library/, then build again
make clean          # also remove the synthesized source assets
```


**Default duration is 5 minutes** (300 s). Use `--duration SECONDS` or `--duration-minutes MINUTES` to override, e.g. `python3 -m scripts.generate --duration 30` for a fast CI/smoke build. Changing the duration regenerates the synthesized sources and invalidates any existing outputs (via mtime comparison) so the whole library stays consistent.

The full 5-minute build takes a few minutes with `JOBS=4`. A 30-second build completes in ~30 seconds. Incremental re-runs at the same duration skip everything that already exists.

## Matrix at a glance

| Section   | Count | What it covers                                                                                                                                                                                                                                                    |
| --------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| video     | 53    | **Chapters (4)** · H.264 / HEVC / AV1 / VP9 / **MPEG-2** / **MPEG-4 ASP (Xvid)** / **WMV2** · in MP4/MKV/TS/WebM/**AVI**/**WMV** · 8-bit + 10-bit · SDR / HDR10 / HLG · 24/25/29.97/30/60 fps · 16:9 / 4:3 / 9:16 / **anamorphic 64:45** · **progressive + 1080i + 480i interlaced** |
| masters   | 6     | **Real content** (Netflix *Sol Levante*, CC BY 4.0) at 4K: HDR10 2160p + 1080p · **HLG** · **Dolby Vision P8.4** · **AV1 HDR10** · **VP9 P2 HDR10** · SDR — ingested, never re-encoded                                                                    |
| audio     | 18    | AAC (mono/stereo/5.1/**7.1**), AC-3 5.1, E-AC-3 5.1, MP3, Opus, FLAC (16-bit + **24-bit 96k / 192k**), PCM (s16le stereo → **s24le stereo** → **7.1.4** → **9.1.4 Atmos-style bed**), **MP2**, **WMA2**                                                           |
| subtitles | 6     | External SRT / WebVTT / ASS · embedded SRT / ASS / mov_text                                                                                                                                                                                                       |
| streams   | 2     | HLS (m3u8 + TS segments) · DASH (mpd + fMP4 chunks)                                                                                                                                                                                                               |
| edge      | 25    | No audio, audio-only, multi-audio, multi-subtitle (with forced flag), VFR, broken PTS, low/high bitrate, Annex B, AVCC, fragmented MP4, missing metadata, edit list, truncated file, **6 malformed-chapter cases**                                                                               |
| external  | 10    | **3 now filled** from the Sol Levante masters (DV P5, DV P8.1, real 4K HEVC HDR10). 7 still awaiting a drop-in: HDR10+, DTS-HD MA, TrueHD/Atmos, E-AC-3 JOC, VC-1 Blu-ray, PGS, VobSub                                                                    |

**Total: 120 declared variants · 113 built (104 ffmpeg-synthesizable + 9 ingested real-content) · 7 external slots still awaiting user-supplied files.**

See [docs/CATALOG.md](docs/CATALOG.md) for every variant with its id, codec and build status.

## CLI cheat sheet

Every `make` build target takes the same flags:

| Flag | Effect |
| --- | --- |
| `JOBS=N` | parallel workers |
| `ONLY=video,edge` | restrict to sections |
| `VARIANT=name,name` | restrict to named variants |
| `FORCE=1` | rebuild even if the output exists |
| `DURATION=SECONDS` | override the 300 s default length |

The underlying scripts take `--only`, `--variant`, `--force` and `--duration`
directly.

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
make catalog                    # regenerate docs/CATALOG.md from the matrix
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

## Reference

Detail lives in [ARCHITECTURE.md](docs/ARCHITECTURE.md), one topic in one place:

| Topic | |
| --- | --- |
| [Asset identity](docs/ARCHITECTURE.md#asset-identity) | UUIDs, the short id in filenames, and which channels a media server actually reads |
| [plex_match.json](docs/ARCHITECTURE.md#plex_matchjson-schema) | the variant-to-Plex join written on every `make plexmatch`, for tooling outside this repo |
| [Chapters](docs/ARCHITECTURE.md#chapters) | the spec format, programme structures, and why chapters are not Plex markers |
| [Geometry model](docs/ARCHITECTURE.md#geometry-model) | SAR vs DAR, `pad_to`, and why `scale` needs `setsar` |
| [Per-variant duration](docs/ARCHITECTURE.md#per-variant-duration) | overriding length for one variant |
| [The faceted library](docs/ARCHITECTURE.md#the-faceted-library-scriptslibrarypy) | how `library/` is projected from the manifest |
| [Determinism model](docs/ARCHITECTURE.md#determinism-model) | what makes a rebuild reproducible |
| [On-screen overlay](docs/ARCHITECTURE.md#on-screen-overlay) | the burnt-in PTS and media-info text |
| [Audio capability](docs/ARCHITECTURE.md#audio-what-the-toolchain-can-actually-deliver) | measured per-codec channel support, and the silent downmixes |
| [Real HDR and Dolby Vision](docs/ARCHITECTURE.md#real-hdr-and-dolby-vision-assets) | the Sol Levante masters and how the DV RPUs were authored |
| [Extending the matrix](docs/ARCHITECTURE.md#extending-the-matrix) | adding a variant or an edge-case handler |

---

## Media server validation

`docker/compose.yml` runs Plex and Jellyfin against `library/`, and `scripts/servercheck.py` diffs what each one scanned against `generated/manifest.json`. The manifest is ground truth — it records what every file measurably *is* — so any disagreement is a finding rather than a guess.

```bash
make servers        # start both, library/ mounted read-only
make servercheck    # diff both inventories against the manifest
make servers-down   # stop and wipe their config volumes
```

Plex is published on **32402**. Not 32400, and not 32401 either: a native Plex Media Server binds *both* of those — 32400 publicly and 32401 on loopback — so 32401 collided with the real server and `http://localhost:32401` resolved to whichever of the two won the race. Override with `PLEX_PORT` if 32402 is taken. Both containers mount `library/` **read-only**, so neither can write metadata back into the generated tree. Config lives in named volumes, so `servers-down` returns both servers to a clean state.

No Plex account is required: the container runs unclaimed with `ALLOWED_NETWORKS`, which permits API access from the Docker networks. Jellyfin's setup wizard is driven over its API. Jellyfin's trickplay and chapter-image extraction are switched **off** at library creation — left on, it generates sprite sheets for every file, including the 100-chapter one.

Findings are sorted into four buckets so that a real defect is not buried:

| Bucket | Meaning |
| --- | --- |
| `missing` | the server should have indexed the file and did not |
| `mismatched` | indexed, but the technical detail disagrees with the manifest |
| `structural` | a consequence of the library's shape, not a defect — audio-only files are not indexed by a TV library |
| `notes` | documented server behaviour, e.g. Plex reports a coarse aspect bucket |

### What validation finds

Latest run, against both servers with the full 120-variant matrix linked:

| | Plex | Jellyfin |
| --- | --- | --- |
| Indexed | 90 / 111 | 108 / 111 |
| Technical mismatches | 0 | 0 |
| Genuinely missing | **1** — DNxHD in MXF | 0 |
| Audio-only not indexed | 20 | 3 |

Plex reconciles exactly: 111 linked − 20 audio-only − 1 MXF = 90, all ok.

Three things worth knowing, all confirmed against the running servers:

- **Plex will not ingest DNxHD in MXF; Jellyfin will.** A real capability difference between the two.
- **Plex's `aspectRatio` is a coarse bucket.** Across the entire library it emits only four values: `1.33`, `1.78`, `1.85`, `2.35`. A 1:1 square file reports `1.33` and a 9:16 vertical file also reports `1.33`. Its `width`/`height` are exact, so those are what the check compares.
- **Jellyfin caches stream details across a rebuild.** After a file was re-encoded in place, Jellyfin kept reporting the old track list — one variant showed 0 audio tracks for a file that plainly has one. A `POST /Library/Refresh` cleared it. Plex re-read the same file without prompting, so a mismatch on one server and not the other is worth retrying before believing.
- **Both servers read chapters correctly**, including the malformed cases — Plex agrees that the out-of-order file has one chapter and the stripped file has none. Plex omits chapters unless the request asks for them with `?includeChapters=1`.

## Using the library from Plex apps

The validation harness above runs headless. To actually *play* the test files from a phone, TV or browser, the library needs a different shape — one TV library cannot reach all of it — and the server needs to belong to your account so apps can find it.

Everything is local and self-contained: each person runs their own server against their own build. Nothing is hosted, shared, or coupled to anyone else's account.

The commands are in [Common flows](#play-the-media-from-a-plex-app). The rest of this
section is what they do and why, plus what to do when something is wrong.

Get the claim token from [plex.tv/claim](https://plex.tv/claim) and pass it straight in:

```bash
make plex-claim TOKEN=claim-xxxxxxxxxxxxxxxxxxxx
```

Fetch it immediately before you run the command — it expires in about four minutes. If it
has expired you get `claim failed (tokens expire after ~4 minutes)`; get a new one and run
the same command again. Nothing else needs redoing: the claim goes to the running server,
so there is no reset and no file to edit.

Check state at any time, without changing anything:

```bash
make plex-status
```
```
  url        : http://localhost:32402
  name       : TESTLIB (disposable test library)
  machine id : df90a6326d2b...
  claimed    : no   (run: make plex-claim TOKEN=claim-xxxx)
  plex pass  : no
  pinned     : yes — writes to this server are allowed
  libraries  : 3
```

Claiming is optional. Without it the server still answers the local API and every
validation target works — but Plex apps will not discover it, and account-level features
such as Plex Pass do not apply.

Once claimed the server appears in your account named **`TESTLIB (disposable test
library)`**, deliberately blunt so it is not mistaken for your own server.

Claim once. It survives restarts and `make servers-down`, and `make plex-reset-library`
rebuilds the library without losing it. Only `make servers-reset` drops it.

`library/` is built as three roots, because Plex needs three library types to reach everything:

| Root | Plex library | Purpose |
| --- | --- | --- |
| `library/flat` | Other Videos | **One item per variant, named exactly.** The reliable way to play a specific test file. |
| `library/shows` | TV Shows | Faceted browsing — pick a capability, see everything that has it. |
| `library/audio` | Music | Audio-only variants as Artist/Album/Track. |

All three are hardlinks into `generated/`, so the extra trees cost no disk.

**Why a flat library exists.** Plex's TV scanner rewrites show names that look like they contain technical tokens, so `Widescreen 16x9` is filed as `Widescreen` and unrelated shows merge into it. The most recent scan produced **12 shows out of 52 directories**, with `Widescreen` absorbing dozens of unrelated episodes and `Academy 4x3` reduced to `Academy`. The faceted tree is therefore unreliable as a count — 420 episode files became 90 Plex episodes. Other Videos applies no such naming rules, so every variant appears exactly once under the name the matrix gave it. Use the faceted library to browse and the flat library to test.

### Reach

**106 of 113 built variants are playable from Plex apps.** The seven that are not:

| Variant | Why |
| --- | --- |
| `sub_external_srt_eng`, `_webvtt_eng`, `_ass_eng` | Subtitle sidecars are not playable media in any server. They belong beside a video, not as items. |
| `dnxhd_mxf_pcm` | Plex will not ingest DNxHD in MXF. Jellyfin will — see the validation section. |
| `audio_eac3_51` | A raw E-AC-3 elementary stream is indexed by neither the Music nor the Video Files scanner. |
| `hls_h264_aac`, `dash_h264_aac` | Stream outputs are directories, not files. A scanner indexes media files, so they are excluded from `library/` entirely. |

Two behaviours worth knowing when scripting against this, both learned the hard way:

- **Agent and scanner generations must match**, or the API answers `400 new scanner needs to be paired with new agent`. Confusingly, the modern `tv.plex.agents.none` pairs with the plainly-named scanners — `Plex Video Files`, not `Plex Video Files Scanner`.
- **A refresh issued immediately after creating a section is silently dropped.** The section reports `scannedAt` unset and stays empty forever. `plexsetup` waits, then refreshes, then polls until every section is idle.

### Claiming the test server to an account

An unclaimed server works for API checks but Plex apps will not discover it, and
account-level features — Plex Pass among them — do not apply. Claiming fixes both.

The token comes from <https://plex.tv/claim> and **expires after about four minutes**,
so it cannot be committed. Put it in `docker/.env` (gitignored; copy
`docker/.env.example`):

```bash
cp docker/.env.example docker/.env
# paste the claim-xxxxxxxx token into PLEX_CLAIM
make servers-reset      # a claim only applies to a server with no existing config
make plex-setup
```

`PLEX_CLAIM` is read by the official image on **first boot only**. To claim a server
that is already running, pass the token directly instead:

```bash
make plex-setup CLAIM=claim-xxxxxxxx
```

**The test server is named `TESTLIB (disposable test library)`.** Once claimed it
appears in your Plex account beside your real server, so `plexsetup` sets a blunt name
on every run. Override with `--name`.

### Apps show the server as offline

Inside a container Plex only sees the Docker NAT address, so it advertises something
like `172.22.0.2:32400` to plex.tv. No device on your LAN can reach that, and every app
shows the server offline even though it is running and answering locally.

`make plex-setup` fixes it: it detects the host's LAN address and sets
`customConnections` to `http://<host-ip>:<published-port>`. After that plex.tv hands
apps a reachable connection:

```
https://172-22-0-2.<hash>.plex.direct:32400     local=1   <- unreachable, fails fast
https://192-168-1-123.<hash>.plex.direct:32402  local=1   <- the one apps use
```

Both are listed. The NAT entry cannot be removed, but it fails immediately so clients
fall through to the working one. Verified end to end — valid certificate, `HTTP 200`,
correct `machineIdentifier`, and an authenticated library listing over the
`plex.direct` URL.

**Re-run `make plex-setup` if your machine's IP changes.** The advertised address is
recorded once; DHCP moving the host leaves it stale and the server goes offline again.

### Securing the server

The server is reachable on the LAN so Plex apps on a phone or TV can play from it.
That is the point, and it is safe **as long as the API requires authentication**.

It did not, at first. The stack shipped with:

```yaml
ALLOWED_NETWORKS: 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

which grants **unauthenticated** access to the whole API from every address in those
ranges. Measured on a published port, any device on the LAN could read and write
settings:

```
http://192.168.1.x:32402/:/prefs   ->  HTTP 200, no token
allowMediaDeletion = 1
```

That includes changing preferences, deleting media and re-claiming the server. It was
only there so the headless tooling could work against an *unclaimed* server.

Once the server is claimed, a token replaces it:

```bash
make plex-claim TOKEN=claim-xxxx   # claim the server
make plex-token                    # store its token in docker/.env (gitignored)
```

Every script then authenticates with `PLEX_TOKEN`, and `ALLOWED_NETWORKS` stays empty.
Verified after the change:

| | localhost | LAN address |
| --- | --- | --- |
| No token | **401** | **401** |
| With token | 200 | 200 |

Plex apps are unaffected — they authenticate through your account. `allowMediaDeletion`
is now `0` as well, so even an authenticated mistake cannot delete source media.

Jellyfin needed no change: only `/System/Info/Public` is open, everything else already
returned 401.

**If you run an unclaimed throwaway server**, set `ALLOWED_NETWORKS` in `docker/.env`
to a single subnet — the Docker bridge, not `192.168.0.0/16` — and expect the API to be
open to it.

### Resetting without losing the claim

Claim once and it lasts: the token lives in `Preferences.xml` inside the `plex-config`
Docker volume, which survives restarts and `make servers-down`. Only a full reset
destroys it.

So there are two different resets, and the difference matters once you are claimed:

| Target | Library | Claim | Machine identifier / pin |
| --- | --- | --- | --- |
| `make servers-down` | kept | kept | kept |
| `make plex-reset-library` | **wiped** | kept | kept |
| `make servers-reset` | wiped | **lost** | **lost** |

`plex-reset-library` removes only `Databases`, `Media` and `Metadata`, leaving
`Preferences.xml` alone. Verified: the machine identifier is unchanged after a wipe, so
the write guard's pin stays valid and no fresh claim token is needed.

```bash
make plex-reset-library    # clean library, same server identity
make plex-setup            # recreate the three libraries and scan
```

Reach for `servers-reset` only when you want a genuinely new server — and expect to
fetch a new claim token and re-run `plex-setup` to re-pin afterwards.

## Sources

By default, the generator auto-creates synthetic test-pattern sources under `sources/` if real ones aren't supplied:

- `sources/source_master.mov` — ProRes 4444 HQ, 1920×1080, 24 fps, matches configured duration (default 5 min). ffmpeg `testsrc2` pattern.
- `sources/source_audio.wav` — PCM 16-bit stereo, 48 kHz, matches configured duration. 440 Hz + 880 Hz sine mix.
- `sources/source_sub.ass` — ASS subtitle with **one cue per second** for the full duration. Each cue's text is `[HH:MM:SS] Cue N of TOTAL — timestamp sync sample`, which makes A/V/subtitle alignment visually verifiable at any point in playback.

If the configured duration changes (via `--duration` or editing `config/media_matrix.json`), the bootstrap code detects the mismatch (by ffprobing existing sources and counting subtitle cues) and regenerates whatever needs refreshing. The generator then rebuilds downstream outputs whose source mtime is newer than the output.

To use real sources, drop files with those exact names into `sources/` before running the generator — it only synthesizes what's missing (and won't overwrite a real file unless its duration deviates from the target).

External samples (DV, HDR10+, DTS-HD, etc.) go under `sources/external/`. See [docs/CATALOG.md](docs/CATALOG.md#external-10) for the seven pre-declared slots and how to add new ones.

## What each script produces

| Script                  | Output(s)                                                                            | What it's for                                                                                                                        |
| ----------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `scripts/generate.py`   | `generated/<section>/<name>.<ext>` + sibling `.nfo`; `generated/build_manifest.json` | Build the library from the matrix config.                                                                                            |
| `scripts/verify.py`     | `generated/verify_report.json`                                                       | Post-build validation — every file decodes, matches its declared contract, has a valid NFO, satisfies per-flag bitstream assertions. |
| `scripts/manifest.py`   | `generated/manifest.json`                                                            | Machine-readable library index for downstream test automation.                                                                       |
| `scripts/thumbnails.py` | `generated/_thumbs/*.jpg`, `contact_sheet.jpg`                                       | Visual sanity check — catches HDR tone-mapping / color-range / orientation bugs that probes don't see.                               |

## Troubleshooting

**`Unknown encoder 'libaom-av1'` / `libx265`** — your ffmpeg was built without the encoder. On macOS: `brew install ffmpeg` pulls in libx264/libx265/libsvtav1/libvpx-vp9/libopus/libmp3lame by default. Check with `ffmpeg -encoders | grep <name>`.

**`Invalid data found when processing input` on a DASH manifest** — ffmpeg 6+/8.1 has a DASH _muxer_ but no DASH _demuxer_. The pipeline already works around this by probing DASH init segments directly. If you see this error from a third-party tool, probe the `init-*.m4s` files rather than the `.mpd`.

**`pending external: external_*`** — expected and informational. Those entries are declared in the matrix but their source files aren't in `sources/external/`. Drop the files in and rerun, or leave them pending indefinitely — nothing else is blocked.

**Contract mismatch** — the build failed because ffmpeg produced a structurally different output than declared (e.g. wrong pix_fmt). Check the encoder flags in `config/media_matrix.json` for that variant, or re-read the ffmpeg stderr that preceded the error.

**VFR thumbnail or probe reports `avg_frame_rate=24/1`** — the `edge_variable_fps` variant produces per-frame PTS variation via a `setpts` filter, but x264 emits CFR-style packet timing so ffprobe's average may still report the nominal rate. The file _is_ variable at the source/packet level; probe deeper with `ffprobe -select_streams v -show_entries packet=pts_time` to see actual frame spacing.

## License / redistribution

Synthetic sources and generated outputs have no encumbrances (test patterns, sine waves, sample text). External samples under `sources/external/` are presumed to carry licensing / redistribution constraints and are `.gitignore`d by default — do not commit them to public repos.
