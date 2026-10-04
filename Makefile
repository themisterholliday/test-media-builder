PY ?= python3
ROOT := $(shell pwd)
JOBS ?= 1

.PHONY: help bootstrap generate check clean clean-generated dry-run force rebuild verify manifest thumbs library ids catalog plexmatch servers servers-down servers-reset plex-reset-library plex-claim plex-token plex-status plex-setup servercheck all

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
	@echo "  ids             mint UUIDs for variants added to the matrix by hand"
	@echo "  catalog         regenerate docs/CATALOG.md from the matrix"
	@echo "  plexmatch       join manifest variants to a Plex library (WRITE_LABELS=1 to stamp)"
	@echo "  plex-reset-library  wipe the Plex library but keep the claim"
	@echo "  plex-claim      claim the test server (make plex-claim TOKEN=claim-xxxx)"
	@echo "  plex-token      store the server token in docker/.env (after claiming)"
	@echo "  plex-status     show which server answers, and whether it is claimed"
	@echo "  plex-setup      create the three Plex libraries and scan"
	@echo "  servers-down    stop the servers, keeping their config"
	@echo "  servers-reset   stop and wipe all server config"
	@echo "  all             generate + verify + manifest + thumbs + library + catalog"
	@echo "  clean-generated remove everything under generated/"
	@echo "  clean           remove generated/ and synthesized sources"
	@echo ""
	@echo "  Flags (all targets):  ONLY=video,edge  VARIANT=h264_mp4_baseline  JOBS=4"
	@echo "  build flags:          JOBS=N  ONLY=section,..  VARIANT=name,..  FORCE=1  DURATION=SECONDS"
	@echo "  library flags:        LINK_MODE=hardlink|symlink|copy  FACETS=codec,container,..."

bootstrap:
	$(PY) -m scripts.generate --bootstrap --check

generate:
	$(PY) -m scripts.generate --jobs $(JOBS) $(if $(FORCE),--force) $(if $(DURATION),--duration $(DURATION)) $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

force:
	$(PY) -m scripts.generate --force --jobs $(JOBS) $(if $(DURATION),--duration $(DURATION)) $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

dry-run:
	$(PY) -m scripts.generate --dry-run $(if $(DURATION),--duration $(DURATION)) $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

check:
	$(PY) -m scripts.generate --check $(if $(DURATION),--duration $(DURATION)) $(if $(ONLY),--only $(ONLY)) $(if $(VARIANT),--variant $(VARIANT))

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
	@echo "plex:     http://localhost:$${PLEX_PORT:-32402}"
	@echo "jellyfin: http://localhost:$${JELLYFIN_PORT:-8096}"
	@echo "run 'make servercheck' once both have finished scanning"

servers-down:
	$(COMPOSE) down

servers-reset:
	$(COMPOSE) down -v

# Wipe the Plex library but keep Preferences.xml, which holds the claim and the
# machine identifier the write guard is pinned to. Use this instead of
# servers-reset once the server is claimed: a reset needs a fresh claim token,
# this does not.
PLEX_CFG = /config/Library/Application Support/Plex Media Server
plex-reset-library:
	$(COMPOSE) stop plex
	$(DOCKER) run --rm -v testlib_plex-config:/config alpine:latest sh -c \
	  'rm -rf "$(PLEX_CFG)/Plug-in Support/Databases" "$(PLEX_CFG)/Media" "$(PLEX_CFG)/Metadata"'
	$(COMPOSE) start plex
	@echo "library wiped, claim kept — now run 'make plex-setup'"

# Claim the running test server to a Plex account:
#   make plex-claim TOKEN=claim-xxxxxxxx
# Get the token from https://plex.tv/claim. It expires about four minutes after it
# is issued, so fetch it immediately before running this. Works against a running
# server, so no reset is needed and a retry costs nothing.
plex-claim:
ifndef TOKEN
	@echo "usage: make plex-claim TOKEN=claim-xxxxxxxx"
	@echo "get a token from https://plex.tv/claim (expires in ~4 minutes)"
	@exit 1
endif
	@case "$(TOKEN)" in claim-*) ;; \
	  *) echo "'$(TOKEN)' is not a claim token (expected it to start with 'claim-')"; exit 1 ;; esac
	$(PY) -m scripts.plexsetup --claim "$(TOKEN)" --no-scan

# Report which server answers, whether it is claimed, and whether it is the pinned
# test container. Read-only.
# Store the claimed server's own token in docker/.env (gitignored). With a token the
# API needs no ALLOWED_NETWORKS, so the LAN gets no unauthenticated access.
plex-token:
	$(PY) -m scripts.plexsetup --save-token

plex-status:
	@$(PY) -m scripts.plexsetup --status

plex-setup:
	$(PY) -m scripts.plexsetup $(if $(CLAIM),--claim $(CLAIM)) $(if $(NAME),--name '$(NAME)')

servercheck:
	$(PY) -m scripts.servercheck

# Mint a UUID for any variant added to the matrix by hand. Existing ids are never
# changed. Everything downstream refuses to run until every variant has one.
ids:
	$(PY) -m scripts.ids

catalog:
	$(PY) -m scripts.catalog

plexmatch:
	$(PY) -m scripts.plexmatch \
		$(if $(WRITE_LABELS),--write-labels) \
		$(if $(ALL),--all) \
		$(if $(JSON),--json $(JSON))

all: generate verify manifest thumbs library catalog

rebuild: clean-generated generate

clean-generated:
	rm -rf generated library

clean: clean-generated
	rm -f sources/source_master.mov sources/source_audio.wav sources/source_sub.ass
