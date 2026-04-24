# External Samples

Slots for media that **cannot be synthesized by stock ffmpeg** due to proprietary encoders, licensed metadata tooling, or real-world content requirements. These files are referenced in `config/media_matrix.json` under `external_variants` and must be dropped into this directory manually.

## How it works

1. Each slot is declared in `config/media_matrix.json` under `external_variants` with a fixed `source_path`.
2. When `scripts/generate.py` runs, it looks for the file at that path.
3. If present, the file is copied to `generated/external/`, probed with ffprobe, verified against any declared structural contract, and an NFO is written.
4. If absent, the slot is reported as `pending_external` and skipped — the rest of the matrix still builds.

Run `make check` to see which external slots are currently missing.

## Declared slots

| File (drop here)                             | Variant name                    | Why it cannot be synthesized                                                      |
|----------------------------------------------|---------------------------------|-----------------------------------------------------------------------------------|
| `dolby_vision_p5.mp4`                        | `external_dolby_vision_p5`      | Requires Dolby RPU tooling to inject DV metadata NAL units.                       |
| `dolby_vision_p8_1.mp4`                      | `external_dolby_vision_p8_1`    | Same as above; profile 8.1 adds HDR10-compatible base layer.                      |
| `hdr10plus.mp4`                              | `external_hdr10_plus`           | HDR10+ dynamic metadata (SMPTE ST 2094-40) requires `hdr10plus_tool` or a sample. |
| `dts_hd_ma_5_1.mkv`                          | `external_dts_hd_ma`            | ffmpeg decodes DTS-HD MA but cannot encode it — encoder is proprietary.           |
| `truehd_atmos.mkv`                           | `external_truehd_atmos`         | Dolby TrueHD + Atmos encoder is proprietary.                                      |
| `eac3_joc_atmos.mp4`                         | `external_eac3_joc_atmos`       | Dolby Digital Plus JOC (Atmos) metadata requires Dolby tooling.                   |
| `4k_hevc_hdr10.mkv`                          | `external_4k_hevc_real_content` | Real-world 4K HDR10 content (not a synthetic test pattern).                       |
| `vc1_bluray.m2ts`                            | `external_vc1_bluray`           | VC-1 (SMPTE 421M) video. ffmpeg has no VC-1 encoder — requires a licensed sample (e.g. Blu-ray rip). |

## Adding a new slot

1. Drop the file under `sources/external/` (filename free but conventionally matches the table).
2. Add an entry to `external_variants` in `config/media_matrix.json`:
   ```json
   {
     "name": "external_my_sample",
     "title": "My Sample",
     "description": "Why this sample exists and what it validates.",
     "source_path": "sources/external/my_sample.mkv",
     "container": "mkv",
     "flags": ["external", "my_tag"],
     "license_note": "Where the sample came from; redistribution constraints.",
     "out_subdir": "external"
   }
   ```
3. Run `make check` — the new slot should appear.
4. Run `make generate --only external` (or just `make generate`) to probe it and produce its NFO.

## License and redistribution

This directory is `.gitignore`d. Samples here are presumed to carry licensing or redistribution constraints — do not commit them to a public repository. Use a separate private store or artifact registry for shared access.
