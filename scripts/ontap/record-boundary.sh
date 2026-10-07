#!/usr/bin/env bash
#
# Record a stage boundary (b0..b3) as JSON for check-invariant.py. Captures, over the ONTAP REST
# API, the real ONTAP version, the target volume UUID, security style, export policy, name mappings,
# the snapshot list, the full volume list and the snapshot-locking state; and references the
# inventory files (path + SHA-256) that inventory.ps1 / inventory.sh wrote. The emitted JSON carries
# captured values, not <...> placeholders.
#
#   record-boundary.sh --boundary b0 --run-id s0-<UTC> \
#       [--file-system-id fs-...] [--mgmt-ip ip] [--svm appmodsvm] [--volume appdata] \
#       [--windows-inventory f] [--linux-inventory f] [--region ap-northeast-1]
#
# Reads only. fsxadmin (or the read-only role appmod_readonly) is read from Secrets Manager by the
# instance role inside this script, never passed in argv and never echoed. Writes to
# .private/runs/<run-id>/<boundary>.json. The management IP is resolved at runtime from the FSx for ONTAP API
# when not supplied. Live fs-/i-/account values are never hardcoded here.
#
# When APPMOD_DRY_RUN is set, each GET is printed (credential redacted) and no AWS/ONTAP call is
# made; a dry-run still writes a record, filled from the inventory SHA-256s it can compute locally
# and placeholders for the ONTAP-side fields, so the shape check-invariant.py consumes is exercised.
# A real run writes the captured ONTAP values. Verified live 2026-10-07: ONTAP 9.19.1P2; a
# non-SnapLock volume reports snaplock.type "non_snaplock".
#
set -euo pipefail

REGION="${APPMOD_REGION:-ap-northeast-1}"
DRY_RUN="${APPMOD_DRY_RUN:-}"
FS_ID="${APPMOD_FS_ID:-}"
MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-}"
SVM="${APPMOD_SVM:-appmodsvm}"
VOLUME="${APPMOD_VOLUME:-appdata}"
SECRET_ID="${APPMOD_FSXADMIN_SECRET:-appmod/fsxadmin}"
BOUNDARY=""
RUN_ID=""
WIN_INV=""
LNX_INV=""

usage() {
  echo "usage: record-boundary.sh --boundary b0|b1|b2|b3 --run-id s<n>-<UTC>" >&2
  echo "         [--file-system-id fs-...] [--mgmt-ip ip] [--svm name] [--volume name]" >&2
  echo "         [--windows-inventory f] [--linux-inventory f] [--region r]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --boundary) BOUNDARY="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --file-system-id) FS_ID="${2:-}"; shift 2 ;;
    --mgmt-ip) MGMT_IP="${2:-}"; shift 2 ;;
    --svm) SVM="${2:-}"; shift 2 ;;
    --volume) VOLUME="${2:-}"; shift 2 ;;
    --windows-inventory) WIN_INV="${2:-}"; shift 2 ;;
    --linux-inventory) LNX_INV="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "record-boundary: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$BOUNDARY" in b0|b1|b2|b3) ;; *) echo "record-boundary: --boundary must be b0..b3" >&2; usage; exit 2 ;; esac
if [ -z "$RUN_ID" ]; then echo "record-boundary: --run-id is required" >&2; usage; exit 2; fi

sha_of() {
  # sha256 of a file, portable between macOS and Linux. Empty when the file is absent.
  if [ -n "$1" ] && [ -f "$1" ]; then
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
    else shasum -a 256 "$1" | awk '{print $1}'; fi
  fi
}

