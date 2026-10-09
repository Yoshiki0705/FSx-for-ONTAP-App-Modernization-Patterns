#!/usr/bin/env bash
#
# Failure-path tests: a failing command must never read as success. Each case puts a mocked aws,
# curl, git or gitleaks first on PATH, makes one call fail the way it fails in the field (an
# expired SSO token, a curl transport error that prints 000, an HTTP error answer, a paginated
# answer, an unreadable directory), and asserts the script stops instead of reporting success.
#
# The class under test: bash clears errexit inside $(...) (macOS /bin/bash 3.2 has no
# inherit_errexit), never checks a substitution inside [ ] or a process substitution, and curl
# without -f exits 0 on an HTTP error. NO AWS, ONTAP or network call is made; this proves shell
# control flow, not service behavior. Exits 0 only when every expectation holds. Called from
# `make test`.
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1
FIXTURES="scripts/tests/fixtures"
FAILURES=0

pass() { echo "ok: $1"; }
fail() {
  local label="$1"; shift
  echo "FAIL: $label" >&2
  local extra
  for extra in "$@"; do [ -n "$extra" ] && printf '%s\n' "$extra" >&2; done
  FAILURES=$((FAILURES + 1))
}
has() { printf '%s' "$2" | grep -qF -- "$1"; }

TMP="$(mktemp -d)"
cleanup() {
  # Restore the permission the inventory case removes, so the temp tree can be deleted.
  chmod -R u+rwx "$TMP" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
unset APPMOD_DRY_RUN

# --------------------------------------------------------------------------- mocked aws (teardown)
# Logs every call. Fails with the expired-token message when MOCK_FAIL_MATCH is a substring of the
# arguments, or for every call once the MOCK_EXPIRED marker exists. MOCK_EXPIRE_AFTER_SECRETS makes
# the first secrets listing create that marker: the session ages out after step 9, before step 10.
# MOCK_FULL answers the full-path calls (steps 0 and 1) so a run reaches step 2.
TD_MOCK="$TMP/td-mock"; mkdir -p "$TD_MOCK"
cat >"$TD_MOCK/aws" <<'MOCK'
#!/bin/bash
echo "aws $*" >>"$MOCK_AWS_LOG"
expired() { echo "Error when retrieving token from sso: Token has expired and refresh failed" >&2; exit 255; }
if [ -n "${MOCK_EXPIRED:-}" ] && [ -f "$MOCK_EXPIRED" ]; then expired; fi
if [ -n "${MOCK_FAIL_MATCH:-}" ]; then
  case "$*" in *"$MOCK_FAIL_MATCH"*) expired ;; esac
fi
if [ -n "${MOCK_NOT_FOUND_MATCH:-}" ]; then
  case "$*" in *"$MOCK_NOT_FOUND_MATCH"*)
    echo "An error occurred (ResourceNotFoundException) when calling the GetSchedule operation" >&2; exit 254 ;;
  esac
fi
case "$*" in
  *"secretsmanager list-secrets"*)
    if [ -n "${MOCK_EXPIRE_AFTER_SECRETS:-}" ]; then : >"$MOCK_EXPIRED"; fi
    echo "" ;;
  *"cloudformation delete-stack"*)
    # Cut a full-path run short at its first stack delete; the log records whether it was reached.
    if [ -n "${MOCK_FULL:-}" ]; then exit 99; fi ;;
  *"describe-instance-information"*) echo Online ;;
  *"ssm send-command"*) echo cmd-1 ;;
  *"get-command-invocation"*"--query Status "*) echo Success ;;
  *"get-command-invocation"*"ResponseCode"*) echo 0 ;;
  *"cloudformation list-stacks"*) if [ -n "${MOCK_FULL:-}" ]; then echo CREATE_COMPLETE; else echo ""; fi ;;
  *) echo "" ;;
esac
MOCK
chmod +x "$TD_MOCK/aws"
TD_ENV=(env -u APPMOD_DRY_RUN PATH="$TD_MOCK:$PATH" APPMOD_ABSENCE_TRIES=1 APPMOD_ABSENCE_SLEEP=0
  APPMOD_SSM_ONLINE_TRIES=1 APPMOD_SSM_ONLINE_SLEEP=0)
export MOCK_AWS_LOG

# R1 (a): every step-10 enumeration fails (the SSO token expired after the secrets were listed).
# Step 10 must not print "nothing left", and teardown must exit non-zero after the overridden
# single try (R4: APPMOD_ABSENCE_TRIES=1 is honored).
MOCK_AWS_LOG="$TMP/td-r1a.log"; : >"$MOCK_AWS_LOG"
OUT="$("${TD_ENV[@]}" MOCK_EXPIRED="$TMP/td-expired" MOCK_EXPIRE_AFTER_SECRETS=1 \
  bash scripts/teardown.sh --after-failed-create --apply --file-system-id fs-test 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && ! has "nothing left" "$OUT" && has "QUERY-FAILED" "$OUT" \
  && has "resources remain after 1 checks" "$OUT"; then
  pass "teardown step 10 with an expired token: exit $RC, QUERY-FAILED, never 'nothing left'"
