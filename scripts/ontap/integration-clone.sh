#!/usr/bin/env bash
#
# Create and delete the FlexClones used by the AIMF Phase-3 integration gate, over the ONTAP REST
# API, on the Linux EC2 host (SSM Run Command). The clone name is always appdata_it_<n>; the parent
# volume `appdata` is never created, modified or deleted here. A name that is not appdata_it_<n>
# with n in 1..99 is rejected with exit 2 BEFORE any AWS or ONTAP call.
#
#   integration-clone.sh create --step <n>   [connection options]
#   integration-clone.sh delete --step <n>   [connection options]
#   integration-clone.sh sweep               [connection options]   teardown: delete every leftover
#   integration-clone.sh verify-clean        [connection options]   teardown: recovery queue, clones
#                                                                   and it_* snapshots all empty
#   connection options: --file-system-id fs-... | --mgmt-ip ip, [--svm appmodsvm] [--region r]
#                       [--credential itclone|fsxadmin]   (fsxadmin only for sweep / verify-clean)
#
# create: Snapshot it_<n> on appdata (POST /api/storage/volumes/{appdata-uuid}/snapshots, body
#         carries only the name: no expiry_time, no snaplock_expiry_time, no SnapMirror label), then
#         FlexClone appdata_it_<n> from it (POST /api/storage/volumes with clone.parent_volume.uuid)
#         at junction /appdata_it_<n>.
# delete: in this order, because the snapshot cannot go while a clone depends on it and a clone in
#         the recovery queue keeps the clone relationship that blocks deleting appdata:
#           1. FlexClone: unmount (PATCH nas.path ""), then DELETE /api/storage/volumes/{clone-uuid}
#              with force=true (bypasses the recovery queue; documented for ONTAP 9.12 and later)
#           2. recovery queue: show (GET /api/private/cli/volume/recovery-queue) and purge any entry
#              left for the clone (POST .../recovery-queue/purge). Defense in depth: the SVM already
#              has volume-delete-retention-hours=0 (U27, set live in stage 0). The purge form is
#              UNCONFIRMED (U26)
#           3. Snapshot it_<n>: DELETE /api/storage/volumes/{appdata-uuid}/snapshots/{snapshot-uuid}
#         Before deleting, the clone must be a FlexClone whose parent is appdata, and must not be
#         appdata's UUID; otherwise exit 2. An absent clone (0 records) skips step 1 and still runs
#         2 and 3, so a re-run after a partial delete, or a sweep over an orphan it_<n>, exits 0.
#
# Every path that is SVM- or volume-scoped is keyed by the UUID resolved at runtime. Credentials:
# create/delete read only appmod/ontap-itclone (ONTAP user appmod-itclone) via the instance role.
# teardown.sh runs sweep/verify-clean with --credential fsxadmin, because by then lock-fsxadmin.sh
# is off and check-no-locking.sh has already used fsxadmin. No password is ever echoed.
#
# The ONTAP REST role cannot express a per-volume-name restriction, so the name guard here is the
# layer that prevents touching appdata. No call here enables SnapLock or snapshot locking.
#
# When APPMOD_DRY_RUN is set, every request is printed (credential redacted) and NO AWS, ONTAP or
# network call is made; the name/range validation runs regardless.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ontap/lib-ontap-rest.sh
. "$HERE/lib-ontap-rest.sh"

ONTAP_WHO="integration-clone"
DRY_RUN="${APPMOD_DRY_RUN:-}"
REGION="${APPMOD_REGION:-ap-northeast-1}"
FS_ID="${APPMOD_FS_ID:-}"
MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-}"
SVM="${APPMOD_SVM:-appmodsvm}"
PARENT="appdata"
CREDENTIAL="itclone"
# Poll budget for an asynchronous create/delete to become visible: 60 x 5 s.
POLL_TRIES="${APPMOD_POLL_TRIES:-60}"
POLL_SLEEP="${APPMOD_POLL_SLEEP:-5}"

