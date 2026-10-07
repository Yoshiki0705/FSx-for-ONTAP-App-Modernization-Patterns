#!/usr/bin/env bash
#
# Stage-0 ONTAP configuration over the REST API, run on the Linux EC2 host via SSM Run Command.
# Idempotent: every object is checked with a GET before it is created, so a re-run records the
# existing object instead of erroring on it. fsxadmin is read from Secrets Manager by the instance
# role inside this script, never passed in argv or to an SSM parameter, and never echoed.
#
# What it configures on the SVM:
#   - SMB share `appdata` at path /appdata, with a share-level ACL
#   - NTFS ACLs on /appdata: appsvc read/write; appreader read-only with an explicit deny-write ACE
#   - the directories seed/, probe/, out/ on the volume
#   - the REST role appmod_itclone and user appmod-itclone for integration-clone.sh (paths limited to
#     /api/storage/volumes, /api/storage/volumes/*/snapshots and the recovery-queue CLI path; U26)
#   - the read-only REST role appmod_readonly used for boundary reads during stage 2 (U26)
#   - the SVM volume-delete-retention-hours set to 0 if possible (U27)
#
# Before configuring SMB it asserts the SVM has discovered a domain controller, via
#   GET /api/protocols/cifs/domains/{svm-uuid}?fields=discovered_servers
# and requires at least one ms_dc server in state "ok" (the /api/protocols/active-directory
# collection alone returns 0 records and is not sufficient; confirmed live 2026-10-07). Without a
# discovered DC it exits 4 (R8.3: do not record b0, tear the environment down and recreate).
#
# Inputs (never hardcoded; the live fs-/i-/account values stay out of this file):
#   --file-system-id <fs-id>   or env APPMOD_FS_ID       the FSx for ONTAP file system id
#   --svm <name>               or env APPMOD_SVM         default appmodsvm
#   --volume <name>            or env APPMOD_VOLUME      default appdata
#   --mgmt-ip <ip>             or env APPMOD_ONTAP_MGMT_IP  skip the describe-file-systems lookup
#   --region <region>          or env APPMOD_REGION      default ap-northeast-1
#
# The management IP is resolved at runtime from the FSx for ONTAP API when not supplied:
#   aws fsx describe-file-systems --file-system-id <fs> \
#     --query 'FileSystems[0].OntapConfiguration.Endpoints.Management.IpAddresses' --output text
#
# When APPMOD_DRY_RUN is set, every curl is printed with its method, endpoint and body (the
# credential shown as fsxadmin:<redacted>) and NO AWS, ONTAP or network call is made. This is how a
# reviewer confirms the real commands are built. Verified: ONTAP 9.19.1P2; a non-SnapLock volume
# reports snaplock.type "non_snaplock" (U25 resolved, live 2026-10-07).
#
set -euo pipefail

REGION="${APPMOD_REGION:-ap-northeast-1}"
DRY_RUN="${APPMOD_DRY_RUN:-}"
FS_ID="${APPMOD_FS_ID:-}"
SVM="${APPMOD_SVM:-appmodsvm}"
VOLUME="${APPMOD_VOLUME:-appdata}"
MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-}"
SECRET_ID="${APPMOD_FSXADMIN_SECRET:-appmod/fsxadmin}"

usage() {
  echo "usage: stage0-smb.sh [--file-system-id fs-...] [--svm name] [--volume name]" >&2
  echo "                     [--mgmt-ip ip] [--region region]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --file-system-id) FS_ID="${2:-}"; shift 2 ;;
    --svm) SVM="${2:-}"; shift 2 ;;
    --volume) VOLUME="${2:-}"; shift 2 ;;
    --mgmt-ip) MGMT_IP="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "stage0-smb: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
# --svm is the ONTAP SVM name, not the SVM ID from the FSx for ONTAP API (svm-...). Same check as teardown.sh.
if [[ "$SVM" =~ ^svm-[0-9a-f]+$ ]]; then
  echo "stage0-smb: --svm takes the ONTAP SVM name (for example appmodsvm), not the SVM ID from the FSx for ONTAP API ($SVM)" >&2
  exit 2
fi

note() { echo "stage0-smb: $*"; }

