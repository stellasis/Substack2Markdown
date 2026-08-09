#!/usr/bin/env bash
# Merge local Substack2Markdown -> droplet (union / remote-superset). No --delete.
# Prefer Git Bash on Windows.
#
#   ./scripts/remote-superset/Sync-RemoteSuperset.sh --dry-run
#   ./scripts/remote-superset/Sync-RemoteSuperset.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${SCRIPT_DIR}/config.json"
DRY_RUN=0
SKIP_STASH=0
SKIP_PUSH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run|-n) DRY_RUN=1 ;;
    --skip-stash) SKIP_STASH=1 ;;
    --skip-push) SKIP_PUSH=1 ;;
    --config) CONFIG="$2"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done

export SS2MD_CONFIG="$CONFIG"
export SS2MD_DRY_RUN="$DRY_RUN"
export SS2MD_SKIP_STASH="$SKIP_STASH"
export SS2MD_SKIP_PUSH="$SKIP_PUSH"
export SS2MD_SCRIPT_DIR="$SCRIPT_DIR"

# Drive logic in Python (path/quoting is painful across Win Python + Git Bash).
# Shell still owns ssh/scp/tar pipes where needed via subprocess.
python "$SCRIPT_DIR/sync_remote_superset.py"
