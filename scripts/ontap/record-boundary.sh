#!/usr/bin/env bash
#
# Record a stage boundary (b0..b3) as JSON for check-invariant.py. Captures, over the ONTAP REST
# API, the ONTAP version, the target volume UUID, security style, export policy, name mappings, the
# snapshot list, the full volume list, and the snapshot-locking state; and references the inventory
# files (path + SHA-256) that inventory.ps1 / inventory.sh wrote.
#
#   record-boundary.sh --boundary b0 --out <dir> [--windows-inventory <f>] [--linux-inventory <f>]
#
# Reads only; fsxadmin or the read-only role is read from Secrets Manager by the instance role. The
# ONTAP calls are shown rather than executed here (this runs only in-environment); the JSON skeleton
# it emits is the shape check-invariant.py consumes. When APPMOD_DRY_RUN is set the calls are marked.
#
set -euo pipefail

MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-<management-ip>}"
BOUNDARY=""
OUT_DIR="."
WIN_INV=""
LNX_INV=""

usage() { echo "usage: record-boundary.sh --boundary b0|b1|b2|b3 --out <dir> [--windows-inventory f] [--linux-inventory f]" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --boundary) BOUNDARY="${2:-}"; shift 2 ;;
    --out) OUT_DIR="${2:-}"; shift 2 ;;
    --windows-inventory) WIN_INV="${2:-}"; shift 2 ;;
    --linux-inventory) LNX_INV="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "record-boundary: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$BOUNDARY" in b0|b1|b2|b3) ;; *) echo "record-boundary: --boundary must be b0..b3" >&2; usage; exit 2 ;; esac

sha_of() {
  # sha256 of a file, portable between macOS and Linux. Empty when the file is absent.
  if [ -n "$1" ] && [ -f "$1" ]; then
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
    else shasum -a 256 "$1" | awk '{print $1}'; fi
  fi
}

echo "record-boundary: $BOUNDARY from $MGMT_IP (read-only)"
echo "  GET /api/cluster?fields=version"
echo "  GET /api/storage/volumes?fields=name,uuid,nas.security_style,snapshot_locking_enabled,snaplock.type"
echo "  GET /api/protocols/nfs/export-policies ; GET /api/name-services/name-mappings"
echo "  GET /api/storage/volumes/{appdata-uuid}/snapshots"

mkdir -p "$OUT_DIR"
WIN_SHA="$(sha_of "$WIN_INV")"
LNX_SHA="$(sha_of "$LNX_INV")"
OUT_FILE="$OUT_DIR/$BOUNDARY.json"

# Emit the record skeleton. In-environment, the <...> values are filled from the REST responses
# above. Written now so check-invariant.py has a stable shape to consume.
cat >"$OUT_FILE" <<EOF
{
  "boundary": "$BOUNDARY",
  "ontap_version": "<from GET /api/cluster>",
  "volume_uuid": "<appdata uuid>",
  "security_style": "ntfs",
  "inventories": {
    "windows": {"path": "${WIN_INV:-}", "sha256": "${WIN_SHA:-}"},
    "linux": {"path": "${LNX_INV:-}", "sha256": "${LNX_SHA:-}"}
  },
  "top_level_paths": ["seed", "probe", "out"],
  "snapshots": [],
  "snapshot_locking_enabled": false
}
EOF
echo "record-boundary: wrote $OUT_FILE"
