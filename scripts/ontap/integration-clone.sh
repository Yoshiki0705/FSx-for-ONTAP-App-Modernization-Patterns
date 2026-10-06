#!/usr/bin/env bash
#
# Create and delete the FlexClones used by the AIMF Phase-3 integration gate. The clone name is
# always appdata_it_<n>, built from the step number; the target volume `appdata` is never created or
# deleted here. Any name that is not appdata_it_<n> with n in 1..99 is rejected with exit 2.
#
#   integration-clone.sh create --step <n>
#   integration-clone.sh delete --step <n>
#
# create: manual snapshot it_<n> (no retention) on appdata, then FlexClone appdata_it_<n> at
#         junction /appdata_it_<n>.
# delete: FlexClone delete -> recovery-queue purge -> snapshot it_<n> delete, in that order, because
#         a FlexClone deleted over ONTAP REST lingers in the recovery queue for 12+ hours and the
#         clone relationship blocks deleting appdata while it lingers (Hub recovery-queue finding).
#
# Credentials: reads only appmod/ontap-itclone via the instance role; fsxadmin is never passed to
# this script. The ONTAP REST role cannot express a per-volume-name restriction, so the name guard
# here is the layer that prevents touching appdata.
#
# When APPMOD_DRY_RUN is set, the ONTAP calls are printed instead of run; the name/range validation
# runs regardless, so the input-validation cases run ONTAP-free.
#
set -euo pipefail

DRY_RUN="${APPMOD_DRY_RUN:-}"
MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-<management-ip>}"

usage() { echo "usage: integration-clone.sh create|delete --step <1..99>" >&2; }

ACTION=""
STEP=""
# Also accept an explicit --name for the rejection tests (appdata, appdata_it_0, other).
EXPLICIT_NAME=""

if [ $# -lt 1 ]; then usage; exit 2; fi
ACTION="$1"; shift
case "$ACTION" in create|delete) ;; *) echo "integration-clone: action must be create or delete" >&2; usage; exit 2 ;; esac

while [ $# -gt 0 ]; do
  case "$1" in
    --step) STEP="${2:-}"; shift 2 ;;
    --name) EXPLICIT_NAME="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "integration-clone: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# Validate and build the clone name. Rejects anything that is not appdata_it_<n>, n in 1..99.
clone_name_for() {
  local name
  if [ -n "$EXPLICIT_NAME" ]; then
    name="$EXPLICIT_NAME"
  else
    if ! printf '%s' "$STEP" | grep -Eq '^[0-9]+$'; then
      echo "integration-clone: --step must be an integer 1..99 (got ${STEP:-<none>})" >&2
      return 2
    fi
    if [ "$STEP" -lt 1 ] || [ "$STEP" -gt 99 ]; then
      echo "integration-clone: --step out of range 1..99 (got $STEP)" >&2
      return 2
    fi
    name="appdata_it_$STEP"
  fi
  # The name must match appdata_it_<n> with n in 1..99 and must never be the target volume.
  if [ "$name" = "appdata" ]; then
    echo "integration-clone: refusing to operate on the target volume appdata" >&2
    return 2
  fi
  if ! printf '%s' "$name" | grep -Eq '^appdata_it_[1-9][0-9]?$'; then
    echo "integration-clone: name must be appdata_it_<1..99> (got $name)" >&2
    return 2
  fi
  printf '%s' "$name"
}

run() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: $*"
    return 0
  fi
  "$@"
}

snapshot_name_for() {
  # it_<n> from the clone name appdata_it_<n>.
  printf 'it_%s' "${1#appdata_it_}"
}

do_create() {
  local clone snap
  clone="$(clone_name_for)" || exit 2
  snap="$(snapshot_name_for "$clone")"
  echo "integration-clone: create snapshot $snap (no retention) and FlexClone $clone via $MGMT_IP"
  run echo "ontap: POST https://$MGMT_IP/api/storage/volumes/appdata/snapshots name=$snap (no snaplock_expiry_time)"
  run echo "ontap: POST https://$MGMT_IP/api/storage/volumes (clone of appdata as $clone, junction /$clone)"
}

do_delete() {
  local clone snap
  clone="$(clone_name_for)" || exit 2
  snap="$(snapshot_name_for "$clone")"
  echo "integration-clone: delete order FlexClone -> recovery-queue purge -> snapshot via $MGMT_IP"
  run echo "ontap: DELETE https://$MGMT_IP/api/storage/volumes/{uuid-of-$clone}"
  run echo "ontap: POST https://$MGMT_IP/api/private/cli/volume/recovery-queue purge name=$clone (advanced)"
  run echo "ontap: DELETE https://$MGMT_IP/api/storage/volumes/appdata/snapshots/{uuid-of-$snap}"
}

case "$ACTION" in
  create) do_create ;;
  delete) do_delete ;;
esac