# Resolve the ONTAP management IP from the FSx for ONTAP API unless supplied. Under dry-run the lookup
# is printed and a placeholder is used, so no AWS call is made.
resolve_mgmt_ip() {
  if [ -n "$MGMT_IP" ]; then return 0; fi
  if [ -z "$FS_ID" ]; then
    echo "stage0-smb: need --file-system-id (or APPMOD_FS_ID) to resolve the management IP, or pass --mgmt-ip" >&2
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
    echo "stage0-smb: could not resolve a management IP for $FS_ID" >&2
    exit 2
  fi
}

# Read the fsxadmin password from Secrets Manager via the instance role. The value is kept in a
# variable in this process only; it is never printed and never passed on a command line. Under
# dry-run this is not called at all.
read_fsxadmin_password() {
  aws --region "$REGION" secretsmanager get-secret-value \
    --secret-id "$SECRET_ID" --query SecretString --output text \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])'
}

# curl against the ONTAP REST API. $1 method, $2 path (begins with /), $3 optional JSON body.
# Under dry-run the exact method, endpoint and body are printed with the credential redacted and
# nothing is sent. Otherwise the fsxadmin password (held in $ONTAP_PW) is used for basic auth and
# never appears in argv of any logged line. -k is required because the FSx for ONTAP management
# endpoint presents a certificate whose CN is the management DNS name, and the REST calls reach it
# by the management IP resolved from the FSx for ONTAP API, so IP-based hostname verification cannot match.
ontap() {
  local method="$1" path="$2" body="${3:-}"
  local url="https://$MGMT_IP$path"
  if [ -n "$DRY_RUN" ]; then
    if [ -n "$body" ]; then
      echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> -X $method $url -d '$body'"
    else
      echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> -X $method $url"
    fi
    return 0
  fi
  if [ -n "$body" ]; then
    curl_auth -sS -k -X "$method" "$url" -H 'Content-Type: application/json' -d "$body"
  else
    curl_auth -sS -k -X "$method" "$url"
  fi
}

# curl with the fsxadmin credential on stdin as a config line (-K -). Passing it with -u put the
# password in curl's argv, where any local process can read it from /proc.
curl_auth() {
  local esc="${ONTAP_PW//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  printf 'user = "fsxadmin:%s"\n' "$esc" | curl -K - "$@"
}

# Every write (POST/PATCH), and every GET whose answer is used as a fact (SVM and volume UUIDs, DC
# discovery), goes through here. curl without -f exits 0 on an HTTP error, so `ontap` alone let a
# refused create read as done and a refused GET read as "no records"; the status is read with -w
# and checked. A transport error or an HTTP status >= 400 stops the script (exit 1). The response
# body is printed as before. With a 4th argument the HTTP error is reported and the script
# continues: used only for the U27 retention setting, which the design applies "if possible".
# Under dry-run this is `ontap`.
ontap_checked() {
  local method="$1" path="$2" body="${3:-}" tolerate="${4:-}"
  if [ -n "$DRY_RUN" ]; then
    ontap "$method" "$path" "$body"
    return 0
  fi
  local out status
  out="$(mktemp)"
  local args=(-sS -k -X "$method" -o "$out" -w '%{http_code}')
  if [ -n "$body" ]; then args+=(-H 'Content-Type: application/json' -d "$body"); fi
  if ! status="$(curl_auth "${args[@]}" "https://$MGMT_IP$path")" \
    || ! [[ "$status" =~ ^[1-5][0-9][0-9]$ ]]; then
    rm -f "$out"
    echo "stage0-smb: $method $path failed before an HTTP status (curl transport error, status '${status:-}')" >&2
    exit 1
  fi
  cat "$out"
  rm -f "$out"
  if [ "$status" -ge 400 ]; then
    if [ -n "$tolerate" ]; then
      note "$method $path returned HTTP $status; continuing ($tolerate)"
      return 0
    fi
    echo "stage0-smb: $method $path returned HTTP $status; stopping" >&2
    exit 1
  fi
}

# GET a path and return 0 only when the response has at least one record. Under dry-run it prints
# the GET and reports "absent" so the create branch is exercised and shown.
ontap_exists() {
  local path="$1"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> https://$MGMT_IP$path   # check-then-create"
    return 1
  fi
  local out
  # Called as `if ontap_exists ...`, where errexit is off, so the curl status is checked here: a
  # transport failure stops the script instead of reading as "absent". An HTTP error answer still
  # reads as absent (a missing files/<dir> path answers with an error), and the create that follows
  # goes through ontap_checked, which stops on an HTTP error.
  if ! out="$(ontap GET "$path")"; then
    echo "stage0-smb: GET $path failed (curl transport error)" >&2
    exit 1
  fi
  printf '%s' "$out" | python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if data.get("num_records", len(data.get("records", []))) else 1)'
}