else
  fail "teardown step 10 with an expired token (rc=$RC) must exit non-zero without 'nothing left'" "$OUT"
fi

# R1 (b): the appdata lookup fails. It must not read as "appdata already gone", and the base stack
# delete must not be reached.
MOCK_AWS_LOG="$TMP/td-r1b.log"; : >"$MOCK_AWS_LOG"
OUT="$("${TD_ENV[@]}" MOCK_FAIL_MATCH="Volumes[?Name=='appdata']" \
  bash scripts/teardown.sh --after-failed-create --apply --file-system-id fs-test 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && ! has "already gone" "$OUT" && has "describe-volumes failed" "$OUT" \
  && ! grep -q "delete-stack --stack-name appmod-base" "$MOCK_AWS_LOG"; then
  pass "teardown stops (exit 1) when the appdata lookup fails, before the base stack delete"
else
  fail "teardown with a failed appdata lookup (rc=$RC) must exit 1 without 'already gone' or a stack delete" \
    "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi

# Step 2 (sweep): a failed get-schedule must not read as "schedule not found"; appmod-stage3 must
# not be deleted while its schedule may still be enabled.
TD_FULL=(--apply --file-system-id fs-test --linux-instance i-lnx --bucket appmod-artifacts-example
  --windows-role appmod-test-WindowsRole)
MOCK_AWS_LOG="$TMP/td-s2.log"; : >"$MOCK_AWS_LOG"
OUT="$("${TD_ENV[@]}" MOCK_FULL=1 MOCK_FAIL_MATCH="scheduler get-schedule" \
  bash scripts/teardown.sh "${TD_FULL[@]}" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && has "get-schedule appmod-stage3-worker-poll failed" "$OUT" \
  && ! grep -q "delete-stack --stack-name appmod-stage3" "$MOCK_AWS_LOG"; then
  pass "teardown stops (exit 1) on a failed get-schedule, before deleting appmod-stage3"
else
  fail "teardown with a failed get-schedule (rc=$RC) must exit 1 before the stage3 delete" \
    "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi
# Control: ResourceNotFoundException still means "no schedule" and the stage3 delete proceeds.
MOCK_AWS_LOG="$TMP/td-s2c.log"; : >"$MOCK_AWS_LOG"
OUT="$("${TD_ENV[@]}" MOCK_FULL=1 MOCK_NOT_FOUND_MATCH="scheduler get-schedule" \
  bash scripts/teardown.sh "${TD_FULL[@]}" 2>&1)"; RC=$?
if has "schedule appmod-stage3-worker-poll not found" "$OUT" \
  && grep -q "delete-stack --stack-name appmod-stage3" "$MOCK_AWS_LOG"; then
  pass "teardown treats get-schedule ResourceNotFoundException as no schedule and deletes appmod-stage3"
else
  fail "teardown control: ResourceNotFoundException should continue to the stage3 delete (rc=$RC)" \
    "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi
# R4: a non-integer budget is refused before any call.
MOCK_AWS_LOG="$TMP/td-r4.log"; : >"$MOCK_AWS_LOG"
"${TD_ENV[@]}" APPMOD_ABSENCE_TRIES=ten bash scripts/teardown.sh --after-failed-create --apply \
  --file-system-id fs-test >/dev/null 2>&1; RC=$?
if [ "$RC" -eq 2 ] && [ ! -s "$MOCK_AWS_LOG" ]; then
  pass "teardown refuses a non-integer APPMOD_ABSENCE_TRIES (exit 2, no call)"
else
  fail "teardown with APPMOD_ABSENCE_TRIES=ten (rc=$RC) should exit 2 before any call" "$(cat "$MOCK_AWS_LOG")"
fi

# --------------------------------------------------------------------------- mocked curl (ONTAP)
# Answers from a per-case route list (method + URL substring, first unused match wins, "once"
# routes are consumed) and behaves like curl: the body goes to -o when given, otherwise to stdout;
# -w prints the status. A "transport_fail" route writes no body, prints 000 under -w and exits 7,
# as curl does when it cannot connect. A "status" route returns that HTTP status with exit 0, as
# curl without -f does.
ONTAP_MOCK="$TMP/ontap-mock"; mkdir -p "$ONTAP_MOCK"
cat >"$ONTAP_MOCK/curl" <<'MOCK'
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if "-K" in args:
    sys.stdin.read()
# The URL is not always last (a caller may put -H/-d after it), so take the first https:// argument.
method, out, wfmt = "GET", None, None
url = next((a for a in args if a.startswith("https://")), args[-1])
i = 0
while i < len(args):
    a = args[i]
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "-o": out = args[i + 1]; i += 2; continue
    if a == "-w": wfmt = args[i + 1]; i += 2; continue
    if a in ("-u", "-H", "-K", "-d", "--data-binary"): i += 2; continue
    i += 1