usage() {
  echo "usage: integration-clone.sh create|delete --step <1..99> [connection options]" >&2
  echo "       integration-clone.sh sweep|verify-clean [connection options]" >&2
  echo "       connection options: --file-system-id fs-... | --mgmt-ip ip, [--svm name] [--region r]" >&2
  echo "                           [--credential itclone|fsxadmin]" >&2
}

ACTION=""
STEP=""
# Also accept an explicit --name for the rejection tests (appdata, appdata_it_0, other).
EXPLICIT_NAME=""

if [ $# -lt 1 ]; then usage; exit 2; fi
ACTION="$1"; shift
case "$ACTION" in
  create|delete|sweep|verify-clean) ;;
  *) echo "integration-clone: action must be create, delete, sweep or verify-clean" >&2; usage; exit 2 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --step) STEP="${2:-}"; shift 2 ;;
    --name) EXPLICIT_NAME="${2:-}"; shift 2 ;;
    --file-system-id) FS_ID="${2:-}"; shift 2 ;;
    --mgmt-ip) MGMT_IP="${2:-}"; shift 2 ;;
    --svm) SVM="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --credential) CREDENTIAL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "integration-clone: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# A name is acceptable only as appdata_it_<n>, n in 1..99, and never the parent volume.
valid_clone_name() {
  local name="$1"
  if [ "$name" = "$PARENT" ]; then
    echo "integration-clone: refusing to operate on the target volume $PARENT" >&2
    return 2
  fi
  if ! [[ "$name" =~ ^appdata_it_[1-9][0-9]?$ ]]; then
    echo "integration-clone: name must be appdata_it_<1..99> (got $name)" >&2
    return 2
  fi
}

# Validate and build the clone name from --step (or --name). Runs before any call.
clone_name_for() {
  local name
  if [ -n "$EXPLICIT_NAME" ]; then
    name="$EXPLICIT_NAME"
  else
    if ! [[ "$STEP" =~ ^[0-9]+$ ]]; then
      echo "integration-clone: --step must be an integer 1..99 (got ${STEP:-<none>})" >&2
      return 2
    fi
    if [ "$STEP" -lt 1 ] || [ "$STEP" -gt 99 ]; then
      echo "integration-clone: --step out of range 1..99 (got $STEP)" >&2
      return 2
    fi
    name="appdata_it_$STEP"
  fi
  valid_clone_name "$name" || return 2
  printf '%s' "$name"
}

snapshot_name_for() {
  # it_<n> from the clone name appdata_it_<n>.
  printf 'it_%s' "${1#appdata_it_}"
}

# Validate everything that does not need a call, in the order: name, credential.
CLONE=""
case "$ACTION" in
  create|delete)
    CLONE="$(clone_name_for)" || exit 2
    if [ "$CREDENTIAL" != "itclone" ]; then
      echo "integration-clone: $ACTION uses only the appmod-itclone credential" >&2
      exit 2
    fi
    ;;
  *)
    if [ -n "$STEP" ] || [ -n "$EXPLICIT_NAME" ]; then
      echo "integration-clone: $ACTION takes no --step or --name" >&2
      exit 2
    fi
    ;;
esac
case "$CREDENTIAL" in
  itclone) ONTAP_ACCOUNT="appmod-itclone"; SECRET_ID="${APPMOD_ITCLONE_SECRET:-appmod/ontap-itclone}" ;;
  fsxadmin) ONTAP_ACCOUNT="fsxadmin"; SECRET_ID="${APPMOD_FSXADMIN_SECRET:-appmod/fsxadmin}" ;;
  *) echo "integration-clone: --credential must be itclone or fsxadmin" >&2; exit 2 ;;
esac

# Wait until GET <path> reports <want> records (0 = gone, 1 = present). Dry-run returns at once.
wait_count() {
  local path="$1" want="$2" what="$3" i n
  if [ -n "$DRY_RUN" ]; then return 0; fi
  for i in $(seq 1 "$POLL_TRIES"); do
    n="$(ontap_count "$path")" || exit 1
    if { [ "$want" = "0" ] && [ "$n" = "0" ]; } || { [ "$want" = "1" ] && [ "$n" != "0" ]; }; then
      return 0
    fi
    sleep "$POLL_SLEEP"
  done
  ontap_die 1 "$what did not reach the expected state after $i checks"
}

