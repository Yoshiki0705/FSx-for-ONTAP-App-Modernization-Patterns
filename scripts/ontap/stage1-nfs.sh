#!/usr/bin/env bash
#
# Stage-1 ONTAP configuration: add NFS access to the SAME volume. No volume is cloned, rebuilt or
# moved, and the security style stays ntfs. Runs on the Linux EC2 host via SSM Run Command.
# Idempotent: every object is read with a GET before it is created, and an existing object that
# differs from the contract stops the script (exit 1) rather than being silently rewritten.
#
#   stage1-nfs.sh --client-cidr <primary-subnet-cidr> (--file-system-id fs-... | --mgmt-ip ip)
#                 [--svm appmodsvm] [--volume appdata] [--domain APPMOD] [--region ap-northeast-1]
#
# What it sets on the SVM:
#   - export policy appmod_nfs with one rule: clients = --client-cidr (the primary subnet CIDR,
#     never hardcoded), protocols nfs4 (the mount uses vers=4.1), ro_rule/rw_rule sys,
#     superuser none
#   - that policy assigned to the volume (PATCH by volume UUID)
#   - UNIX users appsvc (uid 10001) and appreader (uid 10002), primary gid equal to the uid
#   - name mappings in both directions: win_unix APPMOD\appsvc -> appsvc, APPMOD\appreader ->
#     appreader, and unix_win appsvc -> APPMOD\appsvc, appreader -> APPMOD\appreader
#
# It never sets a default UNIX user or a default Windows user. It reads and prints the values the
# SVM currently has (CIFS default_unix_user, NFS windows.default_user) so the record shows whether
# an unmapped principal would fall back to one of them.
#
# It asserts the volume security style is ntfs before and after the changes, and exits 1 if it is
# not. It reads the NFS service and exits 1 if NFSv4.1 is reported disabled (it does not enable it).
#
# SVM- and volume-scoped objects are keyed by UUID (resolved at runtime with GET
# /api/svm/svms?name= and GET /api/storage/volumes?name=&svm.name=). fsxadmin is read from Secrets
# Manager (appmod/fsxadmin) by the instance role and never echoed.
#
# When APPMOD_DRY_RUN is set, every request is printed with its method, path and body (credential
# shown as fsxadmin:<redacted>) and NO AWS, ONTAP or network call is made.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ontap/lib-ontap-rest.sh
. "$HERE/lib-ontap-rest.sh"

ONTAP_WHO="stage1-nfs"
REGION="${APPMOD_REGION:-ap-northeast-1}"
DRY_RUN="${APPMOD_DRY_RUN:-}"
FS_ID="${APPMOD_FS_ID:-}"
MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-}"
SVM="${APPMOD_SVM:-appmodsvm}"
VOLUME="${APPMOD_VOLUME:-appdata}"
CLIENT_CIDR="${APPMOD_SUBNET_CIDR:-}"
DOMAIN="${APPMOD_DOMAIN_SHORT:-APPMOD}"
SECRET_ID="${APPMOD_FSXADMIN_SECRET:-appmod/fsxadmin}"
POLICY="appmod_nfs"

usage() {
  echo "usage: stage1-nfs.sh --client-cidr <cidr> (--file-system-id fs-... | --mgmt-ip ip)" >&2
  echo "                     [--svm name] [--volume name] [--domain APPMOD] [--region region]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --client-cidr) CLIENT_CIDR="${2:-}"; shift 2 ;;
    --file-system-id) FS_ID="${2:-}"; shift 2 ;;
    --mgmt-ip) MGMT_IP="${2:-}"; shift 2 ;;
    --svm) SVM="${2:-}"; shift 2 ;;
    --volume) VOLUME="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "stage1-nfs: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# The client CIDR is required and must be a strict IPv4 network (host bits zero). No default: a
# placeholder would silently open the export to the wrong range.
if [ -z "$CLIENT_CIDR" ]; then
  echo "stage1-nfs: --client-cidr (the primary subnet CIDR) is required" >&2; usage; exit 2