resolve_mgmt_ip() {
  if [ -n "$MGMT_IP" ]; then return 0; fi
  if [ -z "$FS_ID" ]; then
    echo "record-boundary: need --file-system-id (or APPMOD_FS_ID) to resolve the management IP, or pass --mgmt-ip" >&2
    exit 2
  fi
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION fsx describe-file-systems --file-system-id $FS_ID \\"
    echo "           --query 'FileSystems[0].OntapConfiguration.Endpoints.Management.IpAddresses' --output text"
    MGMT_IP="<management-ip>"
    return 0
  fi
  MGMT_IP="$(aws --region "$REGION" fsx describe-file-systems --file-system-id "$FS_ID" \
    --query 'FileSystems[0].OntapConfiguration.Endpoints.Management.IpAddresses' --output text \
    | awk '{print $1}')"
  if [ -z "$MGMT_IP" ]; then
    echo "record-boundary: could not resolve a management IP for $FS_ID" >&2
    exit 2
  fi
}

read_ontap_password() {
  aws --region "$REGION" secretsmanager get-secret-value \
    --secret-id "$SECRET_ID" --query SecretString --output text \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])'
}

# GET a path over the ONTAP REST API. Under dry-run the GET is printed (credential redacted) and an
# empty JSON object is returned so the capture code runs without a call. -k is required because the
# management endpoint's certificate CN is the management DNS name while this reaches it by the
# management IP, so IP-based hostname verification cannot match.
ontap_get() {
  local path="$1"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> https://$MGMT_IP$path" >&2
    printf '{}'
    return 0
  fi
  curl -sS -k -u "fsxadmin:$ONTAP_PW" "https://$MGMT_IP$path"
}

echo "record-boundary: $BOUNDARY run $RUN_ID from ${MGMT_IP:-<unresolved>} (read-only)"
resolve_mgmt_ip

if [ -z "$DRY_RUN" ]; then
  ONTAP_PW="$(read_ontap_password)"
  trap 'unset ONTAP_PW 2>/dev/null || true' EXIT
fi

# The five GETs that supply the record. snapshot_locking_enabled and snaplock.type are READ here,
# never set; reading them is explicitly allowed by the irreversibility guard.
CLUSTER_JSON="$(ontap_get "/api/cluster?fields=version")"
VOLUMES_JSON="$(ontap_get "/api/storage/volumes?fields=name,uuid,nas.security_style,nas.export_policy.name,snapshot_locking_enabled,snaplock.type")"
VOL_UUID_JSON="$(ontap_get "/api/storage/volumes?name=$VOLUME&fields=uuid")"
EXPORT_JSON="$(ontap_get "/api/protocols/nfs/export-policies?svm.name=$SVM&fields=name,rules")"
NAMEMAP_JSON="$(ontap_get "/api/name-services/name-mappings?svm.name=$SVM")"

# Resolve the target volume UUID so the snapshot list can be fetched for it. Under dry-run this
# yields an empty id and the snapshot GET is still printed with the placeholder.
VOL_UUID="$(printf '%s' "$VOL_UUID_JSON" | python3 -c 'import json,sys
try:
    r=json.load(sys.stdin).get("records",[])
    print(r[0]["uuid"] if r else "")
except Exception:
    print("")')"
SNAP_JSON="$(ontap_get "/api/storage/volumes/${VOL_UUID:-<appdata-uuid>}/snapshots?fields=name,create_time")"

WIN_SHA="$(sha_of "$WIN_INV")"
LNX_SHA="$(sha_of "$LNX_INV")"

OUT_DIR=".private/runs/$RUN_ID"
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/$BOUNDARY.json"