connect() {
  ontap_resolve_mgmt_ip
  ontap_note "ONTAP management endpoint: $MGMT_IP (account $ONTAP_ACCOUNT)"
  ontap_login "$ONTAP_ACCOUNT" "$SECRET_ID"
  SVM_UUID="$(ontap_svm_uuid "$SVM")" || exit 1
  PARENT_UUID="$(ontap_volume_uuid "$PARENT" "$SVM")" || exit 1
}

do_create() {
  local clone="$1" snap
  snap="$(snapshot_name_for "$clone")"
  local snaps="/api/storage/volumes/$PARENT_UUID/snapshots"
  local snap_query="$snaps?name=$snap&fields=uuid"
  ontap_note "create snapshot $snap on $PARENT (no retention, no expiry) and FlexClone $clone"
  # Assigned before comparing: a substitution inside [ ] is never checked, so a failed lookup
  # would read as "present" and skip the create.
  local n
  n="$(ontap_count "$snap_query")" || exit 1
  if [ "$n" != "0" ]; then
    ontap_note "snapshot $snap already present; left unchanged"
  else
    # The body carries the name only: no expiry_time, no snaplock_expiry_time, no snapmirror_label.
    ontap_ok POST "$snaps?return_timeout=120" "{\"name\":\"$snap\"}"
    wait_count "$snap_query" 1 "snapshot $snap"
  fi
  local clone_query="/api/storage/volumes?name=$clone&svm.uuid=$SVM_UUID&fields=uuid"
  n="$(ontap_count "$clone_query")" || exit 1
  if [ "$n" != "0" ]; then
    ontap_note "FlexClone $clone already present; left unchanged"
  else
    ontap_ok POST "/api/storage/volumes?return_timeout=120" \
      "{\"name\":\"$clone\",\"svm\":{\"uuid\":\"$SVM_UUID\"},\"clone\":{\"is_flexclone\":true,\"parent_volume\":{\"uuid\":\"$PARENT_UUID\"},\"parent_snapshot\":{\"name\":\"$snap\"}},\"nas\":{\"path\":\"/$clone\"}}"
    wait_count "$clone_query" 1 "FlexClone $clone"
  fi
  # The clone must not carry snapshot locking (it never should; this is a read-only assertion).
  if [ -z "$DRY_RUN" ]; then
    ontap_ok GET "/api/storage/volumes?name=$clone&svm.uuid=$SVM_UUID&fields=snapshot_locking_enabled"
    local locked
    locked="$(ontap_jq 'str((d.get("records") or [{}])[0].get("snapshot_locking_enabled", False))')" \
      || ontap_die 1 "could not parse the snapshot_locking_enabled answer for $clone"
    if [ "$locked" = "True" ]; then
      ontap_die 3 "FlexClone $clone reports snapshot_locking_enabled true; stop and report"
    fi
  fi
  ontap_note "created $clone at junction /$clone"
}

