# Matrix Catalog

Complete catalog of every declared variant in `config/media_matrix.json`, grouped by section. Each entry lists the filename, what it tests, and the ffmpeg command shape used to produce it. Numbers match the current matrix (109 total: 99 synthesizable, 10 external).

---

## Conventions

- **Source assets**: all synthesizable variants derive from `sources/source_master.mov` (1920×1080 ProRes), `sources/source_audio.wav` (PCM stereo 48 kHz, 440 Hz + 880 Hz sine mix), and `sources/source_sub.ass` (ASS subtitle with **one 1-second cue per second**, each showing `[HH:MM:SS] Cue N of TOTAL — timestamp sync sample`). Bootstrap synthesizes these if you don't supply real ones; a sentinel file `sources/.synthetic_sources.json` tracks which files were synthesized so user-supplied assets are never overwritten. Drop images into `sources/images/` to drive the master's picture from real stills instead of the `testsrc2` pattern.
- **On-screen overlay**: every video variant carries burned-in text — per-frame `PTS` / frame number / timecode top-left (inherited from the master), and the variant name plus codec / container / resolution / fps / audio bottom-left (drawn at encode time). Audio-only variants, external subtitles, and the `external_*` copies carry none. Disable per run with `--no-burn-in`, or per variant with `"burn_in": false`.
- **Duration**: default is **300 seconds (5 minutes)**, configured in `config/media_matrix.json` → `defaults.duration_seconds`. Override at invocation with `--duration SECONDS` or `--duration-minutes MINUTES`. Changing the duration regenerates the synthetic sources (which touches their mtime), which in turn invalidates any existing outputs so the whole library stays internally consistent.
- **Default output geometry**: 1280×720 at 24 fps unless the variant declares a `width` / `height` / `framerate` override.
- **Naming**: `<codec>_<container>[_qualifier].<ext>` for normal variants; `edge_<behavior>` for edge cases; `external_<feature>` for licensed/proprietary slots.
- **NFO**: every file gets a sibling `.nfo` — identical basename, `.nfo` extension. Kodi-compatible `<movie>` schema plus a `<testlib>` extension block.

---

## Video (48 variants)

Standard video + audio files at 720p 24 fps unless noted. Covers codec profiles, containers, bit depths, HDR modes, frame rates, aspect ratios, interlacing, and legacy codecs that a media-server pipeline must ingest.