path = os.environ["MOCK_CURL_ROUTES"]
routes = json.load(open(path))
route, tag = None, "UNMATCHED"
for r in routes:
    if r.get("used") or r["method"] != method or r["path"] not in url:
        continue
    route, tag = r, "matched"
    if r.get("once"):
        r["used"] = True
        json.dump(routes, open(path, "w"))
    break
with open(os.environ["MOCK_CURL_LOG"], "a") as log:
    kind = "transport-fail" if route and route.get("transport_fail") else tag
    log.write(f"{method} {url} {kind}\n")
if route and route.get("transport_fail"):
    sys.stderr.write("curl: (7) Failed to connect to the mock management endpoint\n")
    if wfmt:
        sys.stdout.write("000")
    sys.exit(7)
if route:
    status, body = route.get("status", 200), route.get("body", {})
else:
    status, body = 404, {"error": {"message": "no mock route"}}
data = json.dumps(body)
if out:
    open(out, "w").write(data)
else:
    sys.stdout.write(data)
if wfmt:
    sys.stdout.write(str(status))
MOCK
cat >"$ONTAP_MOCK/aws" <<'MOCK'
#!/bin/bash
case "$*" in
  *"secretsmanager get-secret-value"*) printf '%s\n' '{"username":"mock","password":"mock-pw"}' ;;
  *) echo "ontap mock: unexpected aws call: $*" >&2; exit 1 ;;
esac
MOCK
chmod +x "$ONTAP_MOCK/curl" "$ONTAP_MOCK/aws"
ONTAP_ENV=(env -u APPMOD_DRY_RUN PATH="$ONTAP_MOCK:$PATH" APPMOD_POLL_SLEEP=0 APPMOD_POLL_TRIES=3)
export MOCK_CURL_ROUTES MOCK_CURL_LOG
ROUTES_BASE='{"method":"GET","path":"/api/svm/svms?name=appmodsvm","body":{"records":[{"uuid":"svm-u"}],"num_records":1}},
{"method":"GET","path":"/api/storage/volumes?name=appdata&svm.name=appmodsvm","body":{"records":[{"uuid":"appdata-u"}],"num_records":1}}'

# R2 (a): curl cannot connect on the clone lookup (000, exit 7). integration-clone delete must stop
# as a transport error, before any PATCH or DELETE, rather than parse the previous response.
MOCK_CURL_ROUTES="$TMP/r2a.json"; MOCK_CURL_LOG="$TMP/r2a.log"; : >"$MOCK_CURL_LOG"
cat >"$MOCK_CURL_ROUTES" <<EOF
[$ROUTES_BASE,
{"method":"GET","path":"/api/storage/volumes?name=appdata_it_3&svm.uuid=svm-u","transport_fail":true}]
EOF
OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/integration-clone.sh delete --step 3 --mgmt-ip 203.0.113.5 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && has "curl transport error" "$OUT" \
  && ! grep -qE "^(PATCH|DELETE) " "$MOCK_CURL_LOG"; then
  pass "integration-clone delete stops (exit 1) on a transport failure at the clone lookup"
else
  fail "integration-clone delete with a transport failure at the clone lookup (rc=$RC) must exit 1 as a transport error" \
    "$OUT" "$(cat "$MOCK_CURL_LOG")"
fi

# R2 (b): the clone is absent, then curl cannot connect on the snapshot lookup. The run must not end
# "deleted appdata_it_3 and it_3" with exit 0 while the snapshot may remain.
MOCK_CURL_ROUTES="$TMP/r2b.json"; MOCK_CURL_LOG="$TMP/r2b.log"; : >"$MOCK_CURL_LOG"
cat >"$MOCK_CURL_ROUTES" <<EOF
[$ROUTES_BASE,
{"method":"GET","path":"/api/storage/volumes?name=appdata_it_3&svm.uuid=svm-u","body":{"records":[],"num_records":0}},
{"method":"GET","path":"/api/private/cli/volume/recovery-queue","body":{"records":[],"num_records":0}},
{"method":"GET","path":"/api/storage/volumes/appdata-u/snapshots?name=it_3","transport_fail":true}]
EOF
OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/integration-clone.sh delete --step 3 --mgmt-ip 203.0.113.5 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && ! has "deleted appdata_it_3 and it_3" "$OUT" && has "curl transport error" "$OUT"; then
  pass "integration-clone delete does not report 'deleted' after a transport failure at the snapshot lookup"
else
  fail "integration-clone delete with a transport failure at the snapshot lookup (rc=$RC) must not succeed" \
    "$OUT" "$(cat "$MOCK_CURL_LOG")"
fi

# R2 (c): the [ "$(ontap_count ...)" != "0" ] site in create. A transport failure on the snapshot
# lookup must not read as "snapshot already present".
MOCK_CURL_ROUTES="$TMP/r2c.json"; MOCK_CURL_LOG="$TMP/r2c.log"; : >"$MOCK_CURL_LOG"
cat >"$MOCK_CURL_ROUTES" <<EOF
[$ROUTES_BASE,
{"method":"GET","path":"/api/storage/volumes/appdata-u/snapshots?name=it_3","transport_fail":true},
{"method":"GET","path":"/api/storage/volumes?name=appdata_it_3&svm.uuid=svm-u","body":{"records":[],"num_records":0}}]
EOF
OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/integration-clone.sh create --step 3 --mgmt-ip 203.0.113.5 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && ! has "already present" "$OUT" && has "curl transport error" "$OUT"; then
  pass "integration-clone create stops (exit 1) when the snapshot lookup fails, not 'already present'"
