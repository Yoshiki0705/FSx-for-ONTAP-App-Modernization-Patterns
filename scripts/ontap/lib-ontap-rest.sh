#!/usr/bin/env bash
#
# Shared ONTAP REST helpers for stage1-nfs.sh and integration-clone.sh. Sourced, never executed.
# The caller sets these globals before calling the helpers:
#
#   DRY_RUN   non-empty: print every call (credential redacted) and make NO AWS, ONTAP or network call
#   REGION    AWS Region for the FSx for ONTAP and Secrets Manager lookups
#   FS_ID     file system id, used to resolve the management IP when MGMT_IP is empty
#   MGMT_IP   management IP; resolved at runtime by ontap_resolve_mgmt_ip when empty
#   ONTAP_WHO the calling script's name, used as the message prefix
#
# Live facts these helpers encode (ONTAP 9.19.1P2, verified 2026-10-07):
#   - SVM- and volume-scoped endpoints take the UUID, not the name, so resolve UUIDs at runtime.
#   - The management endpoint is reached by IP while its certificate CN is the DNS name, so curl
#     needs -k (hostname verification cannot match an IP).
#   - The management IP comes from describe-file-systems OntapConfiguration.Endpoints.Management.
#
# The password is read from Secrets Manager by the instance role, kept in this process only, and
# handed to curl on stdin as a config line (-K -), so it never appears in argv, in a log line or in
# the dry-run output.

ONTAP_USER=""
ONTAP_PW=""
ONTAP_BODY=""

ontap_note() { echo "${ONTAP_WHO:-ontap}: $*"; }

# Dry-run lines go to stderr so a helper used inside $(...) does not mix them into its result.
ontap_dry() { echo "DRY-RUN: $*" >&2; }

ontap_die() {
  local code="$1"; shift
  echo "${ONTAP_WHO:-ontap}: $*" >&2
  exit "$code"
}

# Resolve the ONTAP management IP from the FSx for ONTAP API unless MGMT_IP is already set.
ontap_resolve_mgmt_ip() {
  if [ -n "$MGMT_IP" ]; then return 0; fi
  if [ -z "$FS_ID" ]; then
    ontap_die 2 "need --file-system-id (or APPMOD_FS_ID) to resolve the management IP, or pass --mgmt-ip"
  fi
  if [ -n "$DRY_RUN" ]; then
    ontap_dry "aws --region $REGION fsx describe-file-systems --file-system-id $FS_ID" \
      "--query 'FileSystems[0].OntapConfiguration.Endpoints.Management.IpAddresses' --output text"
    MGMT_IP="<management-ip>"
    return 0
  fi
  local ips
  ips="$(aws --region "$REGION" fsx describe-file-systems --file-system-id "$FS_ID" \
    --query 'FileSystems[0].OntapConfiguration.Endpoints.Management.IpAddresses' --output text)" \
    || ontap_die 1 "describe-file-systems failed for $FS_ID"
  MGMT_IP="${ips%%[[:space:]]*}"
  if [ -z "$MGMT_IP" ] || [ "$MGMT_IP" = "None" ]; then
    ontap_die 2 "could not resolve a management IP for $FS_ID"
  fi
}

ontap_cleanup() {
  ONTAP_PW=""
  if [ -n "$ONTAP_BODY" ]; then rm -f "$ONTAP_BODY"; fi
}

# ontap_login <ontap-user> <secret-id>: read the password (JSON key "password") via the instance
# role. Under dry-run nothing is read.
ontap_login() {
  ONTAP_USER="$1"
  local secret_id="$2"
  if [ -n "$DRY_RUN" ]; then
    ontap_dry "aws --region $REGION secretsmanager get-secret-value --secret-id $secret_id" \
      "(password kept in-process, shown as $ONTAP_USER:<redacted>)"
    return 0
  fi
  ONTAP_BODY="$(mktemp)"
  trap ontap_cleanup EXIT
  local secret
  secret="$(aws --region "$REGION" secretsmanager get-secret-value \
    --secret-id "$secret_id" --query SecretString --output text)" \
    || ontap_die 1 "could not read secret $secret_id"
  ONTAP_PW="$(printf '%s' "$secret" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')" \
    || { secret=""; ontap_die 1 "secret $secret_id has no readable password"; }
  secret=""
  if [ -z "$ONTAP_PW" ]; then ontap_die 1 "secret $secret_id has no password"; fi
}