# Resolve the SVM UUID (needed for the cifs/domains discovery assertion and for the share-ACL and
# file-security endpoints, whose path segment is the SVM UUID, not the name). Under dry-run a
# placeholder is returned.
svm_uuid() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> https://$MGMT_IP/api/svm/svms?name=$SVM&fields=uuid" >&2
    printf '<svm-uuid>'
    return 0
  fi
  ontap_checked GET "/api/svm/svms?name=$SVM&fields=uuid" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin).get("records",[]); print(r[0]["uuid"] if r else "")'
}

# Resolve the target volume UUID. The files/{path} endpoint takes the volume UUID in its path, not
# the name (ONTAP REST rejects the name for volume.uuid). Under dry-run a placeholder is returned.
vol_uuid() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> https://$MGMT_IP/api/storage/volumes?name=$VOLUME&fields=uuid" >&2
    printf '<appdata-uuid>'
    return 0
  fi
  ontap_checked GET "/api/storage/volumes?name=$VOLUME&fields=uuid" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin).get("records",[]); print(r[0]["uuid"] if r else "")'
}

# Assert a domain controller has been discovered for the SVM. Uses the cifs/domains
# discovered_servers path (the active-directory collection alone is not sufficient; confirmed live
# 2026-10-07). Requires at least one ms_dc server in state "ok". Exits 4 when none is found; exits 1
# on a transport error or an HTTP 401/403 (a credential or role error, not a missing DC).
assert_dc_discovered() {
  # An unresolved SVM UUID is a lookup failure, not "no DC discovered": it must not reach the
  # exit-4 branch, whose instruction is to tear the environment down.
  local uuid
  uuid="$(svm_uuid)" || exit 1
  if [ -z "$DRY_RUN" ] && [ -z "$uuid" ]; then
    echo "stage0-smb: could not resolve the SVM UUID for $SVM" >&2
    exit 1
  fi
  local path="/api/protocols/cifs/domains/$uuid?fields=discovered_servers"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> https://$MGMT_IP$path   # require an ms_dc in state ok"
    return 0
  fi
  # The status is read so that a credential or role error (HTTP 401/403) stops with exit 1 instead
  # of the exit-4 "tear down and recreate" branch: those statuses are authentication and
  # authorization answers whatever ONTAP returns for an SVM with no domain. Every other HTTP error
  # answer still reads as "no DC discovered" (exit 4, R8.3), because what ONTAP answers for an SVM
  # without a domain is not recorded. A transport failure stops with exit 1.
  local resp status
  resp="$(mktemp)"
  if ! status="$(curl_auth -sS -k -X GET -o "$resp" -w '%{http_code}' \
      "https://$MGMT_IP$path")" || ! [[ "$status" =~ ^[1-5][0-9][0-9]$ ]]; then
    rm -f "$resp"
    echo "stage0-smb: GET $path failed before an HTTP status (curl transport error, status '${status:-}')" >&2
    exit 1
  fi
  if [ "$status" = "401" ] || [ "$status" = "403" ]; then
    rm -f "$resp"
    echo "stage0-smb: GET $path returned HTTP $status: credential or role error, not a missing DC" >&2
    exit 1
  fi
  if ! python3 -c 'import json,sys
d=json.load(sys.stdin)
servers=d.get("discovered_servers") or []
ok=[s for s in servers if s.get("server_type")=="ms_dc" and s.get("state")=="ok"]
sys.exit(0 if ok else 1)' <"$resp"; then
    rm -f "$resp"
    echo "stage0-smb: no discovered domain controller (ms_dc, state ok) for SVM $SVM" >&2
    echo "stage0-smb: do not record b0; tear down and recreate (R8.3)" >&2
    exit 4
  fi
  rm -f "$resp"
  note "domain controller discovered for $SVM (cifs/domains discovered_servers)"
}

create_share() {
  if ontap_exists "/api/protocols/cifs/shares?svm.name=$SVM&name=appdata"; then
    note "SMB share appdata already present on $SVM; left unchanged"
    return 0
  fi
  note "create SMB share appdata at /$VOLUME on $SVM"
  ontap_checked POST "/api/protocols/cifs/shares" \
    "{\"svm\":{\"name\":\"$SVM\"},\"name\":\"appdata\",\"path\":\"/$VOLUME\"}"
}