else
  fail "integration-clone create with a failed snapshot lookup (rc=$RC) must exit 1 without 'already present'" \
    "$OUT" "$(cat "$MOCK_CURL_LOG")"
fi

# stage0-smb (sweep): an HTTP 400 on the share create must stop the script, not read as created.
S0_PRE='{"method":"GET","path":"/api/svm/svms?name=appmodsvm","body":{"records":[{"uuid":"svm-u"}],"num_records":1}},
{"method":"GET","path":"/api/protocols/cifs/domains/svm-u","body":{"discovered_servers":[{"server_type":"ms_dc","state":"ok"}]}},
{"method":"GET","path":"/api/storage/volumes?name=appdata&fields=uuid","body":{"records":[{"uuid":"appdata-u"}],"num_records":1}}'
MOCK_CURL_ROUTES="$TMP/s0.json"; MOCK_CURL_LOG="$TMP/s0.log"; : >"$MOCK_CURL_LOG"
cat >"$MOCK_CURL_ROUTES" <<EOF
[$S0_PRE,
{"method":"GET","path":"/api/protocols/cifs/shares?svm.name=appmodsvm&name=appdata","body":{"records":[],"num_records":0}},
{"method":"POST","path":"/api/protocols/cifs/shares","status":400,"body":{"error":{"message":"mock refusal"}}}]
EOF
OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/stage0-smb.sh --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && has "returned HTTP 400" "$OUT" && ! has "stage0-smb: done." "$OUT"; then
  pass "stage0-smb stops (exit 1) on an HTTP 400 share create"
else
  fail "stage0-smb with an HTTP 400 share create (rc=$RC) must exit 1 without 'done.'" "$OUT" "$(cat "$MOCK_CURL_LOG")"
fi
# Regression guard: the U27 retention PATCH stays non-fatal ("if possible"); an HTTP error there
# is reported and the run completes, as before.
MOCK_CURL_ROUTES="$TMP/s0-u27.json"; MOCK_CURL_LOG="$TMP/s0-u27.log"; : >"$MOCK_CURL_LOG"
PRESENT='{"records":[{"name":"x"}],"num_records":1}'
cat >"$MOCK_CURL_ROUTES" <<EOF
[$S0_PRE,
{"method":"GET","path":"/api/protocols/cifs/shares?svm.name=appmodsvm&name=appdata","body":$PRESENT},
{"method":"GET","path":"/api/protocols/cifs/shares/svm-u/appdata/acls","body":$PRESENT},
{"method":"POST","path":"/api/protocols/file-security/permissions/svm-u/","status":202,"body":{}},
{"method":"GET","path":"/api/storage/volumes/appdata-u/files/","body":$PRESENT},
{"method":"GET","path":"/api/security/roles?name=","body":$PRESENT},
{"method":"GET","path":"/api/security/accounts?name=appmod-itclone","body":$PRESENT},
{"method":"PATCH","path":"/api/private/cli/vserver","status":400,"body":{"error":{"message":"mock refusal"}}}]
EOF
OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/stage0-smb.sh --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && has "continuing (U27" "$OUT" && has "stage0-smb: done." "$OUT" \
  && ! grep -q "UNMATCHED" "$MOCK_CURL_LOG"; then
  pass "stage0-smb keeps the U27 retention PATCH non-fatal (HTTP 400 reported, run completes)"
else
  fail "stage0-smb with an HTTP 400 on the U27 PATCH (rc=$RC) should report it and complete" \
    "$OUT" "$(cat "$MOCK_CURL_LOG")"
fi

# record-boundary (sweep): an HTTP 401 on the name-mapping GET must not be recorded as "no name
# mappings"; no record is written.
MOCK_CURL_ROUTES="$TMP/rb.json"; MOCK_CURL_LOG="$TMP/rb.log"; : >"$MOCK_CURL_LOG"
cat >"$MOCK_CURL_ROUTES" <<EOF
[{"method":"GET","path":"/api/cluster?fields=version","body":{"version":{"full":"mock ONTAP"}}},
{"method":"GET","path":"/api/storage/volumes?fields=","body":{"records":[{"name":"appdata","uuid":"appdata-u","nas":{"security_style":"ntfs"},"snapshot_locking_enabled":false,"snaplock":{"type":"non_snaplock"}}]}},
{"method":"GET","path":"/api/storage/volumes?name=appdata&fields=uuid","body":{"records":[{"uuid":"appdata-u"}]}},
{"method":"GET","path":"/api/protocols/nfs/export-policies","body":{"records":[]}},
{"method":"GET","path":"/api/name-services/name-mappings","status":401,"body":{"error":{"message":"mock unauthorized"}}},
{"method":"GET","path":"/api/storage/volumes/appdata-u/snapshots","body":{"records":[]}}]
EOF
rm -rf .private/runs/s0-rbfailtest
OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/record-boundary.sh --boundary b0 --run-id s0-rbfailtest \
  --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && has "returned HTTP 401" "$OUT" && [ ! -f .private/runs/s0-rbfailtest/b0.json ]; then
  pass "record-boundary stops (exit 1) on an HTTP 401 GET and writes no record"
