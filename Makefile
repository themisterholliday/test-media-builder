PY ?= python3
ROOT := $(shell pwd)
JOBS ?= 1

.PHONY: help bootstrap generate check clean clean-generated dry-run force rebuild verify manifest thumbs all

help:
	@echo "Targets:"
	@echo "  bootstrap       synthesize missing source assets under sources/"
	@echo "  generate        build the full matrix (skips existing outputs)"
	@echo "  force           rebuild every matrix entry from scratch"
	@echo "  rebuild         clean generated/ then build the full matrix"
	@echo "  check           report what would be built (no actions)"
	@echo "  dry-run         print ffmpeg commands without executing"
	@echo "  verify          post-creation validation (decode + contract + nfo + bitstream)"
	@echo "  manifest        write generated/manifest.json index"
	@echo "  thumbs          extract per-variant thumbnails + contact sheet"
	@echo "  all             generate + verify + manifest + thumbs"
	@echo "  clean-generated remove everything under generated/"
	@echo "  clean           remove generated/ and synthesized sources"
	@echo ""
	@echo "  Flags (all targets):  ONLY=video,edge  VARIANT=h264_mp4_baseline  JOBS=4"

bootstrap:
	$(PY) -m scripts.generate --bootstrap --check

generate:
	$(PY) -m scripts.generate --jobs $(JOBS) $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

force:
	$(PY) -m scripts.generate --force --jobs $(JOBS) $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

dry-run:
	$(PY) -m scripts.generate --dry-run $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

check:
	$(PY) -m scripts.generate --check $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

verify:
	$(PY) -m scripts.verify $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

manifest:
	$(PY) -m scripts.manifest $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

thumbs:
	$(PY) -m scripts.thumbnails $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

all: generate verify manifest thumbs

rebuild: clean-generated generate

clean-generated:
	rm -rf generated

clean: clean-generated
	rm -f sources/source_master.mov sources/source_audio.wav sources/source_sub.ass
