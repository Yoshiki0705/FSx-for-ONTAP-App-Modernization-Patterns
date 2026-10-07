#!/usr/bin/env bash
#
# Confirm that NO volume on the file system has snapshot locking enabled and that NO SnapLock volume
# exists, at every stage boundary and before deletion. Enumerates ALL volumes (the SVM root volume
# and any FlexClone included), so a locked volume cannot hide outside a name filter.
#
#   GET /api/storage/volumes?fields=name,uuid,snapshot_locking_enabled,snaplock.type
#
# Fails (exit 3) when any volume has snapshot_locking_enabled other than false (absent included),
# or a snaplock.type other than "non_snaplock" (absent included), OR when the scanned list does not contain the target volume `appdata`
# (an empty or wrong scan must not read as "clean"). Prints the scanned volume names either way.
#
# U25 (resolved live 2026-10-07, ONTAP 9.19.1P2): `snaplock.type` is a valid field and a
# non-SnapLock volume reports "non_snaplock". That is the ONLY value accepted as not-locked; an
# absent snaplock object, an empty type, or any other value is a violation. A missing field stops
# teardown for a human rather than reading as clean.
#
# Runs on the Linux EC2 host via SSM Run Command; fsxadmin (or the read-only role) is read from
# Secrets Manager by the instance role, never passed in argv. When APPMOD_ONTAP_FIXTURE points at a
# JSON file, that is parsed INSTEAD of calling ONTAP, so the judgement runs ONTAP-free in tests.
#
#   check-no-locking.sh [--file-system-id fs-... | --mgmt-ip ip] [--region ap-northeast-1]
#
# The management IP is resolved at runtime from describe-file-systems
# (OntapConfiguration.Endpoints.Management.IpAddresses) when --mgmt-ip is not given. curl uses -k:
# the endpoint is reached by IP while its certificate CN is the DNS name (live 2026-10-07).
#
set -euo pipefail

NON_SNAPLOCK_VALUES='"non_snaplock"'
FIXTURE="${APPMOD_ONTAP_FIXTURE:-}"
MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-}"
FS_ID="${APPMOD_FS_ID:-}"
REGION="${APPMOD_REGION:-ap-northeast-1}"
TARGET_VOLUME="appdata"

while [ $# -gt 0 ]; do
  case "$1" in
    --file-system-id) FS_ID="${2:-}"; shift 2 ;;
    --mgmt-ip) MGMT_IP="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    *) echo "check-no-locking: unknown argument: $1" >&2; exit 2 ;;
  esac
done

fetch_volumes() {
  if [ -n "$FIXTURE" ]; then
    cat "$FIXTURE"
    return 0
  fi
  # Real path: resolve the management IP, read the fsxadmin password from Secrets Manager via the
  # instance role, and hand it to curl on stdin (-K -) so it never appears in argv.
  if [ -z "$MGMT_IP" ]; then
    if [ -z "$FS_ID" ]; then
      echo "check-no-locking: need --file-system-id (or APPMOD_FS_ID) or --mgmt-ip" >&2
      exit 2
    fi
    local ips
    ips="$(aws --region "$REGION" fsx describe-file-systems --file-system-id "$FS_ID" \
      --query 'FileSystems[0].OntapConfiguration.Endpoints.Management.IpAddresses' --output text)"
    MGMT_IP="${ips%%[[:space:]]*}"
  fi
  local secret pw
  secret="$(aws --region "$REGION" secretsmanager get-secret-value \
    --secret-id appmod/fsxadmin --query SecretString --output text)"
  pw="$(printf '%s' "$secret" | python3 -c 'import json,sys;print(json.load(sys.stdin)["password"])')"
  secret=""
  pw="${pw//\\/\\\\}"
  printf 'user = "fsxadmin:%s"\n' "${pw//\"/\\\"}" | curl -sS -k -K - \
    "https://$MGMT_IP/api/storage/volumes?fields=name,uuid,snapshot_locking_enabled,snaplock.type"
  pw=""
}

# Judge a volumes response. The JSON is passed via the APPMOD_RESPONSE env var (not stdin), because
# the Python program itself is supplied on stdin via the here-doc. Prints scanned names; returns 3
# on any violation or when the target volume is absent.
judge_volumes() {
  local response
  response="$(cat)"
  APPMOD_TARGET="$TARGET_VOLUME" \
  APPMOD_NON_SNAPLOCK="$NON_SNAPLOCK_VALUES" \
  APPMOD_RESPONSE="$response" python3 - <<'PY'
import json
import os
import sys

target = os.environ["APPMOD_TARGET"]
non_snaplock = set(json.loads("[" + os.environ["APPMOD_NON_SNAPLOCK"].replace(" ", ",") + "]"))

try:
    data = json.loads(os.environ["APPMOD_RESPONSE"])
except json.JSONDecodeError as exc:
    print(f"check-no-locking: response is not JSON: {exc}", file=sys.stderr)
    sys.exit(3)

if not isinstance(data, dict):
    print("check-no-locking: response is not a JSON object", file=sys.stderr)
    sys.exit(3)

# A paginated collection carries _links.next. Judging the first page only could miss a locked
# volume on a later page while appdata on page one satisfies the target check, so refuse it.
if (data.get("_links") or {}).get("next"):
    print("check-no-locking: response is paginated; refusing a partial scan", file=sys.stderr)
    sys.exit(3)

records = data.get("records", [])
names = [r.get("name", "<unnamed>") for r in records]
print("scanned volumes: " + (", ".join(names) if names else "(none)"))

violations = []
for record in records:
    name = record.get("name", "<unnamed>")
    if record.get("snapshot_locking_enabled") is not False:
        violations.append(
            f"{name}: snapshot_locking_enabled is {record.get('snapshot_locking_enabled')!r}"
        )
    snaplock_type = (record.get("snaplock") or {}).get("type")
    if snaplock_type not in non_snaplock:
        violations.append(f"{name}: snaplock.type is {snaplock_type!r}")

if target not in names:
    print(
        f"check-no-locking: target volume {target!r} is not in the scanned list; "
        "the scan is empty or wrong, which must not read as clean",
        file=sys.stderr,
    )
    sys.exit(3)

if violations:
    print("check-no-locking: locking found:", file=sys.stderr)
    for violation in violations:
        print(f"  - {violation}", file=sys.stderr)
    sys.exit(3)

print("check-no-locking: no snapshot locking and no SnapLock volume; appdata present")
PY
}

fetch_volumes | judge_volumes