else
  fail "record-boundary with an HTTP 401 name-mapping GET (rc=$RC) must exit 1 and write no record" \
    "$OUT" "$(cat "$MOCK_CURL_LOG")"
fi
rm -rf .private/runs/s0-rbfailtest

# --------------------------------------------------------------------------- check-no-locking (R3)
OUT="$(env APPMOD_ONTAP_FIXTURE="$FIXTURES/ontap_paginated.json" bash scripts/ontap/check-no-locking.sh 2>&1)"; RC=$?
if [ "$RC" -eq 3 ] && has "paginated" "$OUT"; then
  pass "check-no-locking refuses a paginated response (exit 3)"
else
  fail "check-no-locking on a response with _links.next (rc=$RC) must exit 3" "$OUT"
fi

# --------------------------------------------------------------------------- deploy.sh (sweep)
# The parameter reader prints one valid token and then refuses a malformed entry (exit 2). The
# create must not run with the partial list.
EST_DIR="$TMP/estimates"; mkdir -p "$EST_DIR/used"
CREATED="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)-d.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
APPROVED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
EST="$EST_DIR/partial-params-base.json"
cat >"$EST" <<EOF
{"target": "base", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 72,
 "parameters": [{"ParameterKey": "CreateVpc", "ParameterValue": "true"}, {"ParameterKey": "CreateSubnets"}]}
EOF
cat >"$TMP/approval.json" <<EOF
[{"target": "base", "approved_at": "$APPROVED", "hours": 72, "estimate_file": "$EST"}]
EOF
OUT="$(env APPMOD_DRY_RUN=1 APPMOD_ESTIMATES_DIR="$EST_DIR" APPMOD_APPROVAL_FILE="$TMP/approval.json" \
  bash scripts/deploy.sh base --estimate "$EST" --approved-at "$APPROVED" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && ! has "create-stack" "$OUT" && [ -f "$EST" ]; then
  pass "deploy.sh refuses an estimate whose parameter list is partly malformed (exit 2, no create)"
else
  fail "deploy.sh with a malformed second parameter (rc=$RC) must exit 2 without create-stack" "$OUT"
fi

# --------------------------------------------------------------------------- run-probe.sh (sweep)
# The Windows probe command fails (status Failed, no stdout). run-probe must not merge the Linux
# side alone into a record labeled cross-host, and must exit non-zero.
RP_MOCK="$TMP/rp-mock"; mkdir -p "$RP_MOCK"
cat >"$RP_MOCK/aws" <<'MOCK'
#!/bin/bash
case "$*" in
  *"ssm send-command"*) echo cmd-1 ;;
  *"ssm wait command-executed"*) exit 255 ;;
  *"--instance-id i-win --query Status "*) echo Failed ;;
  *"--query Status "*) echo Success ;;
  *"--instance-id i-win --query ResponseCode "*) echo 1 ;;
  *"--query ResponseCode "*) echo 0 ;;
  *"--instance-id i-win --query StandardOutputContent "*) echo "" ;;
  *"--query StandardOutputContent "*)
    echo '{"schema":"appmod-probe/1","role":"contender","behaviors":[{"id":"file-locking","outcome":"measured","observed":{"topology":"single-host"}}]}' ;;
  *) echo "run-probe mock: unexpected aws call: $*" >&2; exit 1 ;;
esac
MOCK
chmod +x "$RP_MOCK/aws"
rm -rf .private/runs/s0-rpfailtest
OUT="$(env -u APPMOD_DRY_RUN PATH="$RP_MOCK:$PATH" bash scripts/run-probe.sh --stage 0 --run-id s0-rpfailtest \
  --windows-instance i-win --linux-instance i-lnx --bucket appmod-artifacts-example 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && [ ! -f .private/runs/s0-rpfailtest/merged.json ] && ! has "coordination complete" "$OUT"; then
  pass "run-probe does not merge (exit $RC) when one side's probe command failed"
else
  fail "run-probe with a failed Windows probe (rc=$RC) must exit non-zero and write no merged.json" "$OUT"
fi
rm -rf .private/runs/s0-rpfailtest

# --------------------------------------------------------------------------- inventory.sh (sweep)
# An unreadable directory under seed/ makes find fail part-way; the inventory must not be written
# as if complete.
INV="$TMP/inv-mount"; mkdir -p "$INV/seed/locked"
printf 'a\n' >"$INV/seed/a.txt"; printf 'b\n' >"$INV/seed/locked/b.txt"
chmod 000 "$INV/seed/locked"
if ls "$INV/seed/locked" >/dev/null 2>&1; then
  pass "inventory unreadable-directory case skipped (running with privileges that ignore mode 000)"
