#!/usr/bin/env bash
#
# Confirm that NO volume on the file system has snapshot locking enabled and that NO SnapLock volume
# exists, at every stage boundary and before deletion. Enumerates ALL volumes (the SVM root volume
# and any FlexClone included), so a locked volume cannot hide outside a name filter.
#
#   GET /api/storage/volumes?fields=name,uuid,snapshot_locking_enabled,snaplock.type
#
# Fails (exit 3) when any volume has snapshot_locking_enabled true, or a snaplock.type other than
# the non-SnapLock value, OR when the scanned list does not contain the target volume `appdata`
# (an empty or wrong scan must not read as "clean"). Prints the scanned volume names either way.
#
# U25 (field name/value): the volume model exposes a `snaplock` object with a `type` field, and
# SnapLock volumes have type Compliance or Enterprise. The exact string a NON-SnapLock volume
# reports was not confirmed offline; this script treats the set {"", "non_snaplock", "none"} as
# not-locked and anything else as locked, and must be re-verified against the ONTAP REST reference
# for the file system's ONTAP version before this check is relied on.
#
# Runs on the Linux EC2 host via SSM Run Command; fsxadmin (or the read-only role) is read from
# Secrets Manager by the instance role, never passed in argv. When APPMOD_ONTAP_FIXTURE points at a
# JSON file, that is parsed INSTEAD of calling ONTAP, so the judgement runs ONTAP-free in tests.
#
set -euo pipefail

NON_SNAPLOCK_VALUES='"" "non_snaplock" "none"'
FIXTURE="${APPMOD_ONTAP_FIXTURE:-}"
MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-<management-ip>}"
TARGET_VOLUME="appdata"

fetch_volumes() {
  if [ -n "$FIXTURE" ]; then
    cat "$FIXTURE"
    return 0
  fi
  # Real path: read the fsxadmin password from Secrets Manager via the instance role (not shown in
  # argv), then curl the ONTAP REST endpoint. Left as the documented call; the test path uses a
  # fixture so no ONTAP is contacted here.
  local pw
  pw="$(aws --region ap-northeast-1 secretsmanager get-secret-value \
    --secret-id appmod/fsxadmin --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["password"])')"
  curl -sS -u "fsxadmin:$pw" \
    "https://$MGMT_IP/api/storage/volumes?fields=name,uuid,snapshot_locking_enabled,snaplock.type"
  unset pw
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

records = data.get("records", [])
names = [r.get("name", "<unnamed>") for r in records]
print("scanned volumes: " + (", ".join(names) if names else "(none)"))

violations = []
for record in records:
    name = record.get("name", "<unnamed>")
    if record.get("snapshot_locking_enabled") is True:
        violations.append(f"{name}: snapshot_locking_enabled is true")
    snaplock_type = (record.get("snaplock") or {}).get("type", "")
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