| Filename                          | Codec              | Container | Audio                 | Notes                                                                                                                 |
| --------------------------------- | ------------------ | --------- | --------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `h264_mp4_baseline.mp4`           | H.264 Baseline 3.1 | MP4       | AAC 128k stereo       | Legacy-compatibility baseline; lowest-common-denominator decoder target.                                              |
| `h264_mp4_high.mp4`               | H.264 High 4.1     | MP4       | AAC 192k stereo       | Mainstream desktop/web profile at 5 Mbps.                                                                             |
| `hevc_mp4_8bit.mp4`               | HEVC Main          | MP4       | AAC 192k stereo       | 8-bit HEVC; exercises `hvc1` tag for Apple-stack playback.                                                            |
| `hevc_mp4_10bit_hdr10_bt2020.mp4` | HEVC Main10        | MP4       | E-AC-3 640k 5.1       | **1080p**, 10-bit, HDR10: BT.2020 primaries, SMPTE-2084 (PQ) transfer, mastering display + max-CLL via `x265-params`. |
| `hevc_mp4_10bit_hlg_bt2020.mp4`   | HEVC Main10        | MP4       | E-AC-3 640k 5.1       | **1080p**, 10-bit, HLG (ARIB STD-B67) transfer, BT.2020 primaries.                                                    |
| `hevc_mp4_10bit_bt709_sdr.mp4`    | HEVC Main10        | MP4       | AAC 192k stereo       | 10-bit SDR studio intermediate; headroom for banding without HDR.                                                     |
| `av1_mkv_opus.mkv`                | AV1 (SVT-AV1)      | MKV       | Opus 128k stereo      | Modern royalty-free codec combo; SVT-AV1 preset 10 for tractable encode time.                                         |
| `vp9_webm_opus.webm`              | VP9                | WebM      | Opus 128k stereo      | Google web-stack codec pair.                                                                                          |
| `h264_mkv_ac3_51.mkv`             | H.264 High         | MKV       | AC-3 448k 5.1         | H.264 + Dolby Digital 5.1 passthrough target.                                                                         |
| `h264_ts_aac.ts`                  | H.264 Main         | MPEG-TS   | AAC 192k stereo       | Broadcast-style container.                                                                                            |
| `h264_mp4_mp3.mp4`                | H.264 Main         | MP4       | MP3 192k stereo       | H.264 + legacy MP3 audio (common in older content).                                                                   |
| `h264_mkv_flac.mkv`               | H.264 High         | MKV       | FLAC stereo           | Lossless audio passthrough target.                                                                                    |
| `h264_mkv_pcm.mkv`                | H.264 High         | MKV       | PCM s16le stereo      | Uncompressed audio (sidecar / edit workflows).                                                                        |
| `h264_mp4_30fps.mp4`              | H.264 High         | MP4       | AAC 192k stereo       | **30 fps** — NTSC-style frame rate branch.                                                                            |
| `h264_mp4_60fps.mp4`              | H.264 High         | MP4       | AAC 192k stereo       | **60 fps** — high-frame-rate branch (sports / gaming).                                                                |
| `h264_mp4_4_3_sd.mp4`             | H.264 Main         | MP4       | AAC 128k stereo 44.1k | **720×540 4:3 SD** — legacy aspect + 44.1k audio (music).                                                             |
| `h264_mp4_9_16_vertical.mp4`      | H.264 High         | MP4       | AAC 128k stereo       | **720×1280 9:16** — mobile / vertical.                                                                                |
| `mpeg2_ts_1080i_broadcast.ts`     | MPEG-2 Video       | MPEG-TS   | MP2 192k stereo       | **1080i 29.97 interlaced**, DAR 16:9. DVB-T / ATSC broadcast profile. Legacy codec + interlaced branch.               |
| `mpeg2_ts_480i_ntsc.ts`           | MPEG-2 Video       | MPEG-TS   | MP2 192k stereo       | **480i 29.97 interlaced**, NTSC SAR 8:9 (snapped from 10:11 by MPEG-2 header). SD broadcast-style.                    |
| `mpeg4_avi_mp3_xvid.avi`          | MPEG-4 Part 2 ASP  | AVI       | MP3 192k stereo       | Tagged `XVID` so legacy DivX/Xvid players accept it. 640×480 progressive.                                             |
| `wmv_asf_wmav2.wmv`               | WMV2 (WMV 8)       | ASF/WMV   | WMA2 128k stereo      | Windows Media codec pair.                                                                                             |
| `h264_mp4_pal_anamorphic.mp4`     | H.264 High         | MP4       | AAC 128k stereo       | **720×576 PAL** with SAR **64:45** → DAR exactly 16:9. Anamorphic widescreen / non-square pixels branch.              |
| `chapters_mp4_10.mp4`             | H.264 High         | MP4       | AAC 192k stereo       | **10 evenly spaced chapters**. MP4 stores chapters as a dedicated chapter track.                                      |
| `chapters_mkv_10.mkv`             | H.264 High         | MKV       | AAC 192k stereo       | The same ten chapters in Matroska, which uses its own chapter structure.                                              |
| `chapters_mkv_unicode.mkv`        | H.264 High         | MKV       | AAC 192k stereo       | **6 chapters with non-ASCII titles** — accented, Japanese, Arabic (RTL), Greek, emoji. All verified to round-trip.    |
| `chapters_mkv_100.mkv`            | H.264 High         | MKV       | AAC 192k stereo       | **100 chapters** — chapter-list UI, scrubber marker density, seek-menu paging.                                        |