else
  OUT="$(bash scripts/inventory.sh --mount "$INV" --out "$TMP/inv.json" 2>&1)"; RC=$?
  if [ "$RC" -eq 1 ] && has "listing" "$OUT"; then
    pass "inventory stops (exit 1) when listing seed/ fails part-way"
  else
    fail "inventory with an unreadable seed/ subdirectory (rc=$RC) must exit 1" "$OUT"
  fi
fi
chmod 755 "$INV/seed/locked"

# --------------------------------------------------------------------------- setup-workspace (sweep)
# git and gitleaks are mocked; the clone writes a stub install.sh. The pinned commit is read from
# the script so it is not repeated here.
SW_MOCK="$TMP/sw-mock"; mkdir -p "$SW_MOCK"
cat >"$SW_MOCK/git" <<'MOCK'
#!/bin/bash
if [ "$1" = "clone" ]; then
  for dest; do :; done
  # The stub install.sh writes the agent config into its current directory, as the real one does,
  # so step 4 finds it only when setup-workspace.sh ran install.sh inside the workspace.
  mkdir -p "$dest/.git"
  printf '%s\n' '#!/bin/bash' 'mkdir -p .kiro/agents' \
    'echo "{\"hooks\": {}}" >.kiro/agents/migration.json' >"$dest/install.sh"
  exit 0
fi
if [ "$1" = "-C" ]; then
  repo="$2"; shift 2
  case "$1" in
    rev-parse) echo "$MOCK_GIT_HEAD" ;;
    init) mkdir -p "$repo/.git" ;;
    check-ignore) exit 0 ;;
    remote)
      # Only `remote -v` (step 7) fails; step 1's plain `remote` lists nothing.
      if [ -n "${MOCK_GIT_REMOTE_FAIL:-}" ] && [ "${2:-}" = "-v" ]; then
        echo "fatal: not a git repository" >&2; exit 128
      fi ;;
  esac
  exit 0
fi
echo "git mock: unexpected call: $*" >&2; exit 1
MOCK
cat >"$SW_MOCK/gitleaks" <<'MOCK'
#!/bin/bash
# MOCK_GITLEAKS=broken: exit 2 with no report (a config that does not load). Otherwise report one
# finding and exit 1, as a scan that flags the planted key does.
if [ "${MOCK_GITLEAKS:-}" = "broken" ]; then echo "gitleaks mock: config failed to load" >&2; exit 2; fi
report=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--report-path" ]; then report="$2"; shift 2; continue; fi
  shift
done
if [ -n "$report" ]; then printf '[{"RuleID":"aws-access-token"}]\n' >"$report"; fi
exit 1
MOCK
chmod +x "$SW_MOCK/git" "$SW_MOCK/gitleaks"
PIN="$(sed -n 's/^AIMF_COMMIT="\(.*\)"$/\1/p' scripts/aimf/setup-workspace.sh)"
SW_ENV=(env -u APPMOD_DRY_RUN PATH="$SW_MOCK:$PATH" MOCK_GIT_HEAD="$PIN")
# Step 6: a gitleaks that exits non-zero without a finding must not read as "flagged".
OUT="$("${SW_ENV[@]}" APPMOD_AIMF_WORKSPACE="$TMP/ws6" MOCK_GITLEAKS=broken \
  bash scripts/aimf/setup-workspace.sh 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && ! has "setup-workspace: done" "$OUT"; then
  pass "setup-workspace step 6 fails (exit $RC) when gitleaks exits non-zero without a finding"
else
  fail "setup-workspace with a gitleaks that cannot load its config (rc=$RC) must not pass step 6" "$OUT"
fi
# Step 7: an unreadable repository must not read as "no remote".
OUT="$("${SW_ENV[@]}" APPMOD_AIMF_WORKSPACE="$TMP/ws7" MOCK_GIT_REMOTE_FAIL=1 \
  bash scripts/aimf/setup-workspace.sh 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && has "send-scan flagged the planted key" "$OUT" && ! has "setup-workspace: done" "$OUT"; then
  pass "setup-workspace step 7 fails (exit $RC) when git cannot read a repository's remotes"
else
  fail "setup-workspace with an unreadable repository (rc=$RC) must not pass step 7" "$OUT"
fi
# Control: both mocks healthy, the run completes.
OUT="$("${SW_ENV[@]}" APPMOD_AIMF_WORKSPACE="$TMP/wsok" bash scripts/aimf/setup-workspace.sh 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && has "setup-workspace: done" "$OUT"; then
  pass "setup-workspace control: healthy gitleaks and git complete the run"
else
  fail "setup-workspace control (rc=$RC) should complete" "$OUT"
fi

# Step 6 with the real gitleaks and the real gitleaks-send.toml (git stays mocked): the planted key
# must be flagged. The documentation example key is allowlisted by the default AWS rule, so a
# self-test built on it can never pass. gitleaks is required here, as it is for `make audit`.
SW_GITONLY="$TMP/sw-gitonly"; mkdir -p "$SW_GITONLY"; cp "$SW_MOCK/git" "$SW_GITONLY/git"
if ! command -v gitleaks >/dev/null 2>&1; then
  fail "setup-workspace step 6 with the real gitleaks: gitleaks is not installed"