set_share_acl() {
  # Share-level ACL: appsvc full_control, appreader read. The acls collection path segment is the
  # SVM UUID (the name is rejected for svm.uuid). Each ACE is created only when absent, so a re-run
  # does not 409 on a duplicate.
  note "set share ACL on appdata: APPMOD\\\\appsvc full_control, APPMOD\\\\appreader read"
  local base="/api/protocols/cifs/shares/$SVM_UUID/appdata/acls"
  if ontap_exists "$base?user_or_group=APPMOD\\appsvc"; then
    note "share ACE for APPMOD\\appsvc already present; left unchanged"
  else
    ontap_checked POST "$base" \
      "{\"permission\":\"full_control\",\"type\":\"windows\",\"user_or_group\":\"APPMOD\\\\appsvc\"}"
  fi
  if ontap_exists "$base?user_or_group=APPMOD\\appreader"; then
    note "share ACE for APPMOD\\appreader already present; left unchanged"
  else
    ontap_checked POST "$base" \
      "{\"permission\":\"read\",\"type\":\"windows\",\"user_or_group\":\"APPMOD\\\\appreader\"}"
  fi
}

set_ntfs_acls() {
  # NTFS ACLs on /appdata via the file-security permissions endpoint: appsvc read/write (modify),
  # appreader read, and an explicit deny-write ACE for appreader so a write is denied even if an
  # allow ACE elsewhere would permit it.
  note "set NTFS ACLs on /$VOLUME: appsvc modify (allow), appreader read (allow) + deny-write ACE"
  # The file-security permissions path segment is the SVM UUID (the name is rejected for svm.uuid).
  # Applying the full ACL set replaces the DACL, so a re-run is idempotent.
  local path="/api/protocols/file-security/permissions/$SVM_UUID/%2F$VOLUME"
  # Each ACE sets apply_to = this_folder + sub_folders + files so the inherited ACEs on files under
  # /appdata carry data rights. Without an explicit apply_to the inherited file ACEs end up scoped
  # to this_folder only, which lets a directory be listed but denies reading file content (observed
  # live 2026-10-07 over both SMB clients). propagation_mode "propagate" pushes the change onto the
  # existing children (seed/ was written before this fix).
  # appsvc gets modify, appreader gets read; both via the simple "rights" model (this ONTAP build
  # rejects the granular advanced_rights.read_attributes keys). The deny ACE uses advanced_rights
  # for the write/append bits only.
  local apply_to="\"apply_to\":{\"this_folder\":true,\"sub_folders\":true,\"files\":true}"
  ontap_checked POST "$path?propagation_mode=propagate" \
    "{\"acls\":[\
{\"access\":\"access_allow\",\"user\":\"APPMOD\\\\appsvc\",$apply_to,\"rights\":\"modify\"},\
{\"access\":\"access_allow\",\"user\":\"APPMOD\\\\appreader\",$apply_to,\"rights\":\"read\"},\
{\"access\":\"access_deny\",\"user\":\"APPMOD\\\\appreader\",$apply_to,\"advanced_rights\":{\"write_data\":true,\"append_data\":true}}\
]}"
}

create_directories() {
  # seed/, probe/, out/ on the volume. The files/{path} endpoint takes the volume UUID in its path
  # (the name is rejected for volume.uuid). A GET on the path first keeps the create idempotent; the
  # GET returns the . and .. entries once the directory exists.
  local dir
  for dir in seed probe out; do
    if ontap_exists "/api/storage/volumes/$VOL_UUID/files/$dir"; then
      note "directory $dir/ already present; left unchanged"
      continue
    fi
    note "create directory $dir/ on $VOLUME"
    ontap_checked POST "/api/storage/volumes/$VOL_UUID/files/$dir" \
      "{\"type\":\"directory\",\"unix_permissions\":\"0775\"}"
  done
}

