PY ?= python3
ROOT := $(shell pwd)
JOBS ?= 1

.PHONY: help bootstrap generate check clean clean-generated dry-run force rebuild verify manifest thumbs library servers servers-down servers-reset plex-setup servercheck all

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
	@echo "  library         build the faceted show tree under library/ (run after generate)"
	@echo "  servers         start Plex + Jellyfin in docker against library/ (read-only)"
	@echo "  servercheck     diff what each server scanned against generated/manifest.json"
	@echo "  plex-setup      create the three Plex libraries (CLAIM=<token> to use Plex apps)"
	@echo "  servers-down    stop the servers, keeping their config"
	@echo "  servers-reset   stop and wipe all server config"
	@echo "  all             generate + verify + manifest + thumbs + library"
	@echo "  clean-generated remove everything under generated/"
	@echo "  clean           remove generated/ and synthesized sources"
	@echo ""
	@echo "  Flags (all targets):  ONLY=video,edge  VARIANT=h264_mp4_baseline  JOBS=4"
	@echo "  library flags:        LINK_MODE=hardlink|symlink|copy  FACETS=codec,container,..."

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

library:
	$(PY) -m scripts.library $(if $(LINK_MODE),--link-mode $(LINK_MODE)) $(if $(FACETS),--facets $(FACETS))

# DOCKER is overridable: Docker Desktop does not always symlink its CLI onto PATH.
DOCKER ?= $(shell command -v docker 2>/dev/null || echo /Applications/Docker.app/Contents/Resources/bin/docker)
COMPOSE := $(DOCKER) compose -f docker/compose.yml

servers:
	$(COMPOSE) up -d
	@echo "plex:     http://localhost:$${PLEX_PORT:-32401}"
	@echo "jellyfin: http://localhost:$${JELLYFIN_PORT:-8096}"
	@echo "run 'make servercheck' once both have finished scanning"

servers-down:
	$(COMPOSE) down

servers-reset:
	$(COMPOSE) down -v
	rm -rf docker/config/plex docker/config/jellyfin

plex-setup:
	$(PY) -m scripts.plexsetup $(if $(CLAIM),--claim $(CLAIM))

servercheck:
	$(PY) -m scripts.servercheck

all: generate verify manifest thumbs library

rebuild: clean-generated generate

clean-generated:
	rm -rf generated library

clean: clean-generated
	rm -f sources/source_master.mov sources/source_audio.wav sources/source_sub.ass
