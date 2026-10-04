# External samples

Drop user-supplied media here. Each file is declared in
`config/media_matrix.json` under `external_variants` with a fixed `source_path`.

- **Which slots exist, and which are still empty:** [docs/CATALOG.md](../../docs/CATALOG.md#external-10)
- **How the pipeline treats them, and how to add a slot:** [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md#external-sample-workflow)

Run `make check` to see which slots are currently missing.

This directory is gitignored. Assume anything placed here carries redistribution
constraints and keep it out of any public repository.