else
  OUT="$(env -u APPMOD_DRY_RUN PATH="$SW_GITONLY:$PATH" MOCK_GIT_HEAD="$PIN" \
    APPMOD_AIMF_WORKSPACE="$TMP/wsreal" bash scripts/aimf/setup-workspace.sh 2>&1)"; RC=$?
  if [ "$RC" -eq 0 ] && has "send-scan flagged the planted key (exit 1" "$OUT" \
    && has "setup-workspace: done" "$OUT"; then
    pass "setup-workspace step 6: the real gitleaks-send.toml flags the run-time planted key"
  else
    fail "setup-workspace step 6 with the real gitleaks (rc=$RC) must flag the planted key" "$OUT"
  fi
fi

# --------------------------------------------------------------------------- lock-fsxadmin (F1)
# A mocked aws logs every call. A real `on` must never write a Deny for a principal that is not the
# Linux instance role, so a missing or malformed APPMOD_LINUX_ROLE_ARN exits 2 before any call.
LK_MOCK="$TMP/lk-mock"; mkdir -p "$LK_MOCK"
cat >"$LK_MOCK/aws" <<'MOCK'
#!/bin/bash
echo "aws $*" >>"$MOCK_AWS_LOG"
case "$*" in
  *"ec2 describe-vpcs"*)
    if [ -n "${MOCK_VPC_FAIL:-}" ]; then
      echo "Error when retrieving token from sso: Token has expired and refresh failed" >&2; exit 255
    fi
    printf '%s\n' "${MOCK_VPC_CIDRS:-}" ;;
  *) echo "{}" ;;
esac
MOCK
chmod +x "$LK_MOCK/aws"
LK_ENV=(env -u APPMOD_DRY_RUN -u APPMOD_LINUX_ROLE_ARN PATH="$LK_MOCK:$PATH")
MOCK_AWS_LOG="$TMP/lk-unset.log"; : >"$MOCK_AWS_LOG"
OUT="$("${LK_ENV[@]}" bash scripts/aimf/lock-fsxadmin.sh on 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && has "APPMOD_LINUX_ROLE_ARN" "$OUT" && ! grep -q "put-resource-policy" "$MOCK_AWS_LOG"; then
  pass "lock-fsxadmin on without APPMOD_LINUX_ROLE_ARN exits 2 with no put-resource-policy"
else
  fail "lock-fsxadmin on without APPMOD_LINUX_ROLE_ARN (rc=$RC) must exit 2 before any call" \
    "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi
MOCK_AWS_LOG="$TMP/lk-bad.log"; : >"$MOCK_AWS_LOG"
OUT="$("${LK_ENV[@]}" APPMOD_LINUX_ROLE_ARN=appmod-linux-role bash scripts/aimf/lock-fsxadmin.sh on 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && has "not an IAM role ARN" "$OUT" && [ ! -s "$MOCK_AWS_LOG" ]; then
  pass "lock-fsxadmin on with a value that is not an IAM role ARN exits 2 with no call"
else
  fail "lock-fsxadmin on with APPMOD_LINUX_ROLE_ARN=appmod-linux-role (rc=$RC) must exit 2 before any call" \
    "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi
# N1: the ARN is matched as a whole string. The script's own placeholder account, a short account,
# and an ARN that only contains ":role/" after another resource type all exit 2 with no call.
for LK_BAD in "arn:aws:iam::123456789012:role/appmod-test-LinuxRole" \
              "arn:aws:iam::1:role/x" \
              "arn:aws:iam::1:user/x:role/y" \
              "arn:aws:iam::12345678901234:role/x"; do
  MOCK_AWS_LOG="$TMP/lk-n1.log"; : >"$MOCK_AWS_LOG"
  OUT="$("${LK_ENV[@]}" APPMOD_LINUX_ROLE_ARN="$LK_BAD" bash scripts/aimf/lock-fsxadmin.sh on 2>&1)"; RC=$?
  if [ "$RC" -eq 2 ] && [ ! -s "$MOCK_AWS_LOG" ]; then
    pass "lock-fsxadmin on rejects APPMOD_LINUX_ROLE_ARN=$LK_BAD (exit 2, no call)"
  else
    fail "lock-fsxadmin on with APPMOD_LINUX_ROLE_ARN=$LK_BAD (rc=$RC) must exit 2 before any call" \
      "$OUT" "$(cat "$MOCK_AWS_LOG")"
  fi