fi
if ! python3 -c 'import ipaddress,sys; ipaddress.IPv4Network(sys.argv[1], strict=True)' \
    "$CLIENT_CIDR" 2>/dev/null; then
  echo "stage1-nfs: --client-cidr is not an IPv4 network: $CLIENT_CIDR" >&2; exit 2
fi

# Assert the volume security style is ntfs. Stage 1 adds NFS to an NTFS volume; any other value
# means the single-volume invariant is already broken, so stop before touching anything.
assert_ntfs() {
  local when="$1" style
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "/api/storage/volumes/$VOL_UUID?fields=nas.security_style" >/dev/null
    ontap_note "security style asserted ntfs ($when; dry-run assumes ntfs)"
    return 0
  fi
  ontap_ok GET "/api/storage/volumes/$VOL_UUID?fields=nas.security_style"
  style="$(ontap_jq '(d.get("nas") or {}).get("security_style", "")')"
  if [ "$style" != "ntfs" ]; then
    ontap_die 1 "security style of $VOLUME is '${style:-<none>}', not ntfs ($when); stopping"
  fi
  ontap_note "security style is ntfs ($when)"
}

check_nfs_service() {
  local path="/api/protocols/nfs/services/$SVM_UUID?fields=enabled,protocol,windows"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$path" >/dev/null
    return 0
  fi
  ontap_ok GET "$path"
  local enabled v41
  enabled="$(ontap_jq 'str(d.get("enabled", ""))')"
  v41="$(ontap_jq 'str((d.get("protocol") or {}).get("v41_enabled", ""))')"
  ontap_note "NFS service on $SVM: enabled=$enabled v41_enabled=${v41:-<not returned>}"
  if [ "$enabled" = "False" ] || [ "$v41" = "False" ]; then
    ontap_die 1 "NFS or NFSv4.1 is disabled on $SVM; enabling it is a separate decision, not made here"
  fi
}

ensure_export_policy() {
  local list="/api/protocols/nfs/export-policies?svm.uuid=$SVM_UUID&name=$POLICY&fields=id"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$list" >/dev/null
    ontap_note "create export policy $POLICY"
    ontap_call POST "/api/protocols/nfs/export-policies" \
      "{\"name\":\"$POLICY\",\"svm\":{\"uuid\":\"$SVM_UUID\"}}" >/dev/null
    POLICY_ID="<$POLICY-id>"
    return 0
  fi
  ontap_ok GET "$list"
  POLICY_ID="$(ontap_jq '(d.get("records") or [{}])[0].get("id", "")')"
  if [ -n "$POLICY_ID" ]; then
    ontap_note "export policy $POLICY already present (id $POLICY_ID); left unchanged"
    return 0
  fi
  ontap_note "create export policy $POLICY"
  ontap_ok POST "/api/protocols/nfs/export-policies" \
    "{\"name\":\"$POLICY\",\"svm\":{\"uuid\":\"$SVM_UUID\"}}"
  ontap_ok GET "$list"
  POLICY_ID="$(ontap_jq '(d.get("records") or [{}])[0].get("id", "")')"
  if [ -z "$POLICY_ID" ]; then ontap_die 1 "export policy $POLICY not found after create"; fi
}

ensure_export_rule() {
  local rules="/api/protocols/nfs/export-policies/$POLICY_ID/rules"
  local body
  body="{\"clients\":[{\"match\":\"$CLIENT_CIDR\"}],\"protocols\":[\"nfs4\"],\"ro_rule\":[\"sys\"],\"rw_rule\":[\"sys\"],\"superuser\":[\"none\"]}"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$rules?fields=clients,protocols,ro_rule,rw_rule,superuser" >/dev/null
    ontap_note "create export rule clients=$CLIENT_CIDR nfs4 sec=sys superuser=none"
    ontap_call POST "$rules" "$body" >/dev/null
    return 0
  fi
  ontap_ok GET "$rules?fields=clients,protocols,ro_rule,rw_rule,superuser"
  local verdict
  verdict="$(APPMOD_CIDR="$CLIENT_CIDR" python3 -c 'import json,os,sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
cidr = os.environ["APPMOD_CIDR"]
want = {"protocols": ["nfs4"], "ro_rule": ["sys"], "rw_rule": ["sys"], "superuser": ["none"]}
for r in d.get("records", []):
    if any(c.get("match") == cidr for c in r.get("clients", [])):
        same = all(sorted(r.get(k, [])) == v for k, v in want.items())
        print("same" if same else "differs")
        break
else:
    print("absent")' "$ONTAP_BODY")"
  case "$verdict" in
    same) ontap_note "export rule for $CLIENT_CIDR already present; left unchanged" ;;
    differs) ontap_die 1 "an export rule for $CLIENT_CIDR exists with different settings; not rewriting it" ;;
    *)
      ontap_note "create export rule clients=$CLIENT_CIDR nfs4 sec=sys superuser=none"
      ontap_ok POST "$rules" "$body"
      ;;
  esac
}

