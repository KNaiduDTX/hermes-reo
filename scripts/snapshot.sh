#!/usr/bin/env bash
# Audit TEST-4 / RL6: data/ and out/ are gitignored SOLE COPIES of hours of
# scraping (166 CPRs, caches, comps DB, history appends). A checksummed local
# snapshot + manifest beats "die with the disk".
#   usage: bash scripts/snapshot.sh [dest-dir]
#   restore: see the "Snapshot & restore" section of USER_GUIDE.md
set -euo pipefail
DEST="${1:-$HOME/.hermes-reo-snapshots}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
STAMP="$(date -u +%Y-%m-%d)"
mkdir -p "$DEST"
TAR="$DEST/hermes-data-${TS}.tar.gz"
# Tar the gitignored sole copies (exclude caches that are safe to rebuild).
tar -czf "$TAR" -C "$REPO" \
  data/cprs data/comps data/history data/current_deals.json \
  data/dcad_cache.json data/geocode_failures.json data/run_summary.json \
  2>/dev/null || true
# SHA-256 manifest alongside the archive.
( cd "$REPO" && find data/cprs data/comps data/history -type f \
    -print0 2>/dev/null | sort -z | xargs -0 shasum -a 256 \
    > "$DEST/hermes-data-${TS}.sha256" ) || true
shasum -a 256 "$TAR" >> "$DEST/hermes-data-${TS}.sha256"
echo "snapshot: $TAR"
echo "manifest: $DEST/hermes-data-${TS}.sha256"