done
# Control: a role ARN is accepted and becomes the Deny's principal. The account is the AWS
# documentation example account, not the placeholder the script rejects.
# Assembled at runtime from the AWS documentation example account (1111-2222-3333): any 12-digit
# literal is read as an account ID by the secret scanners, and the placeholder is what is rejected.
LK_DOC_ACCT="$(printf '%s%s%s' 1111 2222 3333)"
LK_ARN="arn:aws:iam::$LK_DOC_ACCT:role/appmod-base-LinuxRole-Example"
MOCK_AWS_LOG="$TMP/lk-ok.log"; : >"$MOCK_AWS_LOG"
OUT="$("${LK_ENV[@]}" APPMOD_LINUX_ROLE_ARN="$LK_ARN" bash scripts/aimf/lock-fsxadmin.sh on 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "put-resource-policy" "$MOCK_AWS_LOG" \
  && grep -qF "\"AWS\":\"$LK_ARN\"" "$MOCK_AWS_LOG"; then
  pass "lock-fsxadmin on control: a role ARN is written as the Deny principal"
else
  fail "lock-fsxadmin on control with a role ARN (rc=$RC) should call put-resource-policy for it" \
    "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi

# --------------------------------------------------------------------------- preflight --cidr (F2)
# The check the message announces must run: an overlapping existing VPC CIDR (any association, not
# only the primary one) fails the phase, and so does a failed listing. Documentation ranges only.
PF_ARGS=(bash scripts/preflight.sh --phase network --cidr 198.51.100.128/25)
MOCK_AWS_LOG="$TMP/pf-overlap.log"; : >"$MOCK_AWS_LOG"
OUT="$("${LK_ENV[@]}" MOCK_VPC_CIDRS="$(printf '192.0.2.0/24\t198.51.100.0/24')" "${PF_ARGS[@]}" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && has "overlaps existing VPC CIDR(s): 198.51.100.0/24" "$OUT" \
  && ! has "Recommended EgressMode" "$OUT"; then
  pass "preflight --cidr fails (exit 1) on an overlapping secondary VPC CIDR"
else
  fail "preflight --cidr with an overlapping existing VPC CIDR (rc=$RC) must exit 1" "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi
MOCK_AWS_LOG="$TMP/pf-fail.log"; : >"$MOCK_AWS_LOG"
OUT="$("${LK_ENV[@]}" MOCK_VPC_FAIL=1 "${PF_ARGS[@]}" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && ! has "Recommended EgressMode" "$OUT"; then
  pass "preflight --cidr fails (exit 1) when describe-vpcs fails"
else
  fail "preflight --cidr with a failed describe-vpcs (rc=$RC) must exit 1" "$OUT"
fi
MOCK_AWS_LOG="$TMP/pf-ok.log"; : >"$MOCK_AWS_LOG"
OUT="$("${LK_ENV[@]}" MOCK_VPC_CIDRS="$(printf '192.0.2.0/24\t203.0.113.0/24')" "${PF_ARGS[@]}" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && has "no existing VPC CIDR overlaps 198.51.100.128/25" "$OUT" \
  && grep -q "CidrBlockAssociationSet" "$MOCK_AWS_LOG"; then
  pass "preflight --cidr control: no overlap is reported as compared, and the phase passes"
else
  fail "preflight --cidr control without overlap (rc=$RC) should pass after comparing" "$OUT" "$(cat "$MOCK_AWS_LOG")"
fi

# --------------------------------------------------------------------------- stage0-smb DC (F3)
# HTTP 401/403 on cifs/domains is a credential or role error: exit 1, not the exit-4 "tear down and
# recreate" branch. Any other HTTP error answer keeps the designed exit 4.
for S0_CODE in 401 403 404; do
  MOCK_CURL_ROUTES="$TMP/s0dc-$S0_CODE.json"; MOCK_CURL_LOG="$TMP/s0dc-$S0_CODE.log"; : >"$MOCK_CURL_LOG"
  cat >"$MOCK_CURL_ROUTES" <<EOF
[{"method":"GET","path":"/api/svm/svms?name=appmodsvm","body":{"records":[{"uuid":"svm-u"}],"num_records":1}},
{"method":"GET","path":"/api/protocols/cifs/domains/svm-u","status":$S0_CODE,"body":{"error":{"message":"mock HTTP $S0_CODE"}}}]
EOF
  OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/stage0-smb.sh --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata 2>&1)"; RC=$?
  if [ "$S0_CODE" = 404 ]; then
    if [ "$RC" -eq 4 ] && has "tear down and recreate" "$OUT"; then
      pass "stage0-smb keeps exit 4 for an HTTP 404 on cifs/domains (no DC discovered)"
    else
      fail "stage0-smb with an HTTP 404 on cifs/domains (rc=$RC) should keep exit 4" "$OUT" "$(cat "$MOCK_CURL_LOG")"
    fi
  elif [ "$RC" -eq 1 ] && has "credential or role error, not a missing DC" "$OUT" \
    && ! has "tear down and recreate" "$OUT"; then
    pass "stage0-smb exits 1 (not 4) on an HTTP $S0_CODE from cifs/domains"
  else
    fail "stage0-smb with an HTTP $S0_CODE on cifs/domains (rc=$RC) must exit 1, not 4" "$OUT" "$(cat "$MOCK_CURL_LOG")"
  fi
done

echo "----"
if [ "$FAILURES" -ne 0 ]; then
  echo "failure_path_tests: $FAILURES failure(s)" >&2
  exit 1
fi
echo "failure_path_tests: all cases passed"