assign_policy() {
  local path="/api/storage/volumes/$VOL_UUID"
  local body="{\"nas\":{\"export_policy\":{\"name\":\"$POLICY\"}}}"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$path?fields=nas.export_policy.name" >/dev/null
    ontap_note "assign export policy $POLICY to $VOLUME (by volume UUID)"
    ontap_call PATCH "$path?return_timeout=120" "$body" >/dev/null
    return 0
  fi
  ontap_ok GET "$path?fields=nas.export_policy.name"
  local current
  current="$(ontap_jq '((d.get("nas") or {}).get("export_policy") or {}).get("name", "")')"
  if [ "$current" = "$POLICY" ]; then
    ontap_note "$VOLUME already uses export policy $POLICY; left unchanged"
    return 0
  fi
  ontap_note "assign export policy $POLICY to $VOLUME (was '${current:-<none>}')"
  ontap_ok PATCH "$path?return_timeout=120" "$body"
  ontap_ok GET "$path?fields=nas.export_policy.name"
  current="$(ontap_jq '((d.get("nas") or {}).get("export_policy") or {}).get("name", "")')"
  if [ "$current" != "$POLICY" ]; then ontap_die 1 "export policy on $VOLUME is '$current' after PATCH"; fi
}

ensure_unix_user() {
  local name="$1" uid="$2"
  local path="/api/name-services/unix-users/$SVM_UUID/$name"
  local body="{\"svm\":{\"uuid\":\"$SVM_UUID\"},\"name\":\"$name\",\"id\":$uid,\"primary_gid\":$uid}"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$path?fields=id,primary_gid" >/dev/null
    ontap_note "create UNIX user $name uid=$uid"
    ontap_call POST "/api/name-services/unix-users" "$body" >/dev/null
    return 0
  fi
  local status existing
  status="$(ontap_call GET "$path?fields=id,primary_gid")"
  if [ "$status" = "200" ]; then
    existing="$(ontap_jq 'str(d.get("id", ""))')"
    if [ "$existing" != "$uid" ]; then
      ontap_die 1 "UNIX user $name exists with uid $existing, not $uid; not rewriting it"
    fi
    local existing_gid
    existing_gid="$(ontap_jq 'str(d.get("primary_gid", ""))')"
    if [ "$existing_gid" != "$uid" ]; then
      ontap_die 1 "UNIX user $name exists with primary_gid $existing_gid, not $uid; not rewriting it"
    fi
    ontap_note "UNIX user $name uid=$uid primary_gid=$uid already present; left unchanged"
    return 0
  fi
  if [ "$status" != "404" ]; then
    ontap_die 1 "GET $path returned HTTP $status"
  fi
  ontap_note "create UNIX user $name uid=$uid"
  ontap_ok POST "/api/name-services/unix-users" "$body"
}