# ontap_call <METHOD> <path> [json-body]: print the HTTP status on stdout and leave the response
# body in $ONTAP_BODY. Under dry-run the exact request is printed and status 200 is reported.
ontap_call() {
  local method="$1" path="$2" body="${3:-}"
  local url="https://$MGMT_IP$path"
  if [ -n "$DRY_RUN" ]; then
    if [ -n "$body" ]; then
      ontap_dry "curl -sS -k -u $ONTAP_USER:<redacted> -X $method $url -d '$body'"
    else
      ontap_dry "curl -sS -k -u $ONTAP_USER:<redacted> -X $method $url"
    fi
    printf '200'
    return 0
  fi
  local esc="${ONTAP_PW//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  local args=(-sS -k -K - -X "$method" -H 'Accept: application/json' -o "$ONTAP_BODY" -w '%{http_code}')
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' --data-binary "$body")
  fi
  # curl writes -o only when data arrives, so without this a failed call would leave the previous
  # response in place for ontap_jq to parse. Truncated first, a failed call can never be read as
  # the previous answer.
  : > "$ONTAP_BODY"
  # pipefail (set by every caller) makes this pipeline fail when curl fails. The callers check that
  # status explicitly (ontap_ok, ontap_status), because they run inside $(...), where errexit is off.
  printf 'user = "%s:%s"\n' "$ONTAP_USER" "$esc" | curl "${args[@]}" "$url"
}

# ontap_ok <METHOD> <path> [json-body]: like ontap_call, but exit 1 on a transport error (curl
# failed, or no HTTP status: curl prints 000) and on an HTTP status >= 400.
ontap_ok() {
  local status
  if ! status="$(ontap_call "$@")" || ! [[ "$status" =~ ^[1-5][0-9][0-9]$ ]]; then
    echo "${ONTAP_WHO:-ontap}: $1 $2 failed before an HTTP status (curl transport error, status '${status:-}')" >&2
    exit 1
  fi
  if [ "$status" -ge 400 ]; then
    echo "${ONTAP_WHO:-ontap}: $1 $2 returned HTTP $status:" >&2
    cat "$ONTAP_BODY" >&2 || true
    echo >&2
    exit 1
  fi
}

# ontap_status <METHOD> <path> [json-body]: print the HTTP status for a caller that branches on it
# (404 = absent, an unconfirmed endpoint's error), but exit 1 on a transport error, which has no
# status to branch on. Same transport check as ontap_ok. Call it as
# `status="$(ontap_status ...)" || exit 1`.
ontap_status() {
  local status
  if ! status="$(ontap_call "$@")" || ! [[ "$status" =~ ^[1-5][0-9][0-9]$ ]]; then
    echo "${ONTAP_WHO:-ontap}: $1 $2 failed before an HTTP status (curl transport error, status '${status:-}')" >&2
    exit 1
  fi
  printf '%s' "$status"
}

# Evaluate a Python expression against the last response body (bound to d). Prints the result.
ontap_jq() {
  APPMOD_EXPR="$1" python3 -c 'import json,os,sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
v = eval(os.environ["APPMOD_EXPR"], {"__builtins__": {"len": len, "str": str, "sorted": sorted}}, {"d": d})
print(v if not isinstance(v, (list, tuple)) else "\n".join(str(x) for x in v))' "$ONTAP_BODY"
}

# ontap_first_uuid <collection-path> <placeholder>: uuid of the first record, or "" when there is
# none. Under dry-run prints the GET and returns the placeholder (the object reads as present).
ontap_first_uuid() {
  local path="$1" placeholder="$2"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$path" >/dev/null
    printf '%s' "$placeholder"
    return 0
  fi
  ontap_ok GET "$path"
  ontap_jq '(d.get("records") or [{}])[0].get("uuid", "")'
}

# ontap_count <collection-path>: number of records. Under dry-run prints the GET and reports 0.
ontap_count() {
  local path="$1"
  if [ -n "$DRY_RUN" ]; then
    ontap_call GET "$path" >/dev/null
    printf '0'
    return 0
  fi
  ontap_ok GET "$path"
  ontap_jq 'd.get("num_records", len(d.get("records", [])))'
}

ontap_svm_uuid() {
  local svm="$1" uuid
  # This function itself runs inside $(...), so the inner status is checked explicitly.
  uuid="$(ontap_first_uuid "/api/svm/svms?name=$svm&fields=uuid" "<svm-uuid>")" || exit 1
  if [ -z "$uuid" ]; then ontap_die 1 "could not resolve the SVM UUID for $svm"; fi
  printf '%s' "$uuid"
}

ontap_volume_uuid() {
  local volume="$1" svm="$2" uuid
  uuid="$(ontap_first_uuid "/api/storage/volumes?name=$volume&svm.name=$svm&fields=uuid" "<$volume-uuid>")" \
    || exit 1
  if [ -z "$uuid" ]; then ontap_die 1 "could not resolve the volume UUID for $volume on $svm"; fi
  printf '%s' "$uuid"
}