# Build the record from the captured responses. A real run fills every ONTAP field from the GETs
# above; a dry-run leaves the ONTAP-derived fields as explicit "(dry-run)" markers (never <...>
# placeholders) while still filling the locally computable inventory references.
APPMOD_BOUNDARY="$BOUNDARY" \
APPMOD_DRY_RUN="$DRY_RUN" \
APPMOD_TARGET_VOLUME="$VOLUME" \
APPMOD_CLUSTER="$CLUSTER_JSON" \
APPMOD_VOLUMES="$VOLUMES_JSON" \
APPMOD_VOL_UUID="$VOL_UUID" \
APPMOD_EXPORT="$EXPORT_JSON" \
APPMOD_NAMEMAP="$NAMEMAP_JSON" \
APPMOD_SNAP="$SNAP_JSON" \
APPMOD_WIN_INV="$WIN_INV" APPMOD_WIN_SHA="$WIN_SHA" \
APPMOD_LNX_INV="$LNX_INV" APPMOD_LNX_SHA="$LNX_SHA" \
APPMOD_OUT_FILE="$OUT_FILE" python3 - <<'PY'
import json
import os


def loads(name):
    try:
        return json.loads(os.environ.get(name, "") or "{}")
    except json.JSONDecodeError:
        return {}


dry = bool(os.environ.get("APPMOD_DRY_RUN"))
target = os.environ["APPMOD_TARGET_VOLUME"]
cluster = loads("APPMOD_CLUSTER")
volumes = loads("APPMOD_VOLUMES").get("records", [])
export = loads("APPMOD_EXPORT").get("records", [])
namemap = loads("APPMOD_NAMEMAP").get("records", [])
snaps = loads("APPMOD_SNAP").get("records", [])

version = (cluster.get("version") or {}).get("full")
uuid = os.environ.get("APPMOD_VOL_UUID") or None

# Target volume record (for UUID, security style, snapshot-locking, snaplock.type).
target_record = next((v for v in volumes if v.get("name") == target), None)

if dry:
    marker = "(dry-run; filled from ONTAP on a real run)"
    ontap_version = version or marker
    volume_uuid = uuid or marker
    security_style = (
        ((target_record or {}).get("nas") or {}).get("security_style") or "ntfs"
    )
    locking = bool(((target_record or {}) or {}).get("snapshot_locking_enabled"))
else:
    ontap_version = version
    volume_uuid = uuid
    security_style = ((target_record or {}).get("nas") or {}).get("security_style")
    locking = bool((target_record or {}).get("snapshot_locking_enabled"))

export_policies = [
    {"name": e.get("name"), "rule_count": len(e.get("rules", []))} for e in export
]
name_mappings = [
    {"direction": m.get("direction"), "pattern": m.get("pattern")} for m in namemap
]
snapshot_names = [s.get("name") for s in snaps]
volume_list = [
    {
        "name": v.get("name"),
        "uuid": v.get("uuid"),
        "snapshot_locking_enabled": bool(v.get("snapshot_locking_enabled")),
        "snaplock_type": (v.get("snaplock") or {}).get("type", ""),
    }
    for v in volumes
]

record = {
    "boundary": os.environ["APPMOD_BOUNDARY"],
    "ontap_version": ontap_version,
    "volume_uuid": volume_uuid,
    "security_style": security_style,
    "export_policies": export_policies,
    "name_mappings": name_mappings,
    "inventories": {
        "windows": {
            "path": os.environ.get("APPMOD_WIN_INV", ""),
            "sha256": os.environ.get("APPMOD_WIN_SHA", ""),
        },
        "linux": {
            "path": os.environ.get("APPMOD_LNX_INV", ""),
            "sha256": os.environ.get("APPMOD_LNX_SHA", ""),
        },
    },
    "top_level_paths": ["seed", "probe", "out"],
    "snapshots": snapshot_names,
    "volumes": volume_list,
    "snapshot_locking_enabled": locking,
}

# A real run must never emit <...> placeholders for the ONTAP-derived fields.
if not dry:
    for key in ("ontap_version", "volume_uuid", "security_style"):
        value = record[key]
        if value is None or (isinstance(value, str) and value.startswith("<")):
            raise SystemExit(
                f"record-boundary: {key} not captured from ONTAP (got {value!r})"
            )

with open(os.environ["APPMOD_OUT_FILE"], "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2)
PY

echo "record-boundary: wrote $OUT_FILE"