# Name mappings. ONTAP treats the pattern as a regular expression and the replacement as a
# substitution string, so the domain separator is written as an escaped backslash in both.
ensure_name_mappings() {
  local list="/api/name-services/name-mappings?svm.uuid=$SVM_UUID&fields=direction,index,pattern,replacement"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$list" >/dev/null
  else
    ontap_ok GET "$list"
  fi
  local plan
  plan="$(APPMOD_DOMAIN="$DOMAIN" APPMOD_SVM_UUID="$SVM_UUID" APPMOD_DRY="${DRY_RUN:+1}" \
    python3 -c 'import json,os,sys
dom = os.environ["APPMOD_DOMAIN"]
svm = os.environ["APPMOD_SVM_UUID"]
records = []
if not os.environ.get("APPMOD_DRY"):
    records = json.load(open(sys.argv[1], encoding="utf-8")).get("records", [])
want = []
for user in ("appsvc", "appreader"):
    win = dom + "\\\\" + user
    want.append(("win_unix", win, user))
    want.append(("unix_win", user, win))
next_index = {}
for r in records:
    d = r.get("direction")
    next_index[d] = max(next_index.get(d, 0), int(r.get("index", 0)))
for direction, pattern, replacement in want:
    found = [r for r in records if r.get("direction") == direction and r.get("pattern") == pattern]
    if found:
        state = "same" if found[0].get("replacement") == replacement else "differs"
        print("\t".join((state, direction, pattern, "-")))
        continue
    next_index[direction] = next_index.get(direction, 0) + 1
    body = {"svm": {"uuid": svm}, "direction": direction, "index": next_index[direction],
            "pattern": pattern, "replacement": replacement}
    print("\t".join(("create", direction, pattern, json.dumps(body, separators=(",", ":")))))' \
    "${ONTAP_BODY:-/dev/null}")"
  local state direction pattern body
  while IFS="$(printf '\t')" read -r state direction pattern body; do
    [ -n "$state" ] || continue
    case "$state" in
      same) ontap_note "name mapping $direction $pattern already present; left unchanged" ;;
      differs) ontap_die 1 "name mapping $direction $pattern exists with a different replacement; not rewriting it" ;;
      create)
        ontap_note "create name mapping $direction $pattern"
        if [ -n "$DRY_RUN" ]; then
          ontap_call POST "/api/name-services/name-mappings" "$body" >/dev/null
        else
          ontap_ok POST "/api/name-services/name-mappings" "$body"
        fi
        ;;
    esac
  done <<EOF
$plan
EOF
}

# Read-only: report the default users the SVM already has. This script never sets either.
report_default_users() {
  ontap_note "no default UNIX user and no default Windows user are set by this script"
  local cifs="/api/protocols/cifs/services/$SVM_UUID?fields=default_unix_user"
  local nfs="/api/protocols/nfs/services/$SVM_UUID?fields=windows"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$cifs" >/dev/null
    ontap_call GET "$nfs" >/dev/null
    return 0
  fi
  local status
  status="$(ontap_call GET "$cifs")"
  if [ "$status" = "200" ]; then
    ontap_note "current CIFS default_unix_user: '$(ontap_jq 'd.get("default_unix_user", "<not returned>")')'"
  else
    ontap_note "CIFS default_unix_user not readable (HTTP $status)"
  fi
  status="$(ontap_call GET "$nfs")"
  if [ "$status" = "200" ]; then
    ontap_note "current NFS windows.default_user: '$(ontap_jq '(d.get("windows") or {}).get("default_user", "<not returned>")')'"
  else
    ontap_note "NFS windows.default_user not readable (HTTP $status)"
  fi
}

ontap_note "add NFS to $VOLUME on SVM $SVM (no clone, no rebuild, no move); clients $CLIENT_CIDR"
ontap_resolve_mgmt_ip
ontap_note "ONTAP management endpoint: $MGMT_IP"
ontap_login fsxadmin "$SECRET_ID"

SVM_UUID="$(ontap_svm_uuid "$SVM")"
VOL_UUID="$(ontap_volume_uuid "$VOLUME" "$SVM")"
POLICY_ID=""
ontap_note "resolved SVM UUID and volume UUID for the UUID-keyed endpoints"

assert_ntfs "before"
check_nfs_service
ensure_export_policy
ensure_export_rule
assign_policy
ensure_unix_user appsvc 10001
ensure_unix_user appreader 10002
ensure_name_mappings
report_default_users
assert_ntfs "after"
ontap_note "done. Mount over NFSv4.1 from the primary subnet, then record b1 with record-boundary.sh."