create_itclone_role() {
  # REST role appmod_itclone: only the three paths integration-clone.sh needs.
  if ontap_exists "/api/security/roles?name=appmod_itclone&owner.name=$SVM"; then
    note "role appmod_itclone already present; left unchanged"
  else
    note "create REST role appmod_itclone (volumes, volumes/*/snapshots, recovery-queue CLI) [U26]"
    ontap_checked POST "/api/security/roles" \
      "{\"name\":\"appmod_itclone\",\"owner\":{\"name\":\"$SVM\"},\"privileges\":[\
{\"path\":\"/api/storage/volumes\",\"access\":\"all\"},\
{\"path\":\"/api/storage/volumes/*/snapshots\",\"access\":\"all\"},\
{\"path\":\"/api/private/cli/volume/recovery-queue\",\"access\":\"all\"}]}"
  fi
  # User appmod-itclone bound to that role; password read from its own secret, never echoed.
  if ontap_exists "/api/security/accounts?name=appmod-itclone&owner.name=$SVM"; then
    note "account appmod-itclone already present; left unchanged"
    return 0
  fi
  note "create account appmod-itclone role=appmod_itclone (password from appmod/ontap-itclone)"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: curl -sS -k -u fsxadmin:<redacted> -X POST https://$MGMT_IP/api/security/accounts \\"
    echo "           -d '{\"name\":\"appmod-itclone\",\"owner\":{\"name\":\"$SVM\"},\"role\":\"appmod_itclone\",\"applications\":[{\"application\":\"http\"}],\"password\":\"<redacted>\"}'"
    return 0
  fi
  local itpw
  itpw="$(aws --region "$REGION" secretsmanager get-secret-value \
    --secret-id appmod/ontap-itclone --query SecretString --output text \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
  ontap_checked POST "/api/security/accounts" \
    "{\"name\":\"appmod-itclone\",\"owner\":{\"name\":\"$SVM\"},\"role\":\"appmod_itclone\",\"applications\":[{\"application\":\"http\",\"authentication_methods\":[\"password\"]}],\"password\":\"$itpw\"}"
  unset itpw
}

create_readonly_role() {
  if ontap_exists "/api/security/roles?name=appmod_readonly&owner.name=$SVM"; then
    note "role appmod_readonly already present; left unchanged"
    return 0
  fi
  note "create read-only REST role appmod_readonly (boundary reads in stage 2) [U26]"
  ontap_checked POST "/api/security/roles" \
    "{\"name\":\"appmod_readonly\",\"owner\":{\"name\":\"$SVM\"},\"privileges\":[\
{\"path\":\"/api/cluster\",\"access\":\"readonly\"},\
{\"path\":\"/api/storage/volumes\",\"access\":\"readonly\"},\
{\"path\":\"/api/protocols\",\"access\":\"readonly\"}]}"
}

set_delete_retention() {
  # U27: set the SVM volume-delete-retention-hours to 0 if the advanced CLI path allows it, so a
  # FlexClone delete does not linger in the recovery queue. This does NOT enable any lock.
  note "set volume-delete-retention-hours=0 on $SVM via the advanced CLI path [U27]"
  ontap_checked PATCH "/api/private/cli/vserver?vserver=$SVM" \
    "{\"volume_delete_retention_hours\":0}" "U27: retention not set; record this outcome"
}

note "configure SMB on SVM $SVM, volume $VOLUME (idempotent, check-then-create)"
resolve_mgmt_ip
note "ONTAP management endpoint: $MGMT_IP"

if [ -z "$DRY_RUN" ]; then
  ONTAP_PW="$(read_fsxadmin_password)" || { echo "stage0-smb: could not read $SECRET_ID" >&2; exit 1; }
  trap 'unset ONTAP_PW 2>/dev/null || true' EXIT
fi

assert_dc_discovered

# Resolve the SVM and volume UUIDs once; the share-ACL, file-security and files/{path} endpoints
# take the UUID in their path, not the name.
SVM_UUID="$(svm_uuid)" || exit 1
VOL_UUID="$(vol_uuid)" || exit 1
if [ -z "$DRY_RUN" ]; then
  if [ -z "$SVM_UUID" ]; then echo "stage0-smb: could not resolve the SVM UUID for $SVM" >&2; exit 1; fi
  if [ -z "$VOL_UUID" ]; then echo "stage0-smb: could not resolve the volume UUID for $VOLUME" >&2; exit 1; fi
fi
note "resolved SVM UUID and volume UUID for the UUID-keyed endpoints"

create_share
set_share_acl
set_ntfs_acls
create_directories
create_itclone_role
create_readonly_role
set_delete_retention
note "done. Record U26/U27 outcomes and the boundary with record-boundary.sh."
