# External samples

This directory holds the media this generator cannot author. Nothing here is committed:
the directory is gitignored apart from this file.

Two kinds of asset live here.

**Seeds** are small bitmap subtitle bitstreams. ffmpeg converts text to text and bitmap
to bitmap, but it will not rasterise text into a bitmap format, so a PGS or VobSub track
has to start from existing bitmaps. The video and audio around them are generated here
as usual.

**Ingests** are whole files in codecs ffmpeg decodes but cannot write — DTS-HD MA,
TrueHD, DTS:X, VC-1 — or carrying signalling no local tool can author, such as Dolby
Vision Profiles 4 and 7.

## Get them

```bash
make fetch                                      # everything with a public URL
make fetch-local MEDIA_ROOT=/path/to/library    # everything without one
make fetch-check                                # what is present, what is not
```

`fetch-local` creates symlinks, and the build hardlinks through them, so a 1.4 GB sample
costs no extra disk. Set `TESTLIB_MEDIA_ROOT` once per shell to skip `MEDIA_ROOT=`.

Each asset is checksum-pinned the first time it is acquired. A later run that finds
different bytes fails instead of silently changing the corpus. Use `make fetch PIN=1` to
record a digest for an asset that has none yet.

## Where each one comes from

Declared in `config/media_matrix.json` under `source_downloads`, and rendered as a table
in [docs/CATALOG.md](../../docs/CATALOG.md#source-assets). Each entry carries a `url`, a
`local_hint`, or both. An asset with only a `local_hint` has no canonical public
download; the hint is the path it sits at inside a media library.

## Licensing

Assume anything that arrives here carries redistribution constraints, and keep it out of
any public repository. A variant's `attribution` field is copied into `manifest.json` and
into every `.nfo`, which is what satisfies the licence wherever an asset travels.

Two sources are known-good for redistribution with attribution: the Netflix Open Content
*Sol Levante* masters (CC BY 4.0), and Dolby's published reference clips. Everything else
should be treated as local-only.

## Add a slot

1. Add a `source_downloads` entry with a `path` and a `url` or `local_hint`.
2. Add the variant that consumes it, with `source_download` naming that entry.
3. `make ids` to mint the variant's UUID.
4. `make fetch` or `make fetch-local`, then `make fetch PIN=1` to pin the digest.
5. `make generate ONLY=external` and `make catalog`.

The full walkthrough is in
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md#external-sample-workflow).