---

## Audio-only (18 variants)

Pure audio files with no video stream. Tests the pipeline's audio-only ingest path (music import, podcast flow) plus channel layout, bit depth, and sample rate coverage.

| Filename                     | Codec     | Container  | Notes                                                                                               |
| ---------------------------- | --------- | ---------- | --------------------------------------------------------------------------------------------------- |
| `audio_aac_stereo.m4a`       | AAC-LC    | M4A        | 192k stereo at 48 kHz.                                                                              |
| `audio_ac3_51.ac3`           | AC-3      | raw AC-3   | 448k 5.1.                                                                                           |
| `audio_eac3_51.eac3`         | E-AC-3    | raw E-AC-3 | 640k 5.1.                                                                                           |
| `audio_opus_stereo.ogg`      | Opus      | Ogg        | 160k stereo.                                                                                        |
| `audio_mp3_stereo.mp3`       | MP3       | raw MP3    | 192k CBR stereo.                                                                                    |
| `audio_flac_stereo.flac`     | FLAC      | native     | Lossless stereo at 48 kHz 16-bit.                                                                   |
| `audio_pcm_wav.wav`          | PCM s16le | WAV        | Uncompressed stereo 16-bit 48 kHz.                                                                  |
| `audio_aac_mono.m4a`         | AAC-LC    | M4A        | **Mono** 96k. Exercises mono-track ingest / UI branch.                                              |
| `audio_aac_71.m4a`           | AAC-LC    | M4A        | **7.1** 384k (8 channels, canonical layout `7.1`).                                                  |
| `audio_pcm_714_bed.wav`      | PCM s16le | WAV        | **7.1.4** bed (12 channels, layout `7.1.4`). Object-audio bed layout without proprietary metadata.  |
| `audio_pcm_914_bed.wav`      | PCM s16le | WAV        | **9.1.4** Atmos-style bed (14 channels, layout `9.1.4`). Atmos objects are proprietary, bed is PCM. |
| `audio_pcm_24bit_48k.wav`    | PCM s24le | WAV        | **24-bit** stereo at 48 kHz. `bits_per_raw_sample=24`.                                              |
| `audio_flac_24bit_96k.flac`  | FLAC      | native     | **Hi-res** FLAC stereo at 96 kHz 24-bit.                                                            |
| `audio_flac_24bit_192k.flac` | FLAC      | native     | **Studio** FLAC stereo at 192 kHz 24-bit.                                                           |

Multi-channel audio sources are synthesized on the fly with `aevalsrc` + a canonical `channel_layout=…` selector (stereo → `source_audio.wav`; 5.1 → `5.1`; 7.1 → `7.1`; 7.1.4 → `7.1.4`; 9.1.4 → `9.1.4`). The encoder actually has N discrete channels to compress. For mono (1 channel) we downmix the stereo source via `-ac 1`.