# Resolve the clone and check it is a FlexClone of appdata before anything is deleted.
clone_uuid_checked() {
  local clone="$1"
  local path="/api/storage/volumes?name=$clone&svm.uuid=$SVM_UUID&fields=uuid,clone.is_flexclone,clone.parent_volume.name"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$path" >/dev/null
    printf '<%s-uuid>' "$clone"
    return 0
  fi
  # This function runs inside $(...), where errexit is off: ontap_ok exits on a transport or HTTP
  # error itself, and each parse below is checked explicitly.
  ontap_ok GET "$path"
  # Absent clone: print nothing. Checked on the record count, before any field is parsed.
  local count
  count="$(ontap_jq 'len(d.get("records") or [])')" || ontap_die 1 "could not parse the $clone lookup"
  if [ "$count" = "0" ]; then return 0; fi
  local facts uuid flex parent
  # Unit separator (0x1f), not tab: tab is IFS whitespace, so `read` would collapse an empty
  # leading field and shift is_flexclone into uuid.
  facts="$(ontap_jq '"\x1f".join(str(x) for x in ((lambda r: (r.get("uuid", ""), (r.get("clone") or {}).get("is_flexclone", False), ((r.get("clone") or {}).get("parent_volume") or {}).get("name", "")))((d.get("records") or [{}])[0])))')" \
    || ontap_die 1 "could not parse the $clone record"
  IFS=$'\x1f' read -r uuid flex parent <<EOF
$facts
EOF
  if [ -z "$uuid" ]; then
    ontap_die 1 "$clone record has no uuid; refusing to guess"
  fi
  if [ "$uuid" = "$PARENT_UUID" ] || [ "$flex" != "True" ] || [ "$parent" != "$PARENT" ]; then
    ontap_die 2 "$clone is not a FlexClone of $PARENT (is_flexclone=$flex parent=$parent); refusing to delete"
  fi
  printf '%s' "$uuid"
}

purge_recovery_queue() {
  local clone="$1"
  # Queued volumes are renamed <name>_<id>, hence the wildcard.
  local show="/api/private/cli/volume/recovery-queue?vserver=$SVM&volume=${clone}_*&fields=volume"
  local entries=""
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$show" >/dev/null
    entries="<$clone recovery-queue entry>"
  else
    local status
    status="$(ontap_status GET "$show")" || exit 1
    if [ "$status" -ge 400 ]; then
      ontap_die 1 "recovery-queue show returned HTTP $status (U26: the passthrough or its RBAC for $ONTAP_ACCOUNT is unconfirmed)"
    fi
    entries="$(ontap_jq '[r.get("volume", "") for r in d.get("records", [])]')" \
      || ontap_die 1 "could not parse the recovery-queue answer"
  fi
  local entry
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    ontap_note "purge $entry from the recovery queue [U26]"
    ontap_ok POST "/api/private/cli/volume/recovery-queue/purge" \
      "{\"vserver\":\"$SVM\",\"volume\":\"$entry\"}"
  done <<EOF
$entries
EOF
  if [ -z "$DRY_RUN" ]; then
    local left
    left="$(ontap_count "$show")" || exit 1
    if [ "$left" != "0" ]; then
      ontap_die 1 "recovery queue still holds an entry for $clone after purge"
    fi
  fi
}

do_delete() {
  local clone="$1" snap clone_uuid
  valid_clone_name "$clone" || exit 2
  snap="$(snapshot_name_for "$clone")"
  ontap_note "delete order: FlexClone $clone -> recovery-queue purge -> snapshot $snap"
  # exit "$?" keeps the guard's exit 2 (not a FlexClone of appdata) distinct from a failed call (1).
  clone_uuid="$(clone_uuid_checked "$clone")" || exit "$?"
  if [ -n "$clone_uuid" ]; then
    ontap_ok PATCH "/api/storage/volumes/$clone_uuid?return_timeout=120" '{"nas":{"path":""}}'
    ontap_ok DELETE "/api/storage/volumes/$clone_uuid?force=true&return_timeout=120"
    wait_count "/api/storage/volumes?name=$clone&svm.uuid=$SVM_UUID&fields=uuid" 0 "FlexClone $clone"
  else
    ontap_note "FlexClone $clone absent; nothing to delete"
  fi
  purge_recovery_queue "$clone"
  local snaps="/api/storage/volumes/$PARENT_UUID/snapshots"
  local snap_uuid
  snap_uuid="$(ontap_first_uuid "$snaps?name=$snap&fields=uuid" "<$snap-uuid>")" || exit 1
  if [ -n "$snap_uuid" ]; then
    ontap_ok DELETE "$snaps/$snap_uuid?return_timeout=120"
    wait_count "$snaps?name=$snap&fields=uuid" 0 "snapshot $snap"
  else
    ontap_note "snapshot $snap absent; nothing to delete"
  fi
  ontap_note "deleted $clone and $snap"
}

