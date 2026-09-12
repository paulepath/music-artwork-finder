#!/usr/bin/env bash
# Drop to the configured PUID/PGID so writes to /music land with NAS-correct ownership.
set -euo pipefail

PUID="${PUID:-1026}"
PGID="${PGID:-100}"

groupadd -o -g "$PGID" appgrp 2>/dev/null || true
useradd -o -u "$PUID" -g "$PGID" -d /app -M -s /usr/sbin/nologin appusr 2>/dev/null || true

mkdir -p /config/secrets /config/backups /config/image_cache
chown -R "$PUID:$PGID" /config 2>/dev/null || true

# Playwright browsers live under /ms-playwright (root-owned, world-readable) — fine to read.
exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups "$@"
