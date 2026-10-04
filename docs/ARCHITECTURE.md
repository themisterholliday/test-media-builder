# Architecture

How the media test library is built, what each component does, and how to extend it. This doc is deeper than the README — if you just want to run the thing, start there.

---

## Table of contents

- [Component map](#component-map)
- [Pipeline flow](#pipeline-flow)
  - [Generate (`scripts/generate.py`)](#generate-scriptsgeneratepy)
  - [Verify (`scripts/verify.py`)](#verify-scriptsverifypy)
  - [Manifest (`scripts/manifest.py`)](#manifest-scriptsmanifestpy)
  - [Thumbnails (`scripts/thumbnails.py`)](#thumbnails-scriptsthumbnailspy)
- [Determinism model](#determinism-model)
- [Asset identity](#asset-identity)
  - [Where the identifier lives, and why there](#where-the-identifier-lives-and-why-there)
  - [Existing files predate the container tags](#existing-files-predate-the-container-tags)
  - [Connecting the library to Plex](#connecting-the-library-to-plex)
- [Real-content masters](#real-content-masters)
  - [Where they live, and why not in `generated/`](#where-they-live-and-why-not-in-generated)
  - [Hardlinked ingest](#hardlinked-ingest)
  - [Contract checking](#contract-checking)
  - [Attribution](#attribution)
- [NFO schema](#nfo-schema)
  - [Top half — Kodi-compatible `<movie>` schema](#top-half--kodi-compatible-movie-schema)
  - [Bottom half — `<testlib>` custom extension](#bottom-half--testlib-custom-extension)
- [plex_match.json schema](#plex_matchjson-schema)
- [manifest.json schema](#manifestjson-schema)
- [verify_report.json schema](#verify_reportjson-schema)
- [server_report.json schema](#server_reportjson-schema)
- [build_manifest.json schema](#build_manifestjson-schema)
- [Chapters](#chapters)
  - [Programme structure: intro and credits](#programme-structure-intro-and-credits)
  - [Chapters are not Plex markers](#chapters-are-not-plex-markers)
- [The faceted library (`scripts/library.py`)](#the-faceted-library-scriptslibrarypy)
- [Geometry model](#geometry-model)
- [Per-variant duration](#per-variant-duration)
- [Things deliberately not in the matrix](#things-deliberately-_not_-in-the-matrix)
- [Extending the matrix](#extending-the-matrix)
  - [Add a synthesizable video variant](#add-a-synthesizable-video-variant)
  - [Add an audio-only variant](#add-an-audio-only-variant)
  - [Add a subtitle variant](#add-a-subtitle-variant)
  - [Add a stream variant](#add-a-stream-variant)
- [Adding a new edge-case handler](#adding-a-new-edge-case-handler)
- [External-sample workflow](#external-sample-workflow)
- [On-screen overlay](#on-screen-overlay)
- [Custom source imagery](#custom-source-imagery)
- [Cadence, chroma and colour](#cadence-chroma-and-colour)
- [Audio: what the toolchain can actually deliver](#audio-what-the-toolchain-can-actually-deliver)
- [Real HDR and Dolby Vision assets](#real-hdr-and-dolby-vision-assets)
  - [How they map into the matrix](#how-they-map-into-the-matrix)
  - [How the Dolby Vision assets were made](#how-the-dolby-vision-assets-were-made)
- [Test-server identity and storage](#test-server-identity-and-storage)
  - [How the tools know which server is which](#how-the-tools-know-which-server-is-which)
  - [Claiming by accident](#claiming-by-accident)
  - [Why config lives in a Docker volume, not a bind mount](#why-config-lives-in-a-docker-volume-not-a-bind-mount)
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
├── ids.py                     # asset identity: UUIDv5 minting, the 8-hex short form, filename
│                              #   decoration and the path -> id parser
├── library.py                 # projects manifest.json into library/ as faceted TV shows (hardlinks)
├── servercheck.py             # diffs a running Plex/Jellyfin scan against manifest.json
├── plexmatch.py               # joins manifest variants to the items in a Plex library, and
│                              #   optionally stamps testlib-uuid:<uuid> labels onto them
├── plexsafety.py              # gates Plex writes on a pinned machineIdentifier; a port is
│                              #   not proof the target is the test container
├── catalog.py                 # generates docs/CATALOG.md, the whole variant catalogue
├── plexsetup.py               # creates the three Plex libraries so the tree is usable from apps
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

library/                       # scraper-facing views built by library.py (all hardlinks into generated/)
├── shows/<Facet>/             #   faceted TV-show tree: tvshow.nfo + Season 01/ episodes
├── flat/<variant>.<ext>       #   one item per variant, for a Plex "Other Videos" library
├── audio/<Codec>/<Album>/     #   audio-only variants, for a Plex "Music" library
├── .episode_map.json          #   persisted episode numbers so they never shift
└── .library_state.json        #   what this tool created, so pruning never touches anything else

docker/compose.yml             # Plex + Jellyfin pointed at library/ read-only, for scan validation

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
  4. NFO exists, parses, both uniqueids match the variant  → checks.nfo.*
     NFO <overlay> record matches what the matrix declares → checks.overlay.*
     (metadata only — that the text truly rendered into the pixels is confirmed
      visually through the thumbnails and contact sheet)
  5. declared audio channel count == probed channel count  → checks.audio.channels_match
  6. per-flag bitstream assertions:
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

## Asset identity

Every variant owns one UUID. It is a UUIDv5 derived from the variant name under a
fixed namespace, so the first mint is reproducible from nothing — the same property
the media itself has. The value is then written into `config/media_matrix.json` and
never recomputed, so renaming a variant later keeps the identifier that external
systems already recorded. `scripts/ids.py` owns all of this; nothing else derives an
id on its own.

The **short form** is the first 8 hex digits. That is what appears in filenames,
because a full UUID costs 36 characters in every path and 8 digits separate 115
variants with a wide margin.

### Where the identifier lives, and why there

| Channel | Form | Coverage |
| --- | --- | --- |
| Matrix | `uuid` | canonical |
| Filename | `Name [57b6f4a7].mp4` | **every container** |
| Manifest | `uuid`, `uuid_short` | every entry |
| NFO | `<uniqueid type="testlib">` | read by Jellyfin, discarded by Plex |
| Container tags | `synopsis` / `title` / `comment` | MP4 family + Matroska |
| Plex label | `testlib-uuid:<uuid>` | every container, written after the fact |

The filename is the primary channel, and that is a conclusion from measurement
rather than a preference. Three things were tested against a running Plex 1.43.3:

- **Plex reads embedded container metadata, but only from ISOBMFF.** It takes `title`
  over the filename and shows `synopsis` as the item summary, preferring `synopsis`
  to `description` to `comment`. MKV and WebM carry the same tags — ffprobe confirms
  they are in the file — and Plex ignores every one, falling back to the filename.
  MPEG-TS has no metadata container at all. That caps the channel at 57 of 115
  variants.
- **Plex will not index a UUID for searching.** `/search` returns nothing for a
  dashed UUID, an undashed one, or even an 8-character prefix, while ordinary words
  in the same title match. Anything that depends on searching Plex for the
  identifier cannot work.
- **The NFO agent is not reachable here.** `tv.plex.agents.nfo` is documented for
  1.43.1 and up, but an unclaimed server without Plex Pass does not offer it: the
  movie agent list is `com.plexapp.agents.{none,imdb,themoviedb}` and
  `tv.plex.agents.{movie,none}`. Under the Personal Media agent the sidecar `.nfo`
  is read and discarded. Jellyfin, by contrast, round-trips it as
  `ProviderIds: {"testlib": ...}`.

Labels are the one channel Plex can filter on exactly, so they are worth writing —
but nothing can write them until the item has already been matched, which is why
they are enrichment rather than the join key.

### Existing files predate the container tags

Identity metadata is added at encode time. Files built before it was introduced do
not carry it, and they are **not** retro-stamped. A remux would rewrite the
container of every edge case whose whole purpose is to be malformed — silently
repairing a truncated `moov` or a broken index is exactly the class of quiet damage
this library exists to catch. The filename channel already covers all 120 variants,
so nothing depends on the tags being present.

---

### Connecting the library to Plex

`scripts/plexmatch.py` reads a running Plex server and pairs each item with the
variant it came from:

```bash
python3 -m scripts.plexmatch                     # report the join
python3 -m scripts.plexmatch --all               # every row, not a sample
python3 -m scripts.plexmatch --json out.json     # machine-readable
python3 -m scripts.plexmatch --write-labels      # stamp testlib-uuid:<uuid> in Plex
```

It walks four channels in order of trust — the short id in the path, a
`testlib-uuid=` stamp in the summary, an existing label, then the bare variant name —
and reports which one produced each match, so a partial result tells you why. A
measured run matched 106 variants entirely through the path channel and left no Plex
item unaccounted for. The 14 it did not match are the 7 pending external slots,
3 subtitle sidecars, 2 stream directories, and 2 files in containers Plex will not
index (MXF and raw E-AC-3).

`--write-labels` adds `testlib-uuid:<uuid>` to every matched item, which is the one
way to query Plex for a variant exactly:

```
GET /library/sections/<id>/all?type=1&label=testlib-uuid:57b6f4a7-fef6-5f37-a86b-a0b4847fb989
```

It is idempotent: a second run reads the labels already present and writes nothing.

---

---

## Real-content masters

The `masters` section holds transcodes of the Netflix Open Content *Sol Levante*
film. They are **ingested, never encoded**: the file already exists and is placed
into `generated/` unchanged. That is the whole point — a Dolby Vision RPU and the
mastering-display metadata survive only if nothing re-encodes them, and ffmpeg
cannot author an RPU (`dovi_rpu` exposes only `strip` and `compression`).

They share the `external` plan kind, because the ingest mechanism is identical, and
differ only in `section`, which keeps them separate in reports and in the library
tree.

### Where they live, and why not in `generated/`

`make clean` and `make rebuild` run `rm -rf generated library`. These assets cannot
be rebuilt: they were derived from a 37 GB ProRes master that was deleted afterwards.
So the source of truth is `sources/masters/`, which is gitignored and outside that
blast radius. `generated/masters/` is a build product like anything else and is safe
to delete.

### Hardlinked ingest

`_ingest()` hardlinks rather than copies, falling back to a copy on a cross-device
link. The same 4.3 GB is reachable from `sources/masters/`, `generated/masters/` and
`generated/external/` while occupying disk once. Nothing modifies these files in
place, which is what makes sharing an inode safe — the same reasoning the faceted
library already relies on.

### Contract checking

An ingested file is not built here, so its matrix declaration is the only thing
asserting the drop-in is the file the matrix means. `_verify_contract` therefore
checks the `external` kind as strictly as a built variant: codec family, pixel
format, width, height, the colour signalling in `video.hdr`, and audio channel
count. Each field is checked only when declared, so the seven unfilled external
slots stay valid while carrying no assertions.

### Attribution

*Sol Levante* is CC BY 4.0, which obliges attribution to travel with the work. Any
variant declaring `attribution` has it copied into `manifest.json` and into the
`<testlib>` block of every `.nfo`, in `generated/` and in `library/` alike.

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
  <uniqueid type="testlib" default="true">14301d59-36dd-559f-919f-0741a4fb5d96</uniqueid>
  <uniqueid type="testlib-name">h264_mp4_baseline</uniqueid>
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
    <uuid>14301d59-36dd-559f-919f-0741a4fb5d96</uuid>
    <uuid_short>14301d59</uuid_short>
    <!-- <attribution> when the variant declares one (CC BY and similar) -->
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

- The default `uniqueid` is the variant's UUID and `testlib-name` is its matrix name. `verify.py` asserts both, and reports an NFO written before identifiers existed as a distinct error telling you to run `--refresh-nfo`.
- `<command>` captures the exact ffmpeg args (minus the leading `ffmpeg`) — reproducing an individual file is just paste-and-run.
- `<sources>` holds SHA-256 of every source input, so if someone swaps a source, you can detect it by comparing the NFO against the actual file.
- `<output>` SHA lets you detect post-build tampering / partial copies.
- `<hdr>` is omitted when `video.hdr` is absent from the matrix entry.

---

## plex_match.json schema

Written on every `make plexmatch` run — no flag needed, because tooling outside this
repo joins against it and must not depend on remembering one. Override the path with
`JSON=`, or pass `--json ''` to skip the write.

```json
{
  "_meta": {
    "generated_at": "2026-10-04T21:05:09+00:00",
    "plex_url": "http://localhost:32402",
    "plex_machine_identifier": "df90a6326d2b22ae349b0b6d0a7a5148b098d313",
    "plex_version": "1.43.3.10896-cb3ebc72d",
    "matched": 106,
    "unmatched_variants": 14,
    "unmatched_plex_items": 0
  },
  "by_uuid": { "14301d59-36dd-559f-919f-0741a4fb5d96": ["417", "522"] },
  "matched": [
    {
      "name": "h264_mp4_baseline",
      "uuid": "14301d59-36dd-559f-919f-0741a4fb5d96",
      "uuid_short": "14301d59",
      "title": "H.264 MP4 Baseline Profile",
      "via": "path",
      "copies": 2,
      "plex": [{ "ratingKey": "417", "guid": "local://417", "title": "Episode 48",
                 "path": "/media/shows/...", "section": "Testlib Shows" }]
    }
  ],
  "unmatched_variants": [{ "name": "...", "uuid": "...", "status": "pending_external" }],
  "unmatched_plex": [{ "ratingKey": "...", "title": "...", "path": "...", "section": "..." }]
}
```

`_meta` exists so a consumer can tell **which** Plex produced the file and when. Joining
a stale file, or one from a different server, otherwise fails silently with plausible
rating keys that point at nothing.

`by_uuid` is the join itself: variant UUID to the Plex rating keys representing it. One
variant maps to several keys because the faceted library hardlinks it into every show
whose facet it satisfies. Use `matched` when you need the surrounding detail.

`via` records which channel produced the match — `path`, `summary`, `label` or `name` —
so a partial result is diagnosable rather than mysterious.

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
      "uuid": "14301d59-36dd-559f-919f-0741a4fb5d96",
      "uuid_short": "14301d59",
      "attribution": "... when the variant declares one ...",
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
  "_meta": {
    "generated_at": "2026-10-04T21:24:54+00:00",
    "ffmpeg": "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "matrix_sha256": "79b865efa0a8..."
  },
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

## server_report.json schema

Written by `make servercheck`. Carries `_meta` with the scan time and which servers
answered, so a report cannot be mistaken for a current one:

```json
{
  "_meta": {
    "generated_at": "2026-10-04T21:25:12+00:00",
    "targets": { "plex": "http://localhost:32402", "jellyfin": "http://localhost:8096" }
  },
  "servers": { "plex": { "scanned": 90, "missing": [], "mismatched": [], "...": [] } },
  "expected_items": 111
}
```

`targets` lists only servers that actually responded. A server that was configured but
unreachable is absent, which is the distinction between "matched everything" and
"never ran".

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

### Programme structure: intro and credits

Five variants are shaped like real programmes instead of an even split, so skip-intro and
skip-credits behaviour has realistic offsets to act on:

| Variant | Shape |
| --- | --- |
| `chapters_tv_episode` | Cold Open / **Intro** / Act One / Act Two / Act Three / **Credits** |
| `chapters_tv_intro_at_zero` | **Intro** from 00:00 / Act One / Act Two / **Credits** |
| `chapters_movie_feature` | Opening Titles / three acts / **End Credits** |
| `chapters_post_credits` | Opening Titles / Main Feature / **End Credits** / Post-Credits Scene |
| `chapters_single` | one chapter over the whole file |

At the 300 s default, `chapters_tv_episode` puts the title sequence at **24 s – 54 s** and the
credits at **270 s – 300 s**. `chapters_post_credits` exists because a player that treats the
credit chapter as the end of the programme will skip content that still has to play.

### Chapters are not Plex markers

Plex's **Skip Intro** and **Skip Credits** buttons are driven by `Marker` records, not by
chapters. Plex generates those itself from audio analysis and stores them in its own
database — it does not read them from the file, and the Kodi NFO schema has no field for
them. Shipping a file with an `Intro` chapter populates the chapter list and does **not**
produce a Skip Intro button.

So these variants serve a chapter-driven implementation. Testing Plex's own marker handling
is a separate problem that needs either a real multi-episode series sharing an intro, so the
detector has something to find, or markers written directly into Plex's database. Neither is
verified here yet.

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

## Things deliberately _not_ in the matrix

These were considered and excluded — either they need external samples, they'd require significant scope, or they're expected v2 additions.

- **ABR ladders** for HLS/DASH (multiple renditions + master playlist)
- **Encrypted** segments (CENC, AES-128, Widevine/FairPlay PSSH)
- **Bitmap subtitles** — PGS (Blu-ray), VobSub (DVD) — requires external samples
- **VC-1** (SMPTE 421M) — ffmpeg has no VC-1 encoder; see `external_vc1_bluray` slot for a real Blu-ray sample
- **E-AC-3 7.1** / **TrueHD** / **Atmos with objects** / **DTS / DTS-HD MA / DTS:X** — ffmpeg cannot encode; see corresponding `external_*` slots
- **Multi-program** MPEG-TS (MPTS vs SPTS)
- **Multi-period** DASH
- **LL-HLS** / **LL-DASH** (low-latency)
- **CMAF** fragments
- **Stereoscopic 3D** (side-by-side / top-and-bottom)

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
2. `make ids` — mint its UUID. A hand-written entry has none, and `catalog`,
   `manifest` and the library naming all refuse to run until it does. Existing
   identifiers are never changed.
3. `make check` — confirm the variant resolves and appears in the section count.
4. `make generate VARIANT=hevc_mkv_aac_1080p` — build just this one.
5. `make verify VARIANT=hevc_mkv_aac_1080p` — confirm it passes decode + contract + NFO + bitstream assertions.
6. If everything's green, `make all` to refresh the thumbnail + manifest.

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
3. In `scripts/encoders.py`, write a handler and add it to `_EDGE_HANDLERS`:
   ```python
   def _edge_negative_pts(variant, defaults, sources_dir, output_path,
                          chapters_path, width, height) -> list[str]:
       """Stream with timestamps that start below zero."""
       ...        # build and return the arg list

   _EDGE_HANDLERS = (
       ...
       ("negative_pts", _edge_negative_pts),
   )
   ```
   Every handler takes the same arguments whether or not it uses them all, so
   `build_edge` can call any of them the same way.

   **Order is load-bearing.** The first matching flag wins, and `chapters` sits
   last so a malformed-chapter variant that also carries `missing_metadata` is
   handled by the strip path instead. Put a new handler where its precedence
   belongs, not at the end by default.
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

The slots and their current state are listed in
[CATALOG.md](CATALOG.md#external-10). Three are filled from the Sol Levante masters;
the rest need a user-supplied file dropped into `sources/external/`.

**Licensing.** `sources/external/` is gitignored. Assume anything placed there carries
redistribution constraints and keep it out of any public repository. The exception in
kind, not in handling, is *Sol Levante*: it is CC BY 4.0 and **may** be redistributed
with attribution, but it is still kept out of git because the files total 4.3 GB. Any
variant declaring an `attribution` field has it copied into `manifest.json` and into
every `.nfo`, which is what satisfies the licence wherever an asset travels.


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

---

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

---

## Cadence, chroma and colour


- **23.976 fps** (`h264_mp4_23976`) — the standard film rate, previously absent, plus 48 and 50 fps.
- **3:2 pulldown** (`mpeg2_ts_telecine`) — 23.976 film carried as 29.97 interlaced; needs inverse telecine to recover.
- **4:2:2 10-bit, 4:4:4 8-bit, 12-bit HEVC** — the library was otherwise entirely 4:2:0.
- **Full-range levels** (`h264_mp4_full_range`, which ffmpeg represents as `yuvj420p`) and **BT.601 matrix** (`h264_mp4_bt601`).

---

## Audio: what the toolchain can actually deliver


Every cell measured by probing the output, because several codecs downmix without any error.

| Codec | 2.0 | 5.1 | 7.1 | 7.1.4 (12ch) | 9.1.4 (14ch) |
| --- | --- | --- | --- | --- | --- |
| PCM | yes | yes | yes | **yes** | **yes** |
| Vorbis | yes | yes | yes | **yes** | **yes** |
| AAC | yes | yes | yes | rejects | rejects |
| FLAC | yes | yes | yes | rejects | rejects |
| Opus | yes | yes | yes | rejects | rejects |
| AC-3 | yes | yes | →6ch | →6ch | →6ch |
| E-AC-3 | yes | yes | →6ch | →6ch | →6ch |
| TrueHD | yes | yes | →6ch | →6ch | →6ch |
| DTS | yes | yes | →6ch | →6ch | →6ch |
| ALAC | yes | yes | **→7ch** | **→7ch** | **→8ch** |
| MP3 / MP2 | yes | →2ch | →2ch | →2ch | →2ch |

`→Nch` means a **silent downmix**: the encode succeeds and reports the wrong channel count.
`rejects` means a clean error, which is the safer failure.

`verify` now guards against this. Every variant declaring `audio.channels` has that count
compared against what ffprobe reports, and a mismatch fails the build with the layout named:

```
audio channels: declared 8, file carries 6 (5.1(side)) --
the encoder accepted the request and delivered something else
```

The matrix is currently honest — 108 variants declare a channel count and all 108 match — so this
is a regression guard, not a backlog. It exists because adding `channels: 8` to an AC-3 variant
would otherwise ship a 6-channel file with nothing to notice.

TrueHD and DTS need `-strict -2` (both are experimental encoders). Without it they fail outright
rather than downmixing, which is easy to mistake for a channel-layout problem.

Three consequences. Above 8 channels only **PCM and Vorbis** deliver, so the 7.1.4 and 9.1.4 beds
cannot be AC-3/E-AC-3/TrueHD/DTS. **ALAC is broken above 5.1** — 7.1 in gives 7 channels out, which
is not even a coherent downmix. And `verify.py` has **no declared-vs-probed channel assertion**,
which is the obvious gap given six codecs lie.

**Atmos remains impossible**: ffmpeg has no JOC encoder, and Netflix Open Content ships only Dolby
encoder *inputs* (ADM BWAV and DAMF), no JOC or TrueHD-Atmos deliverable.

---

## Real HDR and Dolby Vision assets


Nine assets built from the Netflix Open Content **Sol Levante** master, each the full 4 m 23 s
film. They live in `sources/masters/` and are **fully integrated into the matrix**: six as a
`masters` section, three filling external slots that had been empty since day one.

> **Attribution.** *Sol Levante* © Netflix, licensed under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), from
> [Netflix Open Content](https://opencontent.netflix.com/). The derived assets here are
> transcodes of that work and carry the same licence. Every variant records this in its
> `attribution` field, which propagates to `manifest.json` and to each `.nfo`.

They live under `sources/` rather than `generated/` on purpose: `make clean` and `make rebuild`
run `rm -rf generated library`, and these cannot be rebuilt — the 37 GB ProRes master they came
from was deleted after the derivation. `sources/masters/` is gitignored.

Ingest hardlinks rather than copies, so the 4.3 GB appears in `sources/masters/`,
`generated/masters/` and `generated/external/` while occupying disk once.

| File | Codec | Res | Transfer | Audio | Size |
| --- | --- | --- | --- | --- | --- |
| `sollevante_hdr10_2160p24.mp4` | HEVC Main10 | 3840×2160 | smpte2084 | E-AC-3 5.1 | 629 MB |
| `sollevante_hdr10_1080p24.mp4` | HEVC Main10 | 1920×1080 | smpte2084 | E-AC-3 5.1 | 262 MB |
| `sollevante_hlg_2160p24.mp4` | HEVC Main10 | 3840×2160 | arib-std-b67 | E-AC-3 5.1 | 629 MB |
| `sollevante_dv_p5_2160p24.mp4` | HEVC + DV RPU | 3840×2160 | smpte2084 | E-AC-3 5.1 | 630 MB |
| `sollevante_dv_p81_2160p24.mp4` | HEVC + DV RPU | 3840×2160 | smpte2084 | E-AC-3 5.1 | 630 MB |
| `sollevante_dv_p84_2160p24.mp4` | HEVC + DV RPU | 3840×2160 | smpte2084 | E-AC-3 5.1 | 631 MB |
| `sollevante_av1_hdr10_2160p24.mp4` | AV1 | 3840×2160 | smpte2084 | Opus 5.1 | 341 MB |
| `sollevante_vp9_hdr10_1080p24.webm` | VP9 Profile 2 | 1920×1080 | smpte2084 | Opus 5.1 | 201 MB |
| `sollevante_sdr_2160p24.mp4` | H.264 | 3840×2160 | bt709 | E-AC-3 5.1 | 409 MB |

Dolby Vision signalling, as reported by `MP4Box -info`:

| File | Profile | Compatibility |
| --- | --- | --- |
| `dv_p5` | 5 | 0 — DV only |
| `dv_p81` | 8 | 1 — HDR10 compatible |
| `dv_p84` | 8 | 4 — HLG compatible |

`dv_p84` carries a signalling conflict worth knowing about: compatibility id 4 declares an **HLG**
base layer, but the container tags the transfer as `smpte2084` (**PQ**). The matrix declares the
measured value and flags the variant `signalling_conflict`, because a player having to choose
between the `dvvC` box and the container tag is itself a case worth testing.

### How they map into the matrix

| Matrix variant | Section | Source file |
| --- | --- | --- |
| `external_dolby_vision_p5` | external | `sollevante_dv_p5_2160p24.mp4` |
| `external_dolby_vision_p8_1` | external | `sollevante_dv_p81_2160p24.mp4` |
| `external_4k_hevc_real_content` | external | `sollevante_hdr10_2160p24.mp4` |
| `master_dv_p84_2160p24` | masters | `sollevante_dv_p84_2160p24.mp4` |
| `master_hdr10_1080p24` | masters | `sollevante_hdr10_1080p24.mp4` |
| `master_hlg_2160p24` | masters | `sollevante_hlg_2160p24.mp4` |
| `master_av1_hdr10_2160p24` | masters | `sollevante_av1_hdr10_2160p24.mp4` |
| `master_vp9_hdr10_1080p24` | masters | `sollevante_vp9_hdr10_1080p24.webm` |
| `master_sdr_2160p24` | masters | `sollevante_sdr_2160p24.mp4` |

Ingested files are never re-encoded, so the DV RPUs and the mastering-display metadata survive
byte-for-byte. Their declarations are contract-checked on every build — codec, pixel format,
dimensions, colour signalling and audio channel count must match what ffprobe reports, which is
what stops a wrong file being dropped into a slot unnoticed.

### How the Dolby Vision assets were made

No Dolby licence and no DaVinci Resolve. Netflix ships the grade's trim metadata as a 203 KB
`DolbyLabsMDF` XML, which is exactly what `dovi_tool` consumes:

```bash
dovi_tool generate --xml sollevante_dv.xml --profile 8.1   --canvas-width 3840 --canvas-height 2160 -o p81.rpu    # 6314 frames
dovi_tool inject-rpu -i base.hevc --rpu-in p81.rpu -o dv.hevc
MP4Box -add "dv.hevc:dvp=8.1" -new out.mp4                # writes the dvvC box
```

Two things that will bite anyone repeating this. **ffmpeg's MP4 muxer does not write the Dolby
Vision configuration box** — muxing the same elementary stream with ffmpeg yields a file with no DV
side data at all, so MP4Box (GPAC) is required. And `dovi_tool info` takes an *RPU*, not an HEVC
file; use `extract-rpu` first.

The 37 GB ProRes master was deleted after transcoding. Its PCM audio was kept as
`sources/masters/sollevante_audio_51.wav` (5.1, 24-bit) so audio work does not need a re-download.

> **ffmpeg 9 caveat.** `-color_primaries` and `-color_trc` as *output options* are silently ignored
> in ffmpeg 9 — the tags simply do not appear. These assets use the `setparams` filter instead.
> `encoders.py` still uses the old form, so rebuilding the matrix HDR variants under ffmpeg 9 would
> strip their tags. `verify.py` asserts probed colour against the declared values and would fail the
> build, but the encoder needs fixing.

---

## Test-server identity and storage

### How the tools know which server is which

A port is not proof of identity. A native Plex Media Server binds **both** 32400 and
32401 (the second on loopback), so the old 32401 default could resolve to a real
library depending on whether `localhost` went to IPv4 or IPv6. The container now
publishes **32402**.

That alone is not enough, so `plexsetup` records the test server's
`machineIdentifier` in `docker/.testlib-server-id`, and `scripts/plexsafety.py` checks
it before any write:

| Target | Result |
| --- | --- |
| Machine identifier matches the pin | allowed, **claimed or not** |
| No pin yet, server reports `claimed="0"` | allowed — an unclaimed server owns no real library |
| Anything else | refused, naming both identifiers |

The pin is what makes claiming safe: `claimed="0"` stops being a useful signal the
moment you attach the test server to an account, and without the pin every write
would start being refused. Override deliberately with `--allow-claimed-server`.

---

### Claiming by accident

`ALLOWED_NETWORKS` grants unauthenticated access to the whole API — including
`/myplex/claim`. An unclaimed server is therefore not a stable state if anything on the
host can reach it: opening the web UI while signed in to Plex is enough to attach it to
your account. Because the pin identifies the server by machine identifier, an accidental
claim no longer breaks the tooling. If you would rather it stayed unclaimed, narrow
`ALLOWED_NETWORKS` to the container subnet and avoid the web UI.

---

### Why config lives in a Docker volume, not a bind mount

Both servers keep their catalogue in SQLite, and **SQLite on a macOS bind mount through
Docker Desktop does not get working POSIX locks**. The database corrupts; the first
symptom is an HTTP 500 from a library listing while other endpoints still answer:

```
ERROR - SQLITE3: database corruption at line 72267
ERROR - Got exception from request handler: database disk image is malformed
```

This was reproduced on a fresh database that nothing had edited, so it is the mount and
not a one-off. Config therefore lives in the named volumes `plex-config` and
`jellyfin-config`, inside the Linux VM where locking works. `library/` stays a bind mount
because it is read-only and holds no database.

The cost: config is no longer browsable from the host. Reach it with
`docker run --rm -v testlib_plex-config:/config alpine ls /config`, which is what
`make plex-reset-library` does.

---

## FFmpeg quirks handled by the pipeline

Non-obvious things the code accounts for.

| Quirk                                                                                              | Where handled                                                                                                          | Why                                                                                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scale` silently rewrites SAR to preserve the source display aspect, so coded dimensions do not mean what they look like | `encoders._build_vf_chain` now ends in `setsar=1` unless `video.sar` is declared | Three variants shipped claiming 4:3 / 9:16 while displaying 16:9. Square pixels are now the default and anamorphic is opt-in. |
| SQLite on a macOS bind mount through Docker Desktop corrupts, because POSIX locking does not translate across the file-sharing layer | `docker/compose.yml` keeps both servers' `/config` in named volumes, inside the Linux VM | The symptom is narrow and misleading: one endpoint returns HTTP 500 while the rest answer normally, and the log says `database disk image is malformed`. Reproduced on a fresh database nothing had touched. `library/` stays a bind mount — it is read-only and holds no database. |
| A containerised Plex advertises its Docker NAT address, so LAN apps show it offline | `plexsetup.advertise_lan_address` sets `customConnections` to the host address and published port | Plex inside the container only sees `172.22.0.2:32400` and publishes that to plex.tv. It is unreachable from the LAN, so every app reports the server offline while it answers normally on localhost. The port remap matters too: the container listens on 32400 but is published on 32402. |
| `ALLOWED_NETWORKS` grants unauthenticated access to the entire Plex API, not just read access | It is empty by default in `docker/compose.yml`; the tooling authenticates with `PLEX_TOKEN` from `docker/.env`, written by `make plex-token` | Measured: with the shipped `10/8,172.16/12,192.168/16` value, any LAN device got HTTP 200 on `/:/prefs` with no token, while `allowMediaDeletion` was `1`. Clearing the compose variable is not enough — the value persists in `Preferences.xml`, so the stored `allowedNetworks` pref has to be cleared too. |
| A localhost port is not proof of which Plex server answers | The container moved to 32402, and `plexsafety.require_test_server` gates every write on a `machineIdentifier` pinned by `plexsetup` in `docker/.testlib-server-id` | 32401 was chosen because 32400 belongs to the real server. A native Plex install also binds 32401 as a secondary port, so `localhost:32401` resolved to whichever won — IPv6 reached the container, IPv4 the real server. Labels and sections written there are not undone by a rescan. The pin rather than `claimed="0"` is what keeps the check working once the test server is deliberately claimed to an account. |
| ffmpeg 9 silently ignores `-color_primaries` and `-color_trc` as **output** options (it still honours `-colorspace`) | `encoders._setparams_filter` appends `setparams=` to the `-vf` chain | The options are accepted and dropped with no warning, so the file probes as `color_transfer=unknown`. x265 masked this because `x265_params` writes the signalling into the bitstream VUI, but AV1, VP9 and x264 have no such escape hatch — an HDR variant on any of them would have shipped untagged. |
| `-metadata:s:v rotate=` writes no side data in ffmpeg 8                                            | `generate._apply_rotation` remuxes with input-side `-display_rotation` after the encode                                | It is the only route that produces a real display matrix. The temp file must keep the original extension or ffmpeg cannot select a muxer. |
| ffprobe normalises rotation into -180..180                                                         | `verify._geometry_assertions` compares against `((deg + 180) % 360) - 180`                                             | A declared 270 probes as -90 and 180 as -180; a naive equality check would fail on correct files. |
| `-color_range pc` changes the pixel format to `yuvj420p`                                           | the matrix declares `yuvj420p` for the full-range variant                                                              | The contract check compares declared against probed pix_fmt, so declaring `yuv420p` failed the build. |
| Thumbnails ignored pixel aspect, so anamorphic tiles were geometrically wrong                      | `thumbnails._extract_thumb` prepends `scale=iw*sar:ih`                                                                 | The contact sheet misrepresented exactly the variants that exist to test geometry. |
| Reading the head of the container to detect Annex B framing                                        | `verify._is_annexb` checks for an `avcC` box, then scans 64 KiB for start codes                                        | In MPEG-TS the first bytes are packet framing (sync 0x47), so a correct Annex B file looked like a failure. Demuxing to raw h264 is equally wrong: ffmpeg applies `h264_mp4toannexb` on the way out, so even an AVCC source comes back Annex B. |
| `verify` ran its decode smoke against whatever `ffmpeg` was on PATH                                | `_decode_smoke` and `_decode_smoke_dash` now use `ffutil.ffmpeg_bin()`                                                 | Every other entry point honoured `defaults.ffmpeg_bin`; verify silently did not, so it could test a different build than the one that produced the files. |
| `defaults.source_audio` was declared but ignored — the filename was hardcoded in five places       | `generate._source_names()` plus a `defaults` argument to `encoders._audio_src_for_channels`                            | `source_video` and `source_subtitle` were honoured, so the inconsistency was invisible until someone changed the audio value and nothing happened. |
| User-supplied source files accidentally overwritten by duration-aware regen                        | `bootstrap._is_synthetic` + `sources/.synthetic_sources.json`                                                          | The sentinel records which files were produced by our bootstrap. Only those are refreshed when duration changes; real user assets get a warning if their duration mismatches the target but are never touched.                                        |
| `defaults.ffmpeg_bin` is an absolute path, correct on one machine and absent on every other | `ffutil._pick_binary` falls through to PATH when the configured path is not executable, warning once | The path is committed in shared config. Ranking it above PATH with no fallback made `require_binaries()` raise `FFBinaryMissing` on CI and on any machine without that exact Homebrew formula, even with `ffmpeg` installed. |
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