# teardown step 4: every leftover appdata_it_<n> clone and it_<n> snapshot, deleted in order.
do_sweep() {
  local vols="/api/storage/volumes?svm.uuid=$SVM_UUID&name=appdata_it_*&fields=name"
  local snaps="/api/storage/volumes/$PARENT_UUID/snapshots?name=it_*&fields=name"
  local names=""
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$vols" >/dev/null
    ontap_call GET "$snaps" >/dev/null
  else
    ontap_ok GET "$vols"
    names="$(ontap_jq '[r.get("name", "") for r in d.get("records", [])]')"
    ontap_ok GET "$snaps"
    names="$names
$(ontap_jq '["appdata_" + r.get("name", "") for r in d.get("records", [])]')"
  fi
  local steps
  # Plain sort -u, not -n: a numeric key would fold appdata_it_3_x into 3 and hide it.
  steps="$(printf '%s\n' "$names" | sed -n 's/^appdata_it_//p' | sort -u)"
  if [ -z "$steps" ]; then
    ontap_note "no leftover appdata_it_* clone or it_* snapshot"
    return 0
  fi
  local n
  for n in $steps; do
    valid_clone_name "appdata_it_$n" || ontap_die 1 "leftover appdata_it_$n / it_$n is outside 1..99; delete it by hand"
    do_delete "appdata_it_$n"
  done
}

# teardown step 5: confirm the three are empty before appdata is deleted. Prints what was scanned.
do_verify_clean() {
  local failed=""
  local all="/api/storage/volumes?svm.uuid=$SVM_UUID&fields=name"
  local clones="/api/storage/volumes?svm.uuid=$SVM_UUID&clone.is_flexclone=true&fields=name"
  local queue="/api/private/cli/volume/recovery-queue?vserver=$SVM&fields=volume"
  local snaps="/api/storage/volumes/$PARENT_UUID/snapshots?name=it_*&fields=name"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$all" >/dev/null
    ontap_call GET "$queue" >/dev/null
    ontap_call GET "$clones" >/dev/null
    ontap_call GET "$snaps" >/dev/null
    ontap_note "verify-clean: recovery queue, FlexClones and it_* snapshots (dry-run assumes empty)"
    return 0
  fi
  ontap_ok GET "$all"
  ontap_note "scanned volumes: $(ontap_jq '", ".join(r.get("name", "") for r in d.get("records", [])) or "(none)"')"
  local status
  status="$(ontap_status GET "$queue")" || exit 1
  if [ "$status" -ge 400 ]; then
    ontap_note "recovery-queue show returned HTTP $status [U26]"; failed=1
  elif [ "$(ontap_jq 'd.get("num_records", len(d.get("records", [])))')" != "0" ]; then
    ontap_note "recovery queue not empty: $(ontap_jq '", ".join(r.get("volume", "") for r in d.get("records", []))')"; failed=1
  else
    ontap_note "recovery queue empty"
  fi
  ontap_ok GET "$clones"
  if [ "$(ontap_jq 'd.get("num_records", len(d.get("records", [])))')" != "0" ]; then
    ontap_note "FlexClones remain: $(ontap_jq '", ".join(r.get("name", "") for r in d.get("records", []))')"; failed=1
  else
    ontap_note "no FlexClone on $SVM"
  fi
  ontap_ok GET "$snaps"
  if [ "$(ontap_jq 'd.get("num_records", len(d.get("records", [])))')" != "0" ]; then
    ontap_note "it_* snapshots remain on $PARENT: $(ontap_jq '", ".join(r.get("name", "") for r in d.get("records", []))')"; failed=1
  else
    ontap_note "no it_* snapshot on $PARENT"
  fi
  if [ -n "$failed" ]; then ontap_die 1 "verify-clean failed; do not delete $PARENT yet"; fi
  ontap_note "verify-clean passed"
}

connect
case "$ACTION" in
  create) do_create "$CLONE" ;;
  delete) do_delete "$CLONE" ;;
  sweep) do_sweep ;;
  verify-clean) do_verify_clean ;;
esac