**Not synthesizable with stock ffmpeg:** E-AC-3 7.1 (ffmpeg's eac3 encoder tops out at 5.1), DTS / DTS-HD MA / TrueHD / Atmos-with-JOC (all proprietary encoders). These belong in `external_variants` — see [external section](#external-8-slots--user-supplied).

---

## Subtitles (6 variants)

Mix of external sidecar subtitle files and subtitle tracks embedded in a video container.

| Filename                      | Format   | Embedded? | Notes                                                                                |
| ----------------------------- | -------- | --------- | ------------------------------------------------------------------------------------ |
| `sub_external_srt_eng.srt`    | SRT      | No        | Sidecar SRT; ASS source converted via ffmpeg `-c:s srt`.                             |
| `sub_external_webvtt_eng.vtt` | WebVTT   | No        | Sidecar WebVTT.                                                                      |
| `sub_external_ass_eng.ass`    | ASS      | No        | Sidecar ASS (source format).                                                         |
| `h264_mkv_srt_embedded.mkv`   | SRT      | Yes       | Subtitle track tagged `language=eng`, inside H.264 MKV with AAC.                     |
| `h264_mkv_ass_embedded.mkv`   | ASS      | Yes       | Styled ASS track inside MKV — tests pass-through of styling.                         |
| `h264_mp4_mov_text.mp4`       | mov_text | Yes       | 3GPP Timed Text (`tx3g`) inside MP4 — the only subtitle codec MP4 supports natively. |

---

## Streams (2 variants)

Segmented streaming outputs in the two industry formats. Single rendition each (no ABR ladder — that's a separate concern).

| Output dir               | Format   | Segment duration | Notes                                                                                                                                                                                 |
| ------------------------ | -------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `streams/hls_h264_aac/`  | HLS VOD  | 4 s              | `playlist.m3u8` + `seg_000.ts … seg_002.ts` (H.264 Main + AAC 192k). Sibling `hls_h264_aac.nfo` at the `streams/` level.                                                              |
| `streams/dash_h264_aac/` | DASH VOD | 4 s              | `manifest.mpd` + `init-0.m4s / init-1.m4s` + `chunk-0-*.m4s / chunk-1-*.m4s` (fMP4 segments). DASH probe is done via init segments — ffmpeg 8.1 has a DASH muxer but no DASH demuxer. |

---

## Edge cases (14 variants)

Deliberately-pathological files that stress specific pipeline behaviors.

| Filename                         | Flags                               | Behavior                                                                                                                                                                                                                                |
| -------------------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `edge_no_audio.mp4`              | `no_audio`                          | H.264 MP4 with no audio stream. Tests track-enumeration code paths that assume ≥1 audio.                                                                                                                                                |
| `edge_audio_only.m4a`            | `audio_only`                        | Audio-only AAC in M4A. Redundant with `audio_aac_stereo.m4a` but carries the `audio_only` flag for edge-case scans.                                                                                                                     |
| `edge_multi_audio_tracks.mkv`    | `multi_audio`                       | H.264 MKV with **two audio tracks**: English AAC stereo (default), Spanish AC-3 5.1. Tests language/disposition picking.                                                                                                                |
| `edge_multi_subtitle_tracks.mkv` | `multi_subtitle`, `forced_subtitle` | H.264 MKV with **three subtitle tracks**: English SRT (default), Spanish SRT (forced), Japanese ASS. Tests disposition (default/forced).                                                                                                |
| `edge_variable_fps.mkv`          | `variable_fps`                      | H.264 MKV with `setpts` filter producing non-uniform frame intervals. Note: x264 output may still report `avg_frame_rate=24/1` because packet timing is CFR-style; the test file has VFR at the source filter level.                    |
| `edge_broken_pts.ts`             | `broken_timestamps`                 | H.264 MPEG-TS with a 5-second forward PTS jump at frame 120 (setpts discontinuity). Tests discontinuity / drift handling.                                                                                                               |
| `edge_low_bitrate.mp4`           | `low_bitrate`                       | H.264 baseline @ **150 kbps** video, 32 kbps audio. Heavy compression artifacts — stresses decoders against low-quality input.                                                                                                          |
| `edge_high_bitrate_stress.mkv`   | `high_bitrate`                      | H.264 high @ **80 Mbps** video + AC-3 640k 5.1. Stresses I/O throughput and direct-play decisions.                                                                                                                                      |
| `edge_annexb_stream.ts`          | `annexb`                            | H.264 in Annex B byte-stream format (TS container; `-bsf:v h264_mp4toannexb`). Verified via bitstream-head start-code scan.                                                                                                             |
| `edge_avcc_stream.mp4`           | `avcc`                              | H.264 in AVCC (length-prefixed NALU) bitstream format (MP4 default). Paired with `edge_annexb_stream.ts` for A/B testing.                                                                                                               |
| `edge_fragmented_mp4.mp4`        | `fragmented_mp4`                    | fMP4 with `-movflags +frag_keyframe+empty_moov+default_base_moof`. Verified via `moof` box scan in `verify.py`.                                                                                                                         |
| `edge_missing_metadata.mp4`      | `missing_metadata`                  | MP4 with container + stream metadata stripped (`-map_metadata -1 -fflags +bitexact`). Verified via absence of `encoder` tag.                                                                                                            |
| `edge_edit_list.mp4`             | `edit_list`, `nonzero_start_time`   | MP4 with a 500 ms input offset (`-itsoffset 0.5`), which the MP4 muxer emits as an `edts/elst` edit-list entry. Tests players that ignore edit lists and start a half-second early.                                                     |
| `edge_truncated.mp4`             | `truncated`, `corrupt_tail`         | H.264 MP4 with `-movflags +faststart` (moov at front), **truncated to 50 %** of byte length after encoding via a post-process hook. Header parses; decode fails partway through. `verify.py` skips the decode smoke test for this flag. |

### What the overlay exposes on the timestamp edge cases

The burned-in clock comes from the source master and is therefore *unaffected* by any timestamp
transform applied during the encode. On these three variants that mismatch is the point — the
on-screen value and the player's position disagree in a way that names the defect:

| Variant             | On-screen behavior                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------ |
| `edge_variable_fps` | Burned PTS advances linearly while playback speed ramps, making the `setpts` remap visible per frame.  |
| `edge_broken_pts`   | Burned clock stays continuous across the injected 5 s container jump, isolating it to the timestamps.  |
| `edge_edit_list`    | Burned PTS trails the player position by the 0.5 s `itsoffset`, showing the edit list being honored.    |

---

## Chapters

Four well-formed chapter variants live in the video section; six malformed ones live in edge cases.
The malformed set is the interesting half — each row records what the muxer actually produced,
which is not always what was authored:

| Variant | Authored | Result |
| --- | --- | --- |
| `chapters_mp4_10` / `chapters_mkv_10` | 10 even | 10 |
| `chapters_mkv_unicode` | 6, non-ASCII titles | 6, all titles round-trip |
| `chapters_mkv_100` | 100 | 100 |
| `edge_chapters_nonzero_start` | 3, first at 10% | 3 |
| `edge_chapters_overlap` | 3 overlapping | 3, overlaps intact |
| `edge_chapters_zero_length` | 3, one zero-length | 2 — one dropped |
| `edge_chapters_past_eof` | 2, last to 180% | 2, end clamped |
| `edge_chapters_out_of_order` | 3 descending | 1 — rest discarded |
| `edge_chapters_stripped` | 5, then stripped | 0 |

---

## External (10 slots — user-supplied)

Entries that **cannot be synthesized with stock ffmpeg** due to proprietary encoders, licensed metadata tooling, or a need for real-world content. Each slot has a declared `source_path` under `sources/external/`. If the file is present, the pipeline copies it into `generated/external/`, probes it, and writes an NFO. If absent, the slot is reported as `pending_external` and skipped without blocking the rest of the build.

| Variant                         | Expected path                            | Why external                                                                                                                    |
| ------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `external_dolby_vision_p5`      | `sources/external/dolby_vision_p5.mp4`   | HEVC Main10 with Dolby Vision RPU NALs — Profile 5 (IPT-PQ-C2, single-layer, not HDR10-compatible). Requires Dolby RPU tooling. |
| `external_dolby_vision_p8_1`    | `sources/external/dolby_vision_p8_1.mp4` | Dolby Vision Profile 8.1 — base layer is HDR10 so HDR10 devices get valid playback; DV-capable devices use the RPU.             |
| `external_hdr10_plus`           | `sources/external/hdr10plus.mp4`         | HEVC Main10 with HDR10+ dynamic tone-mapping metadata (SMPTE ST 2094-40). Requires `hdr10plus_tool` or a real sample.           |
| `external_dts_hd_ma`            | `sources/external/dts_hd_ma_5_1.mkv`     | DTS-HD Master Audio 5.1 lossless. ffmpeg can decode but not encode.                                                             |
| `external_truehd_atmos`         | `sources/external/truehd_atmos.mkv`      | Dolby TrueHD with Atmos object audio. TrueHD encoder is proprietary.                                                            |
| `external_eac3_joc_atmos`       | `sources/external/eac3_joc_atmos.mp4`    | Dolby Digital Plus with JOC (Atmos). JOC metadata requires Dolby tooling.                                                       |
| `external_4k_hevc_real_content` | `sources/external/4k_hevc_hdr10.mkv`     | Real-world 4K HEVC HDR10 content (not a synthetic test pattern) for realistic pipeline stress.                                  |
| `external_vc1_bluray`           | `sources/external/vc1_bluray.m2ts`       | VC-1 (SMPTE 421M) video, typical Blu-ray container. ffmpeg has no VC-1 encoder — sample must come from a licensed source.       |

See [../sources/external/EXTERNAL_SAMPLES.md](../sources/external/EXTERNAL_SAMPLES.md) for the workflow of adding new slots or providing samples.

---

## Cross-cutting coverage

Breaking down what the 58 synthesizable variants collectively exercise:

| Dimension          | Values exercised                                                                                                                                                                 |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Video codecs       | H.264 Baseline / Main / High, HEVC Main / Main10 (SDR + HDR10 + HLG), SVT-AV1, VP9, **MPEG-2**, **MPEG-4 Part 2 (Xvid-tagged)**, **WMV2**                                        |
| Audio codecs       | AAC (mono / stereo / 5.1 / 7.1), AC-3 (stereo & 5.1), E-AC-3 (stereo & 5.1), Opus, MP3, FLAC (16-bit + **24-bit**), PCM s16le (stereo → 9.1.4), **PCM s24le**, **MP2**, **WMA2** |
| Containers         | MP4, MKV, TS, WebM, M4A, Ogg, WAV, MP3, AC3, EAC3, FLAC, **AVI**, **WMV/ASF**                                                                                                    |
| Subtitle formats   | SRT, WebVTT, ASS, mov_text (3GPP)                                                                                                                                                |
| Resolutions        | 1280×720 (primary), 1920×1080 (HDR / 1080i), 720×540 (4:3 SD), 720×1280 (9:16 vertical), 720×576 (PAL anamorphic), 720×480 (480i), 640×480 (Xvid)                                |
| Frame rates        | 24 fps (primary), 25 fps (PAL), 29.97 fps (NTSC / 1080i), 30 fps, 60 fps                                                                                                         |
| Aspect ratios      | 16:9, 4:3, 9:16, **anamorphic (SAR 64:45)**                                                                                                                                      |
| Bit depths         | 8-bit (yuv420p), 10-bit (yuv420p10le) video; **16-bit + 24-bit** audio                                                                                                           |
| HDR modes          | SDR BT.709 8-bit, SDR BT.709 10-bit, HDR10 BT.2020 PQ, HLG BT.2020                                                                                                               |
| Audio sample rates | 48 kHz (primary), 44.1 kHz (music), **96 kHz (hi-res)**, **192 kHz (studio)**                                                                                                    |
| Audio layouts      | Mono, stereo, 2.1, 5.1, **7.1**, **7.1.4 bed**, **9.1.4 Atmos-style bed**                                                                                                        |
| Scan type          | Progressive, **interlaced (tt / top-field-first)** at 1080i and 480i                                                                                                             |
| Streaming          | HLS VOD, DASH VOD                                                                                                                                                                |
| Track counts       | Single / dual audio, single / triple subtitle, video-only, audio-only                                                                                                            |
| Dispositions       | Default, forced                                                                                                                                                                  |
| Bitstream formats  | H.264 Annex B (start codes), H.264 AVCC (length-prefixed)                                                                                                                        |
| Container edges    | Fragmented MP4, edit list, missing metadata, truncated, variable FPS, broken PTS                                                                                                 |
| Bitrates           | 150 kbps (low) → 80 Mbps (stress)                                                                                                                                                |

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
