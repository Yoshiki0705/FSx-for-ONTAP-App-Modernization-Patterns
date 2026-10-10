#!/usr/bin/env bash
#
# Dry-run tests for the AWS- and ONTAP-calling shell scripts. Every case runs with APPMOD_DRY_RUN=1
# (and fixtures for ONTAP), so NO AWS, ONTAP or network call is made. Covers the design test plan:
# deploy.sh's six entry-check cases, run-atx.sh's same entry check plus its estimate-parameter
# refusals (no parameters, no LimitMinutes, limit 0, transformation outside the allow-list), the
# allow-list parity with estimate.py, and its PATH-mocked real-mode cases (unverified, atx failure,
# success, no debug log, per-transformation and per-limit record mismatch, exit 2 at the limit,
# commit-less / dirty / analysis-copy dotnet send dirs, dotnet success), block_direct_atx.py's two cases,
# check-no-locking.sh's locked / missing-appdata / absent-field cases, integration-clone.sh's rejections, and that
# the happy paths reach their dry-run action.
#
# Exits 0 only when every expectation holds. Called from `make test`.
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1
FIXTURES="scripts/tests/fixtures"
FAILURES=0

expect_exit() {
  local want="$1" label="$2"; shift 2
  local got
  "$@" >/dev/null 2>&1
  got=$?
  if [ "$got" -ne "$want" ]; then
    echo "FAIL: $label (expected exit $want, got $got)" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "ok: $label"
  fi
}

check_contains() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    echo "ok: $label"
  else
    echo "FAIL: $label (missing: $needle)" >&2
    FAILURES=$((FAILURES + 1))
  fi
}
check_absent() {
  local label="$1" needle="$2" haystack="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    echo "FAIL: $label (should not contain: $needle)" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "ok: $label"
  fi
}
# check_before <label> <first> <second> <haystack>: the first line containing <first> comes before
# the first line containing <second>, and both are present.
check_before() {
  local label="$1" first="$2" second="$3" haystack="$4" a b
  a="$(printf '%s\n' "$haystack" | grep -nF -- "$first" | head -1 | cut -d: -f1)"
  b="$(printf '%s\n' "$haystack" | grep -nF -- "$second" | head -1 | cut -d: -f1)"
  if [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; then
    echo "ok: $label"
  else
    echo "FAIL: $label ('$first' at line ${a:-none}, '$second' at line ${b:-none})" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
iso_hours_ago() {
  # portable "N hours ago" in UTC
  python3 -c "import datetime,sys; print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"
}

# --- deploy.sh: six entry-check cases -------------------------------------------------------------
# Build a fresh, valid estimate + approval, then vary one thing per case.
EST_DIR="$TMP/estimates"
mkdir -p "$EST_DIR/used"
APPROVAL="$TMP/approval.json"

CREATED="$(iso_hours_ago 1)"
APPROVED="$(iso_now)"
# The approved all-new deployment parameters, as estimate.py records them. deploy.sh reads this
# "parameters" object and passes it to create-stack; the 10.90.0.0/16 values are the approved run.
PARAMS_JSON='[{"ParameterKey":"CreateVpc","ParameterValue":"true"},{"ParameterKey":"CreateSubnets","ParameterValue":"true"},{"ParameterKey":"VpcCidr","ParameterValue":"10.90.0.0/16"},{"ParameterKey":"PrimarySubnetCidr","ParameterValue":"10.90.0.0/24"},{"ParameterKey":"SecondAzSubnetCidr","ParameterValue":"10.90.1.0/24"},{"ParameterKey":"CreateInterfaceEndpoints","ParameterValue":"true"},{"ParameterKey":"CreateS3GatewayEndpoint","ParameterValue":"true"},{"ParameterKey":"CreateDirectory","ParameterValue":"true"},{"ParameterKey":"EgressMode","ParameterValue":"endpoints"}]'
EST_BASE="$EST_DIR/20260101T000000Z-base.json"
cat >"$EST_BASE" <<EOF
{"target": "base", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 72, "parameters": $PARAMS_JSON}
EOF
cat >"$APPROVAL" <<EOF
[{"target": "base", "approved_at": "$APPROVED", "hours": 72, "estimate_file": "$EST_BASE"}]
EOF

export APPMOD_DRY_RUN=1
export APPMOD_ESTIMATES_DIR="$EST_DIR"
export APPMOD_APPROVAL_FILE="$APPROVAL"

# 1. valid -> deploy proceeds (exit 0). Use a copy so the move to used/ does not consume the fixture.
cp "$EST_BASE" "$EST_DIR/case-valid-base.json"
cat >>"$APPROVAL" <<EOF
EOF
# Rebuild approval to include the case-valid file.
cat >"$APPROVAL" <<EOF
[{"target": "base", "approved_at": "$APPROVED", "hours": 72, "estimate_file": "$EST_DIR/case-valid-base.json"},
 {"target": "atx", "approved_at": "$APPROVED", "estimate_file": "$EST_DIR/case-atx.json"}]
EOF
expect_exit 0 "deploy.sh base valid estimate" \
  bash scripts/deploy.sh base --estimate "$EST_DIR/case-valid-base.json" --approved-at "$APPROVED"

# 1b. The valid dry-run create line must carry --parameters with the approved VpcCidr, proving the
# estimate's parameters reach create-stack rather than the template defaults being used.
VALID_OUT="$(APPMOD_DRY_RUN=1 bash scripts/deploy.sh base \
  --estimate "$EST_DIR/case-valid-base.json" --approved-at "$APPROVED" 2>/dev/null)"
if echo "$VALID_OUT" | grep -q -- "--parameters" \
  && echo "$VALID_OUT" | grep -q "ParameterKey=VpcCidr,ParameterValue=10.90.0.0/16"; then
  echo "ok: deploy.sh base dry-run passes --parameters VpcCidr=10.90.0.0/16"
else
  echo "FAIL: deploy.sh base dry-run did not pass --parameters VpcCidr=10.90.0.0/16" >&2
  echo "$VALID_OUT" >&2
  FAILURES=$((FAILURES + 1))
fi

# 2. no estimate file
expect_exit 2 "deploy.sh base missing estimate" \
  bash scripts/deploy.sh base --estimate "$EST_DIR/nope.json" --approved-at "$APPROVED"

# 3. estimate 25h old
OLD="$EST_DIR/old-base.json"
cat >"$OLD" <<EOF
{"target": "base", "region": "ap-northeast-1", "created_at": "$(iso_hours_ago 25)", "hours": 72}
EOF
cat >>"$APPROVAL" <<EOF
EOF
python3 - "$APPROVAL" "$OLD" "$APPROVED" <<'PY'
import json,sys
path,est,approved=sys.argv[1:4]
data=json.load(open(path))
data.append({"target":"base","approved_at":approved,"hours":72,"estimate_file":est})
json.dump(data,open(path,"w"))
PY
expect_exit 2 "deploy.sh base 25h-old estimate" \
  bash scripts/deploy.sh base --estimate "$OLD" --approved-at "$APPROVED"

# 4. already used (same name under used/)
USED="$EST_DIR/used-base.json"
cat >"$USED" <<EOF
{"target": "base", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 72}
EOF
cp "$USED" "$EST_DIR/used/used-base.json"
expect_exit 2 "deploy.sh base used estimate" \
  bash scripts/deploy.sh base --estimate "$USED" --approved-at "$APPROVED"

# 5. no --approved-at
expect_exit 2 "deploy.sh base no approved-at" \
  bash scripts/deploy.sh base --estimate "$EST_DIR/case-valid-base.json"

# 6a. approval.json has no matching record
LONELY="$EST_DIR/lonely-base.json"
cat >"$LONELY" <<EOF
{"target": "base", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 72}
EOF
expect_exit 2 "deploy.sh base no approval match" \
  bash scripts/deploy.sh base --estimate "$LONELY" --approved-at "$APPROVED"

# 6b. region mismatch
WRONGREGION="$EST_DIR/wrongregion-base.json"
cat >"$WRONGREGION" <<EOF
{"target": "base", "region": "us-east-1", "created_at": "$CREATED", "hours": 72}
EOF
python3 - "$APPROVAL" "$WRONGREGION" "$APPROVED" <<'PY'
import json,sys
path,est,approved=sys.argv[1:4]
data=json.load(open(path))
data.append({"target":"base","approved_at":approved,"hours":72,"estimate_file":est})
json.dump(data,open(path,"w"))
PY
expect_exit 2 "deploy.sh base wrong region" \
  bash scripts/deploy.sh base --estimate "$WRONGREGION" --approved-at "$APPROVED"

# 7. estimate WITHOUT a parameters object -> refused (exit 2) for base. Fresh, unused, approved,
# in-region, target-matched, so it passes the entry check and fails only on the missing parameters
# object. (stage3, below, is deliberately not subject to this gate.)
NOPARAMS="$EST_DIR/noparams-base.json"
cat >"$NOPARAMS" <<EOF
{"target": "base", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 72}
EOF
python3 - "$APPROVAL" "$NOPARAMS" "$APPROVED" <<'PY'
import json,sys
path,est,approved=sys.argv[1:4]
data=json.load(open(path))
data.append({"target":"base","approved_at":approved,"hours":72,"estimate_file":est})
json.dump(data,open(path,"w"))
PY
expect_exit 2 "deploy.sh base estimate without parameters object" \
  bash scripts/deploy.sh base --estimate "$NOPARAMS" --approved-at "$APPROVED"

# 8. stage3: the parameters gate is base-only. A stage3 estimate carries no parameters object
# (its required IDs come from the base stack and a later task, so parameter passing is deferred).
# deploy.sh stage3 must NOT refuse it on the missing-parameters gate, and the dry-run create line
# must NOT carry --parameters. Fresh, unused, approved, in-region, target-matched stage3 estimate.
EST_STAGE3="$EST_DIR/case-stage3.json"
cat >"$EST_STAGE3" <<EOF
{"target": "stage3", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 24}
EOF
python3 - "$APPROVAL" "$EST_STAGE3" "$APPROVED" <<'PY'
import json,sys
path,est,approved=sys.argv[1:4]
data=json.load(open(path))
data.append({"target":"stage3","approved_at":approved,"hours":24,"estimate_file":est})
json.dump(data,open(path,"w"))
PY
cp "$EST_STAGE3" "$EST_DIR/case-stage3-run.json"
python3 - "$APPROVAL" "$EST_DIR/case-stage3-run.json" "$APPROVED" <<'PY'
import json,sys
path,est,approved=sys.argv[1:4]
data=json.load(open(path))
data.append({"target":"stage3","approved_at":approved,"hours":24,"estimate_file":est})
json.dump(data,open(path,"w"))
PY
expect_exit 0 "deploy.sh stage3 estimate without parameters object (gate is base-only)" \
  bash scripts/deploy.sh stage3 --estimate "$EST_DIR/case-stage3-run.json" --approved-at "$APPROVED"

STAGE3_OUT="$(APPMOD_DRY_RUN=1 bash scripts/deploy.sh stage3 \
  --estimate "$EST_STAGE3" --approved-at "$APPROVED" 2>/dev/null)"
if echo "$STAGE3_OUT" | grep -q "create-stack" \
  && ! echo "$STAGE3_OUT" | grep -q -- "--parameters"; then
  echo "ok: deploy.sh stage3 dry-run creates the stack with no --parameters (deferred)"
else
  echo "FAIL: deploy.sh stage3 dry-run should create-stack without --parameters" >&2
  echo "$STAGE3_OUT" >&2
  FAILURES=$((FAILURES + 1))
fi

# --- run-atx.sh: same entry check with target=atx -------------------------------------------------
# The atx estimate records Transformation and LimitMinutes as estimate.py --target atx writes them;
# run-atx.sh reads both from here and from nowhere else.
atx_params_json() {  # atx_params_json <transformation> <limit>
  printf '[{"ParameterKey":"Transformation","ParameterValue":"%s"},{"ParameterKey":"LimitMinutes","ParameterValue":"%s"}]' "$1" "$2"
}
EST_ATX="$EST_DIR/case-atx.json"
cat >"$EST_ATX" <<EOF
{"target": "atx", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 0, "parameters": $(atx_params_json AWS/comprehensive-codebase-analysis 120)}
EOF
export APPMOD_SEND_DIR="$TMP/senddir"
mkdir -p "$APPMOD_SEND_DIR"
expect_exit 0 "run-atx.sh valid estimate (dry-run)" \
  bash scripts/aimf/run-atx.sh --estimate "$EST_ATX" --approved-at "$APPROVED"
ATX_DRY_OUT="$(bash scripts/aimf/run-atx.sh --estimate "$EST_ATX" --approved-at "$APPROVED" 2>&1)"
check_contains "run-atx.sh dry-run passes the estimate's limit to atx --limit" \
  "atx custom def exec -n AWS/comprehensive-codebase-analysis -p . -x -t --limit 120" "$ATX_DRY_OUT"
expect_exit 2 "run-atx.sh wrong target estimate" \
  bash scripts/aimf/run-atx.sh --estimate "$EST_BASE" --approved-at "$APPROVED"
# Each estimate below passes the entry check (fresh, approved, in-region, target atx) and fails only
# on its parameters, so the exit 2 can come from nothing else.
atx_dry_refused() {  # atx_dry_refused <name> <label> <parameters-json-or-empty>
  local path="$EST_DIR/$1.json" params_field=""
  [ -n "$3" ] && params_field=", \"parameters\": $3"
  printf '{"target": "atx", "region": "ap-northeast-1", "created_at": "%s", "hours": 0%s}\n' \
    "$CREATED" "$params_field" >"$path"
  python3 - "$APPROVAL" "$path" "$APPROVED" <<'PY'
import json,sys
p,est,approved=sys.argv[1:4]
d=json.load(open(p)); d.append({"target":"atx","approved_at":approved,"estimate_file":est}); json.dump(d,open(p,"w"))
PY
  expect_exit 2 "$2" bash scripts/aimf/run-atx.sh --estimate "$path" --approved-at "$APPROVED"
}
atx_dry_refused atx-noparams "run-atx.sh refuses an atx estimate without parameters" ""
atx_dry_refused atx-nolimit "run-atx.sh refuses an atx estimate without LimitMinutes" \
  '[{"ParameterKey":"Transformation","ParameterValue":"AWS/comprehensive-codebase-analysis"}]'
atx_dry_refused atx-limit0 "run-atx.sh refuses LimitMinutes 0" \
  "$(atx_params_json AWS/comprehensive-codebase-analysis 0)"
atx_dry_refused atx-unknown-tx "run-atx.sh refuses a transformation outside the allow-list" \
  "$(atx_params_json AWS/java-version-upgrade 120)"
unset APPMOD_SEND_DIR
# The allow-list is held in two places (estimate.py ATX_TRANSFORMATIONS and the case in run-atx.sh);
# both must name exactly the same transformations.
if python3 - <<'PY'
import importlib.util, re, sys
spec = importlib.util.spec_from_file_location("estimate", "scripts/estimate.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
text = open("scripts/aimf/run-atx.sh", encoding="utf-8").read()
in_case = set(re.findall(r"^\s+(AWS/[A-Za-z0-9-]+)\)\s*$", text, re.M))
sys.exit(0 if in_case == set(m.ATX_TRANSFORMATIONS) else 1)
PY
then
  echo "ok: run-atx.sh allow-list matches estimate.py ATX_TRANSFORMATIONS"
else
  echo "FAIL: run-atx.sh allow-list differs from estimate.py ATX_TRANSFORMATIONS" >&2
  FAILURES=$((FAILURES + 1))
fi

# --- block_direct_atx.py: two cases ---------------------------------------------------------------
if echo '{"command": "atx run --project DocIntake"}' | python3 scripts/aimf/block_direct_atx.py >/dev/null 2>&1; then
  echo "FAIL: block_direct_atx direct atx should exit 2" >&2; FAILURES=$((FAILURES+1))
else
  # python exits 2 here; the `if` treats any non-zero as the else branch, which is what we want.
  echo "ok: block_direct_atx direct atx blocked"
fi
if echo '{"command": "bash scripts/aimf/run-atx.sh --estimate e.json"}' | python3 scripts/aimf/block_direct_atx.py >/dev/null 2>&1; then
  echo "ok: block_direct_atx run-atx.sh allowed"
else
  echo "FAIL: block_direct_atx run-atx.sh should pass" >&2; FAILURES=$((FAILURES+1))
fi

# --- check-no-locking.sh: clean / locked / missing-appdata ----------------------------------------
expect_exit 0 "check-no-locking clean" \
  env APPMOD_ONTAP_FIXTURE="$FIXTURES/ontap_clean.json" bash scripts/ontap/check-no-locking.sh
expect_exit 3 "check-no-locking locked volume" \
  env APPMOD_ONTAP_FIXTURE="$FIXTURES/ontap_locked.json" bash scripts/ontap/check-no-locking.sh
expect_exit 3 "check-no-locking appdata absent" \
  env APPMOD_ONTAP_FIXTURE="$FIXTURES/ontap_no_appdata.json" bash scripts/ontap/check-no-locking.sh
# An absent field must fail closed: only "non_snaplock" and an explicit false read as clean.
expect_exit 3 "check-no-locking snaplock object absent" \
  env APPMOD_ONTAP_FIXTURE="$FIXTURES/ontap_snaplock_absent.json" bash scripts/ontap/check-no-locking.sh
expect_exit 3 "check-no-locking locking field absent" \
  env APPMOD_ONTAP_FIXTURE="$FIXTURES/ontap_locking_field_absent.json" bash scripts/ontap/check-no-locking.sh

# --- integration-clone.sh: name/range validation -------------------------------------------------
# A management IP is passed so an exit 2 can only come from the name check, and each rejection must
# happen before any ONTAP request is built (no curl line in the output).
IC=(bash scripts/ontap/integration-clone.sh)
expect_exit 0 "integration-clone create --step 3" "${IC[@]}" create --step 3 --mgmt-ip 203.0.113.5
for bad in "create --name appdata" "create --name appdata_it_0" "delete --step 100" \
           "delete --name appdata_it_100" "create --name other"; do
  # shellcheck disable=SC2086  # $bad is split into its words on purpose
  BAD_OUT="$("${IC[@]}" $bad --mgmt-ip 203.0.113.5 2>&1)"; BAD_RC=$?
  if [ "$BAD_RC" -eq 2 ] && ! printf '%s' "$BAD_OUT" | grep -q "curl " \
    && printf '%s' "$BAD_OUT" | grep -qE "refusing|must be appdata_it_|out of range"; then
    echo "ok: integration-clone rejects '$bad' with exit 2 before any ONTAP call"
  else
    echo "FAIL: integration-clone '$bad' (rc=$BAD_RC) should exit 2 on the name check with no call" >&2
    echo "$BAD_OUT" >&2
    FAILURES=$((FAILURES + 1))
  fi
done

# --- --svm takes the ONTAP SVM name, never the SVM ID from the FSx for ONTAP API --------------------------------
# The live teardown on 2026-10-07 was given the SVM ID and only failed at step 4. Every script that
# takes --svm must reject the svm-<hex> form with exit 2 before building any call (no DRY-RUN line).
# A short placeholder id, so the secret scan does not read it as a real one. record-boundary.sh
# runs from $TMP because its dry-run writes a record under .private/runs/ in the working directory.
SVM_ID_ARG="svm-0123abcd"
svm_id_rejected() {  # svm_id_rejected <label> <command...>
  local label="$1" out rc; shift
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq 2 ] && ! printf '%s' "$out" | grep -q "DRY-RUN" \
    && printf '%s' "$out" | grep -qF "not the SVM ID from the FSx for ONTAP API"; then
    echo "ok: $label rejects an SVM ID from the FSx for ONTAP API passed as --svm (exit 2, no call)"
  else
    echo "FAIL: $label with --svm $SVM_ID_ARG (rc=$rc) must exit 2 before any call" >&2
    printf '%s\n' "$out" >&2
    FAILURES=$((FAILURES + 1))
  fi
}
svm_id_rejected "teardown --apply" env APPMOD_DRY_RUN=1 bash scripts/teardown.sh --apply \
  --file-system-id fs-test --linux-instance i-lnx --bucket appmod-artifacts-example \
  --windows-role appmod-test-WindowsRole --svm "$SVM_ID_ARG"
svm_id_rejected "teardown report-only via APPMOD_SVM" env APPMOD_SVM="$SVM_ID_ARG" bash scripts/teardown.sh
svm_id_rejected "integration-clone sweep" env APPMOD_DRY_RUN=1 bash scripts/ontap/integration-clone.sh \
  sweep --credential fsxadmin --mgmt-ip 203.0.113.5 --svm "$SVM_ID_ARG"
svm_id_rejected "integration-clone create" env APPMOD_DRY_RUN=1 bash scripts/ontap/integration-clone.sh \
  create --step 3 --mgmt-ip 203.0.113.5 --svm "$SVM_ID_ARG"
svm_id_rejected "stage0-smb" env APPMOD_DRY_RUN=1 bash scripts/ontap/stage0-smb.sh \
  --mgmt-ip 203.0.113.5 --svm "$SVM_ID_ARG"
svm_id_rejected "stage1-nfs" env APPMOD_DRY_RUN=1 bash scripts/ontap/stage1-nfs.sh \
  --client-cidr 10.0.0.0/24 --mgmt-ip 203.0.113.5 --svm "$SVM_ID_ARG"
# shellcheck disable=SC2016  # $1 and $@ expand in the child bash, not here
svm_id_rejected "record-boundary" bash -c 'cd "$1" && shift && exec "$@"' _ "$TMP" \
  env APPMOD_DRY_RUN=1 bash "$REPO_ROOT/scripts/ontap/record-boundary.sh" --boundary b0 \
  --run-id s0-20261007T000000Z --mgmt-ip 203.0.113.5 --svm "$SVM_ID_ARG"
# Control: the SVM name itself is accepted (the dry-run reaches its first call).
SVM_OK_OUT="$(APPMOD_DRY_RUN=1 bash scripts/ontap/integration-clone.sh sweep --credential fsxadmin \
  --mgmt-ip 203.0.113.5 --svm appmodsvm 2>&1)"
check_contains "integration-clone accepts the SVM name appmodsvm (control)" "DRY-RUN" "$SVM_OK_OUT"
# --- preflight / create-secrets / lock-fsxadmin / teardown: flow runs under dry-run --------------
expect_exit 0 "preflight network new-vpc dry-run" bash scripts/preflight.sh --phase network
expect_exit 0 "preflight network new-vpc with cidr dry-run" \
  bash scripts/preflight.sh --phase network --cidr 10.0.0.0/16
expect_exit 0 "preflight network existing-vpc dry-run" \
  bash scripts/preflight.sh --phase network --vpc-id vpc-0123456789abcdef0 --subnet-id subnet-0123456789abcdef0
expect_exit 0 "preflight secrets dry-run" bash scripts/preflight.sh --phase secrets
expect_exit 2 "preflight bad phase" bash scripts/preflight.sh --phase bogus
expect_exit 0 "create-secrets dry-run" bash scripts/create-secrets.sh
expect_exit 0 "lock-fsxadmin on dry-run" bash scripts/aimf/lock-fsxadmin.sh on
expect_exit 0 "lock-fsxadmin off dry-run" bash scripts/aimf/lock-fsxadmin.sh off
expect_exit 2 "lock-fsxadmin bad action" bash scripts/aimf/lock-fsxadmin.sh sideways
expect_exit 0 "teardown report-only" bash scripts/teardown.sh

# --- stage0-smb.sh: the dry-run must BUILD the real ONTAP REST calls, not just run ----------------
# Asserting the resolved commands appear is what stops a stub (echo-only) from passing review again.
S0_OUT="$(APPMOD_DRY_RUN=1 bash scripts/ontap/stage0-smb.sh \
  --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata 2>&1)"
# DC discovery via the cifs/domains discovered_servers path (not active-directory alone).
check_contains "stage0-smb asserts DC discovery via cifs/domains" \
  "/api/protocols/cifs/domains/" "$S0_OUT"
check_contains "stage0-smb cifs/domains requests discovered_servers" \
  "discovered_servers" "$S0_OUT"
# The real create calls are built.
check_contains "stage0-smb creates the SMB share appdata (POST)" \
  "POST https://203.0.113.5/api/protocols/cifs/shares" "$S0_OUT"
# The file-security and files endpoints take the SVM / volume UUID in the path (the name is
# rejected by ONTAP REST). In dry-run the UUIDs are placeholders.
check_contains "stage0-smb sets NTFS ACLs via file-security permissions (SVM UUID in path)" \
  "/api/protocols/file-security/permissions/<svm-uuid>/%2Fappdata" "$S0_OUT"
check_contains "stage0-smb includes an explicit deny-write ACE for appreader" \
  '"access":"access_deny","user":"APPMOD\\appreader"' "$S0_OUT"
check_contains "stage0-smb creates the seed/ directory (volume UUID in path)" \
  "/api/storage/volumes/<appdata-uuid>/files/seed" "$S0_OUT"
check_contains "stage0-smb creates the probe/ directory (volume UUID in path)" \
  "/api/storage/volumes/<appdata-uuid>/files/probe" "$S0_OUT"
check_contains "stage0-smb creates the out/ directory (volume UUID in path)" \
  "/api/storage/volumes/<appdata-uuid>/files/out" "$S0_OUT"
check_contains "stage0-smb creates the appmod_itclone REST role" \
  "appmod_itclone" "$S0_OUT"
check_contains "stage0-smb creates the appmod_readonly REST role" \
  "appmod_readonly" "$S0_OUT"
# The credential is never shown; no lock is ever enabled.
check_absent "stage0-smb never prints the fsxadmin password" \
  "fsxadmin:\$ONTAP_PW" "$S0_OUT"
check_contains "stage0-smb redacts the credential under dry-run" \
  "fsxadmin:<redacted>" "$S0_OUT"
check_absent "stage0-smb never enables snapshot locking" \
  'snapshot_locking_enabled":true' "$S0_OUT"
check_absent "stage0-smb never touches a snaplock endpoint" \
  "/api/storage/snaplock" "$S0_OUT"
# Argument validation: needs an IP or a file-system id to resolve one.
expect_exit 2 "stage0-smb without mgmt-ip or fs-id" \
  env -u APPMOD_FS_ID APPMOD_DRY_RUN=1 bash scripts/ontap/stage0-smb.sh --svm appmodsvm
# DC-discovery exit 4 path: a response with no ms_dc in state ok makes it stop with exit 4. Feed a
# fixture via a stubbed curl is not available here, so this is covered by the real-run contract in
# design.md; the dry-run asserts the assertion is wired (the GET above). Exit 4 is proven by the
# assert_dc_discovered body reading discovered_servers, exercised live.

# --- run-probe.sh: the dry-run must BUILD send-command, wait, and merge ----------------------------------
# Short placeholder instance ids (not 17-char hex) so the pre-commit secret scan does not read them
# as real EC2 instance ids. The merge logic does not depend on the id format.
RP_WIN="i-win"
RP_LNX="i-lnx"
RP_OUT="$(APPMOD_DRY_RUN=1 bash scripts/run-probe.sh --stage 1 \
  --run-id s1-testUTC --windows-instance "$RP_WIN" \
  --linux-instance "$RP_LNX" --bucket appmod-artifacts-example 2>&1)"
check_contains "run-probe issues aws ssm send-command for the Windows probe" \
  "ssm send-command --instance-ids $RP_WIN" "$RP_OUT"
check_contains "run-probe drives the Windows probe launcher" \
  "probe-launch.ps1 -Stage" "$RP_OUT"
check_contains "run-probe issues aws ssm send-command for the Linux probe" \
  "ssm send-command --instance-ids $RP_LNX" "$RP_OUT"
check_contains "run-probe drives the Linux probe launcher over SMB" \
  "probe-launch.sh --store smb" "$RP_OUT"
check_contains "run-probe adds the NFS probe at stage 1" \
  "probe-launch.sh --store nfs" "$RP_OUT"
check_contains "run-probe uploads probe output to the artifacts bucket" \
  "--output-s3-bucket-name appmod-artifacts-example" "$RP_OUT"
check_contains "run-probe waits for the command before collecting" \
  "ssm wait command-executed" "$RP_OUT"
check_contains "run-probe merges the two sides" \
  "merged 2 behavior(s)" "$RP_OUT"
check_contains "run-probe runs coordinated pairs with a shared sync id and the bucket" \
  "--pair-behavior file-locking --sync-id s1-testUTC-p5 --bucket appmod-artifacts-example" "$RP_OUT"
# With no proven pair, the merge must NOT label the two-client behaviors cross-host.
if python3 - <<'PY'
import json, sys
d = json.load(open(".private/runs/s1-testUTC/merged.json", encoding="utf-8"))
by = {b["id"]: b for b in d["behaviors"]}
ok = all(
    by[k]["observed"]["topology"] == "not-comparable" for k in ("file-locking", "write-visibility")
) and all(b["outcome"] in {"measured", "error", "skipped"} for b in d["behaviors"]) \
  and d["schema"] == "appmod-probe/1"
sys.exit(0 if ok else 1)
PY
then
  echo "ok: run-probe merge does not label unproven two-client behaviors cross-host"
else
  echo "FAIL: run-probe merge labeled an unproven two-client behavior cross-host" >&2
  FAILURES=$((FAILURES + 1))
fi
rm -rf .private/runs/s1-testUTC
# Required arguments are enforced.
expect_exit 2 "run-probe without --bucket" \
  env APPMOD_DRY_RUN=1 bash scripts/run-probe.sh --stage 0 --run-id s0-x \
    --windows-instance i-0a --linux-instance i-0b
expect_exit 2 "run-probe without --windows-instance" \
  env APPMOD_DRY_RUN=1 bash scripts/run-probe.sh --stage 0 --run-id s0-x \
    --linux-instance i-0b --bucket b

# --- record-boundary.sh: the dry-run must BUILD the REST GETs and write captured values -----------
printf '{"top_level": ["out", "probe", "seed"], "files": [{"path": "seed/a.txt", "size": 1, "sha256": "aa"}]}\n' \
  >"$TMP/wininv.json"
RB_OUT="$(APPMOD_DRY_RUN=1 bash scripts/ontap/record-boundary.sh --boundary b0 \
  --run-id s0-rbtest --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata \
  --windows-inventory "$TMP/wininv.json" 2>&1)"
check_contains "record-boundary GETs the ONTAP version" \
  "/api/cluster?fields=version" "$RB_OUT"
check_contains "record-boundary GETs the volume list with snaplock.type" \
  "snapshot_locking_enabled,snaplock.type" "$RB_OUT"
check_contains "record-boundary GETs the snapshot list" \
  "/snapshots?fields=name,create_time" "$RB_OUT"
check_absent "record-boundary never prints the credential" \
  "fsxadmin:\$ONTAP_PW" "$RB_OUT"
# The written record carries real inventory SHA and NO <...> placeholders.
if python3 - <<'PY'
import json, sys
d = json.load(open(".private/runs/s0-rbtest/b0.json", encoding="utf-8"))
raw = json.dumps(d)
ok = ("<" not in raw) \
  and d["inventories"]["windows"]["sha256"] \
  and d["security_style"] == "ntfs" \
  and d["snapshot_locking_enabled"] is False \
  and "boundary" in d
sys.exit(0 if ok else 1)
PY
then
  echo "ok: record-boundary writes an inventory SHA and no <...> placeholders"
else
  echo "FAIL: record-boundary record has placeholders or missing inventory SHA" >&2
  FAILURES=$((FAILURES + 1))
fi
# It feeds check-invariant.py cleanly.
expect_exit 0 "record-boundary output feeds check-invariant.py" \
  python3 scripts/check-invariant.py \
    --baseline .private/runs/s0-rbtest/b0.json --boundary .private/runs/s0-rbtest/b0.json
rm -rf .private/runs/s0-rbtest
expect_exit 2 "record-boundary without --run-id" \
  env APPMOD_DRY_RUN=1 bash scripts/ontap/record-boundary.sh --boundary b0 --mgmt-ip 203.0.113.5

# --- stage1-nfs.sh: the dry-run must BUILD the UUID-keyed REST calls ------------------------------
S1_OUT="$(APPMOD_DRY_RUN=1 bash scripts/ontap/stage1-nfs.sh --client-cidr 192.0.2.0/24 \
  --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata 2>&1)"
check_contains "stage1-nfs looks up appmod_nfs by SVM UUID" \
  "GET https://203.0.113.5/api/protocols/nfs/export-policies?svm.uuid=<svm-uuid>&name=appmod_nfs" "$S1_OUT"
check_contains "stage1-nfs creates export policy appmod_nfs on the SVM UUID" \
  '"name":"appmod_nfs","svm":{"uuid":"<svm-uuid>"}' "$S1_OUT"
check_contains "stage1-nfs export rule: given CIDR, nfs4, sec=sys, superuser none" \
  '{"clients":[{"match":"192.0.2.0/24"}],"protocols":["nfs4"],"ro_rule":["sys"],"rw_rule":["sys"],"superuser":["none"]}' "$S1_OUT"
check_contains "stage1-nfs assigns the policy by volume UUID" \
  'PATCH https://203.0.113.5/api/storage/volumes/<appdata-uuid>?return_timeout=120 -d '"'"'{"nas":{"export_policy":{"name":"appmod_nfs"}}}' "$S1_OUT"
check_contains "stage1-nfs reads unix user appsvc by SVM UUID" \
  "GET https://203.0.113.5/api/name-services/unix-users/<svm-uuid>/appsvc" "$S1_OUT"
check_contains "stage1-nfs creates unix user appsvc uid 10001" \
  '"svm":{"uuid":"<svm-uuid>"},"name":"appsvc","id":10001' "$S1_OUT"
check_contains "stage1-nfs creates unix user appreader uid 10002" \
  '"svm":{"uuid":"<svm-uuid>"},"name":"appreader","id":10002' "$S1_OUT"
check_contains "stage1-nfs reads name mappings by SVM UUID" \
  "GET https://203.0.113.5/api/name-services/name-mappings?svm.uuid=<svm-uuid>" "$S1_OUT"
check_contains "stage1-nfs maps APPMOD\\appsvc -> appsvc (win_unix)" \
  '"direction":"win_unix","index":1,"pattern":"APPMOD\\\\appsvc","replacement":"appsvc"' "$S1_OUT"
check_contains "stage1-nfs maps appreader -> APPMOD\\appreader (unix_win)" \
  '"direction":"unix_win","index":2,"pattern":"appreader","replacement":"APPMOD\\\\appreader"' "$S1_OUT"
check_contains "stage1-nfs asserts the security style" \
  "/api/storage/volumes/<appdata-uuid>?fields=nas.security_style" "$S1_OUT"
check_absent "stage1-nfs never PATCHes a default unix user" '"default_unix_user":' "$S1_OUT"
check_absent "stage1-nfs never PATCHes the security style" '"security_style":' "$S1_OUT"
check_absent "stage1-nfs never prints the credential" "fsxadmin:\$ONTAP_PW" "$S1_OUT"
expect_exit 2 "stage1-nfs without --client-cidr" \
  env APPMOD_DRY_RUN=1 bash scripts/ontap/stage1-nfs.sh --mgmt-ip 203.0.113.5
expect_exit 2 "stage1-nfs with a non-network CIDR" \
  env APPMOD_DRY_RUN=1 bash scripts/ontap/stage1-nfs.sh --client-cidr 192.0.2.1/24 --mgmt-ip 203.0.113.5

# --- integration-clone.sh: the dry-run must BUILD the volume-UUID-keyed REST calls ----------------
IC_CREATE="$(APPMOD_DRY_RUN=1 bash scripts/ontap/integration-clone.sh create --step 3 --mgmt-ip 203.0.113.5 2>&1)"
check_contains "integration-clone snapshot it_3 on appdata by volume UUID" \
  "POST https://203.0.113.5/api/storage/volumes/<appdata-uuid>/snapshots?return_timeout=120 -d '{\"name\":\"it_3\"}'" "$IC_CREATE"
check_contains "integration-clone FlexClone parent is the appdata UUID, junction /appdata_it_3" \
  '"clone":{"is_flexclone":true,"parent_volume":{"uuid":"<appdata-uuid>"},"parent_snapshot":{"name":"it_3"}},"nas":{"path":"/appdata_it_3"}' "$IC_CREATE"
check_absent "integration-clone snapshot has no expiry" "expiry_time" "$IC_CREATE"
check_absent "integration-clone never sets snapshot locking" "snapshot_locking_enabled\":true" "$IC_CREATE"
check_absent "integration-clone never puts the volume name in a path" "/api/storage/volumes/appdata/" "$IC_CREATE"
IC_DELETE="$(APPMOD_DRY_RUN=1 bash scripts/ontap/integration-clone.sh delete --step 3 --mgmt-ip 203.0.113.5 2>&1)"
check_contains "integration-clone deletes the FlexClone by its UUID, bypassing the recovery queue" \
  "DELETE https://203.0.113.5/api/storage/volumes/<appdata_it_3-uuid>?force=true" "$IC_DELETE"
check_contains "integration-clone purges the recovery queue" \
  "POST https://203.0.113.5/api/private/cli/volume/recovery-queue/purge" "$IC_DELETE"
check_contains "integration-clone deletes snapshot it_3 by volume and snapshot UUID" \
  "DELETE https://203.0.113.5/api/storage/volumes/<appdata-uuid>/snapshots/<it_3-uuid>" "$IC_DELETE"
check_before "integration-clone delete order: FlexClone before recovery-queue purge" \
  "/api/storage/volumes/<appdata_it_3-uuid>?force=true" "recovery-queue/purge" "$IC_DELETE"
check_before "integration-clone delete order: recovery-queue purge before snapshot" \
  "recovery-queue/purge" "/snapshots/<it_3-uuid>" "$IC_DELETE"
check_absent "integration-clone never prints a password" "appmod-itclone:\$" "$IC_DELETE"

# --- ONTAP REST mock: the non-dry-run branches of integration-clone.sh and stage1-nfs.sh ----------
# A PATH-mocked curl answers from a per-case route list (method + URL substring, first unused match
# wins, "once" routes are consumed) and logs every request; a PATH-mocked aws answers only the
# secret read. No AWS, ONTAP or network call is made. This proves shell control flow, not ONTAP.
ONTAP_MOCK="$TMP/ontapmock"; mkdir -p "$ONTAP_MOCK"
cat >"$ONTAP_MOCK/curl" <<'MOCK'
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read()
method, out, url = "GET", None, args[-1]
i = 0
while i < len(args):
    if args[i] == "-X": method = args[i + 1]; i += 2; continue
    if args[i] == "-o": out = args[i + 1]; i += 2; continue
    i += 1
path = os.environ["MOCK_CURL_ROUTES"]
routes = json.load(open(path))
status, body, tag = 404, {"error": {"message": "no mock route"}}, "UNMATCHED"
for r in routes:
    if r.get("used") or r["method"] != method or r["path"] not in url:
        continue
    status, body, tag = r.get("status", 200), r.get("body", {}), "matched"
    if r.get("once"):
        r["used"] = True
        json.dump(routes, open(path, "w"))
    break
cred = "stdin" if stdin.startswith("user = ") else "missing"
leak = "yes" if "mock-pw" in " ".join(args) else "no"
with open(os.environ["MOCK_CURL_LOG"], "a") as log:
    log.write(f"{method} {url} {tag} credential={cred} pw-in-argv={leak}\n")
if out:
    json.dump(body, open(out, "w"))
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
# Common routes: SVM and appdata UUID resolution.
ROUTES_BASE='{"method":"GET","path":"/api/svm/svms?name=appmodsvm","body":{"records":[{"uuid":"svm-u"}],"num_records":1}},
{"method":"GET","path":"/api/storage/volumes?name=appdata&svm.name=appmodsvm","body":{"records":[{"uuid":"appdata-u"}],"num_records":1}}'

# (a) sweep meets an orphan it_3 snapshot with no appdata_it_3 clone: exit 0, the snapshot is
# deleted, and no volume is unmounted or deleted. (An absent clone used to parse as uuid=False.)
export MOCK_CURL_ROUTES="$TMP/routes-sweep.json" MOCK_CURL_LOG="$TMP/curl-sweep.log"
cat >"$MOCK_CURL_ROUTES" <<EOF
[$ROUTES_BASE,
{"method":"GET","path":"/api/storage/volumes?svm.uuid=svm-u&name=appdata_it_*","body":{"records":[],"num_records":0}},
{"method":"GET","path":"/api/storage/volumes/appdata-u/snapshots?name=it_*","body":{"records":[{"name":"it_3"}],"num_records":1}},
{"method":"GET","path":"/api/storage/volumes?name=appdata_it_3&svm.uuid=svm-u","body":{"records":[],"num_records":0}},
{"method":"GET","path":"/api/private/cli/volume/recovery-queue","body":{"records":[],"num_records":0}},
{"method":"GET","path":"/api/storage/volumes/appdata-u/snapshots?name=it_3&fields=uuid","once":true,"body":{"records":[{"uuid":"snap-u"}],"num_records":1}},
{"method":"DELETE","path":"/api/storage/volumes/appdata-u/snapshots/snap-u","status":202,"body":{}},
{"method":"GET","path":"/api/storage/volumes/appdata-u/snapshots?name=it_3","body":{"records":[],"num_records":0}}]
EOF
: >"$MOCK_CURL_LOG"
SW_OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/integration-clone.sh sweep --credential fsxadmin \
  --mgmt-ip 203.0.113.5 2>&1)"; SW_RC=$?
if [ "$SW_RC" -eq 0 ] \
  && grep -q "^DELETE https://203.0.113.5/api/storage/volumes/appdata-u/snapshots/snap-u" "$MOCK_CURL_LOG" \
  && ! grep -qE "^(PATCH|DELETE) https://203.0.113.5/api/storage/volumes/[^/?]+\?" "$MOCK_CURL_LOG" \
  && ! grep -q "UNMATCHED" "$MOCK_CURL_LOG" && ! grep -q "pw-in-argv=yes" "$MOCK_CURL_LOG" \
  && ! grep -q "credential=missing" "$MOCK_CURL_LOG"; then
  echo "ok: integration-clone sweep deletes an orphan it_3 snapshot with no clone (exit 0, no volume delete)"
else
  echo "FAIL: integration-clone sweep on an orphan it_3 snapshot (rc=$SW_RC) should exit 0 and delete only the snapshot" >&2
  echo "$SW_OUT" >&2; cat "$MOCK_CURL_LOG" >&2
  FAILURES=$((FAILURES + 1))
fi

# (b) stage1-nfs: existing UNIX users are accepted only when uid AND primary_gid match.
S1_ROUTES_PRE="$ROUTES_BASE,
{\"method\":\"GET\",\"path\":\"/api/storage/volumes/appdata-u?fields=nas.security_style\",\"body\":{\"nas\":{\"security_style\":\"ntfs\"}}},
{\"method\":\"GET\",\"path\":\"/api/protocols/nfs/services/svm-u?fields=enabled\",\"body\":{\"enabled\":true,\"protocol\":{\"v41_enabled\":true}}},
{\"method\":\"GET\",\"path\":\"/api/protocols/nfs/export-policies?svm.uuid=svm-u&name=appmod_nfs\",\"body\":{\"records\":[{\"id\":7}],\"num_records\":1}},
{\"method\":\"GET\",\"path\":\"/api/protocols/nfs/export-policies/7/rules\",\"body\":{\"records\":[{\"clients\":[{\"match\":\"192.0.2.0/24\"}],\"protocols\":[\"nfs4\"],\"ro_rule\":[\"sys\"],\"rw_rule\":[\"sys\"],\"superuser\":[\"none\"]}]}},
{\"method\":\"GET\",\"path\":\"/api/storage/volumes/appdata-u?fields=nas.export_policy.name\",\"body\":{\"nas\":{\"export_policy\":{\"name\":\"appmod_nfs\"}}}}"
S1_ARGS=(--client-cidr 192.0.2.0/24 --mgmt-ip 203.0.113.5 --svm appmodsvm --volume appdata)
export MOCK_CURL_ROUTES="$TMP/routes-s1-gid.json" MOCK_CURL_LOG="$TMP/curl-s1-gid.log"
cat >"$MOCK_CURL_ROUTES" <<EOF
[$S1_ROUTES_PRE,
{"method":"GET","path":"/api/name-services/unix-users/svm-u/appsvc","body":{"name":"appsvc","id":10001,"primary_gid":20000}}]
EOF
: >"$MOCK_CURL_LOG"
G_OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/stage1-nfs.sh "${S1_ARGS[@]}" 2>&1)"; G_RC=$?
if [ "$G_RC" -eq 1 ] && grep -q "primary_gid 20000, not 10001" <<<"$G_OUT" \
  && ! grep -qE "^(POST|PATCH)" "$MOCK_CURL_LOG"; then
  echo "ok: stage1-nfs stops (exit 1) on an existing appsvc with the wrong primary_gid, writing nothing"
else
  echo "FAIL: stage1-nfs with appsvc primary_gid 20000 (rc=$G_RC) should exit 1 without a write" >&2
  echo "$G_OUT" >&2; cat "$MOCK_CURL_LOG" >&2
  FAILURES=$((FAILURES + 1))
fi
export MOCK_CURL_ROUTES="$TMP/routes-s1-ok.json" MOCK_CURL_LOG="$TMP/curl-s1-ok.log"
cat >"$MOCK_CURL_ROUTES" <<EOF
[$S1_ROUTES_PRE,
{"method":"GET","path":"/api/name-services/unix-users/svm-u/appsvc","body":{"name":"appsvc","id":10001,"primary_gid":10001}},
{"method":"GET","path":"/api/name-services/unix-users/svm-u/appreader","body":{"name":"appreader","id":10002,"primary_gid":10002}},
{"method":"GET","path":"/api/name-services/name-mappings?svm.uuid=svm-u","body":{"records":[],"num_records":0}},
{"method":"POST","path":"/api/name-services/name-mappings","status":201,"body":{}},
{"method":"GET","path":"/api/protocols/cifs/services/svm-u?fields=default_unix_user","body":{"default_unix_user":""}},
{"method":"GET","path":"/api/protocols/nfs/services/svm-u?fields=windows","body":{"windows":{}}}]
EOF
: >"$MOCK_CURL_LOG"
K_OUT="$("${ONTAP_ENV[@]}" bash scripts/ontap/stage1-nfs.sh "${S1_ARGS[@]}" 2>&1)"; K_RC=$?
if [ "$K_RC" -eq 0 ] && grep -q "appsvc uid=10001 primary_gid=10001 already present" <<<"$K_OUT" \
  && [ "$(grep -c "^POST https://203.0.113.5/api/name-services/name-mappings " "$MOCK_CURL_LOG")" = "4" ] \
  && ! grep -q "UNMATCHED" "$MOCK_CURL_LOG" && ! grep -q "pw-in-argv=yes" "$MOCK_CURL_LOG"; then
  echo "ok: stage1-nfs accepts matching existing UNIX users and creates the 4 name mappings"
else
  echo "FAIL: stage1-nfs with matching UNIX users (rc=$K_RC) should exit 0 and post 4 name mappings" >&2
  echo "$K_OUT" >&2; cat "$MOCK_CURL_LOG" >&2
  FAILURES=$((FAILURES + 1))
fi
unset MOCK_CURL_ROUTES MOCK_CURL_LOG

# --- teardown.sh: the dry-run --apply must BUILD the real calls in the design order ---------------
# Short placeholder ids (not 17-char hex) so the pre-commit secret scan does not read them as real.
TD_ARGS=(--file-system-id fs-test --linux-instance i-lnx --bucket appmod-artifacts-example
  --windows-role appmod-test-WindowsRole --volume-id fsvol-test)
TD_OUT="$(APPMOD_DRY_RUN=1 bash scripts/teardown.sh --apply "${TD_ARGS[@]}" 2>&1)"
check_contains "teardown starts the Linux EC2" "ec2 start-instances --instance-ids i-lnx" "$TD_OUT"
check_contains "teardown waits for SSM PingStatus" \
  "ssm describe-instance-information --filters Key=InstanceIds,Values=i-lnx" "$TD_OUT"
check_before "teardown: EC2 start precedes check-no-locking" \
  "ec2 start-instances" "bash ./check-no-locking.sh" "$TD_OUT"
check_before "teardown: SSM Online wait precedes check-no-locking" \
  "ssm describe-instance-information" "bash ./check-no-locking.sh" "$TD_OUT"
check_contains "teardown runs check-no-locking on the Linux host via Run Command" \
  "ssm send-command --instance-ids i-lnx --document-name AWS-RunShellScript --comment appmod teardown check-no-locking.sh" "$TD_OUT"
check_before "teardown: check-no-locking precedes the stage3 delete" \
  "bash ./check-no-locking.sh" "delete-stack --stack-name appmod-stage3" "$TD_OUT"
check_before "teardown: recovery-queue verify-clean precedes the appdata delete" \
  "integration-clone.sh verify-clean" "fsx delete-volume --volume-id fsvol-test" "$TD_OUT"
check_contains "teardown deletes appdata (the given volume id) with SkipFinalBackup=true" \
  "fsx delete-volume --volume-id fsvol-test --ontap-configuration SkipFinalBackup=true" "$TD_OUT"
check_before "teardown: delete-role-policy app-users secret read precedes the base stack delete" \
  "delete-role-policy --role-name appmod-test-WindowsRole --policy-name appmod-read-app-users-secret" \
  "delete-stack --stack-name appmod-base" "$TD_OUT"
check_before "teardown: delete-role-policy artifacts bucket access precedes the base stack delete" \
  "delete-role-policy --role-name appmod-test-WindowsRole --policy-name appmod-artifacts-bucket-access" \
  "delete-stack --stack-name appmod-base" "$TD_OUT"
check_before "teardown: bucket emptied before the base stack delete" \
  "s3 rm s3://appmod-artifacts-example --recursive" "delete-stack --stack-name appmod-base" "$TD_OUT"
for s in appmod/ad-admin appmod/fsxadmin appmod/app-users appmod/ontap-itclone; do
  check_contains "teardown force-deletes $s without recovery" \
    "delete-secret --secret-id $s --force-delete-without-recovery" "$TD_OUT"
done
check_contains "teardown enumerates secrets including planned deletion" \
  "list-secrets --include-planned-deletion" "$TD_OUT"
check_absent "teardown never hardcodes a placeholder volume id" "vol-0123456789abcdef0" "$TD_OUT"
check_contains "teardown step 10 checks backups by the given volume id" \
  "fsx describe-backups --filters Name=volume-id,Values=fsvol-test" "$TD_OUT"
check_contains "teardown stages tracked scripts/ontap files one by one" \
  "s3 cp --quiet $REPO_ROOT/scripts/ontap/lib-ontap-rest.sh s3://appmod-artifacts-example/teardown/scripts/ontap/lib-ontap-rest.sh" "$TD_OUT"
check_absent "teardown never stages scripts/ontap recursively" "s3 cp --recursive --quiet $REPO_ROOT/scripts/ontap/" "$TD_OUT"
# Without --volume-id, step 10 still checks backups by the appdata id resolved by enumeration (H4).
TD_NOVOL="$(APPMOD_DRY_RUN=1 bash scripts/teardown.sh --apply --file-system-id fs-test --linux-instance i-lnx \
  --bucket appmod-artifacts-example --windows-role appmod-test-WindowsRole 2>&1)"
check_contains "teardown step 10 checks backups by the enumerated appdata id without --volume-id" \
  "fsx describe-backups --filters Name=volume-id,Values=<appdata-volume-id>" "$TD_NOVOL"
TD_REPORT="$(env -u APPMOD_DRY_RUN bash scripts/teardown.sh 2>&1)"
check_contains "teardown report names the EC2 start + SSM wait step" "- 0 start the Linux EC2" "$TD_REPORT"
check_absent "teardown report-only makes no AWS call" "DRY-RUN:" "$TD_REPORT"
expect_exit 2 "teardown --apply without the required ids" env APPMOD_DRY_RUN=1 bash scripts/teardown.sh --apply
TD_AFC="$(APPMOD_DRY_RUN=1 bash scripts/teardown.sh --after-failed-create --apply --file-system-id fs-test 2>&1)"
check_contains "teardown --after-failed-create checks SnapLock via the FSx for ONTAP API" \
  "SnaplockConfiguration" "$TD_AFC"
check_contains "teardown --after-failed-create deletes appdata with SkipFinalBackup=true" \
  "--ontap-configuration SkipFinalBackup=true" "$TD_AFC"
check_absent "teardown --after-failed-create starts no instance" "start-instances" "$TD_AFC"
# A failed check-no-locking on the host (mocked aws: ResponseCode 3) must stop with exit 3 before
# anything is deleted. The mock answers only what this path asks; no AWS call is made.
MOCK_BIN="$TMP/mockbin"; mkdir -p "$MOCK_BIN"
cat >"$MOCK_BIN/aws" <<'MOCK'
#!/bin/bash
echo "aws $*" >>"$MOCK_AWS_LOG"
case "$*" in
  *"describe-instance-information"*) echo Online ;;
  *"ssm send-command"*) echo cmd-1 ;;
  *"get-command-invocation"*"--query Status "*) echo Failed ;;
  *"get-command-invocation"*"ResponseCode"*) echo 3 ;;
  *) echo "" ;;
esac
MOCK
chmod +x "$MOCK_BIN/aws"
export MOCK_AWS_LOG="$TMP/mock-aws.log"
: >"$MOCK_AWS_LOG"
env -u APPMOD_DRY_RUN PATH="$MOCK_BIN:$PATH" bash scripts/teardown.sh --apply "${TD_ARGS[@]}" >/dev/null 2>&1
TD_LOCK_RC=$?
if [ "$TD_LOCK_RC" -eq 3 ] && ! grep -qE "delete-(stack|volume|secret|role-policy)|s3 rm" "$MOCK_AWS_LOG"; then
  echo "ok: teardown stops with exit 3 on a failed check-no-locking, before any delete"
else
  echo "FAIL: teardown on a failed check-no-locking (rc=$TD_LOCK_RC) should exit 3 with no delete" >&2
  cat "$MOCK_AWS_LOG" >&2
  FAILURES=$((FAILURES + 1))
fi

# --- run-atx.sh: real mode fails closed while unverified; a failed atx does not consume the estimate
ATX_SEND="$TMP/atx-send"; mkdir -p "$ATX_SEND" "$TMP/atx-logs"
new_atx_estimate() {  # new_atx_estimate <name> [transformation] [limit]
  local path="$EST_DIR/$1.json"
  printf '{"target": "atx", "region": "ap-northeast-1", "created_at": "%s", "hours": 0, "parameters": %s}\n' \
    "$CREATED" "$(atx_params_json "${2:-AWS/comprehensive-codebase-analysis}" "${3:-120}")" >"$path"
  python3 - "$APPROVAL" "$path" "$APPROVED" <<'PY'
import json,sys
p,est,approved=sys.argv[1:4]
d=json.load(open(p)); d.append({"target":"atx","approved_at":approved,"estimate_file":est}); json.dump(d,open(p,"w"))
PY
  printf '%s' "$path"
}
cat >"$MOCK_BIN/atx" <<'MOCK'
#!/bin/bash
printf '%s\n' "$*" >"$MOCK_ATX_ARGV"
sleep 1
if [ -z "${MOCK_ATX_NO_LOG:-}" ]; then
  printf '%s\n' '2026-01-01 00:00:00 [DEBUG]: Initializing FrontendServiceClient with config:' \
    '{ "region": "ap-northeast-1", "regionSource": "mock-source-from-atx-log" }' >"$APPMOD_ATX_LOG_DIR/debug1.log"
fi
exit "${MOCK_ATX_RC:-0}"
MOCK
printf '#!/bin/bash\nexit 0\n' >"$MOCK_BIN/gitleaks"
chmod +x "$MOCK_BIN/atx" "$MOCK_BIN/gitleaks"
export MOCK_ATX_ARGV="$TMP/atx-argv"
ATX_ENV=(env -u APPMOD_DRY_RUN PATH="$MOCK_BIN:$PATH" APPMOD_SEND_DIR="$ATX_SEND"
  APPMOD_ATX_LOG_DIR="$TMP/atx-logs" APPMOD_RUN_LOG="$TMP/atx-run.log")
# (1) no verification record -> exit 2, atx never invoked, estimate untouched
EST_U="$(new_atx_estimate atx-unverified)"
rm -f "$MOCK_ATX_ARGV"
U_OUT="$("${ATX_ENV[@]}" APPMOD_ATX_VERIFIED_RECORD="$TMP/none.json" \
  bash scripts/aimf/run-atx.sh --estimate "$EST_U" --approved-at "$APPROVED" 2>&1)"; U_RC=$?
if [ "$U_RC" -eq 2 ] && printf '%s' "$U_OUT" | grep -q "no verification record for" \
  && [ ! -f "$MOCK_ATX_ARGV" ] && [ -f "$EST_U" ]; then
  echo "ok: run-atx real mode fails closed (exit 2) while the invocation is unverified"
else
  echo "FAIL: run-atx real mode should exit 2 unverified without calling atx (rc=$U_RC)" >&2
  echo "$U_OUT" >&2
  FAILURES=$((FAILURES + 1))
fi
# A verification record naming the exact invocation, limit included (what task 2.4 writes).
VREC="$TMP/atx-verified.json"
cat >"$VREC" <<'EOF'
{"invocation": "atx custom def exec -n AWS/comprehensive-codebase-analysis -p . -x -t --limit 120",
 "transformation": "AWS/comprehensive-codebase-analysis", "limit_minutes": 120,
 "region": "ap-northeast-1", "atx_version": "mock", "verified_at": "2026-01-01T00:00:00Z"}
EOF
# (2) atx fails -> script fails, estimate NOT moved to used/
EST_F="$(new_atx_estimate atx-fails)"
rm -f "$MOCK_ATX_ARGV"
"${ATX_ENV[@]}" APPMOD_ATX_VERIFIED_RECORD="$VREC" MOCK_ATX_RC=7 \
  bash scripts/aimf/run-atx.sh --estimate "$EST_F" --approved-at "$APPROVED" >/dev/null 2>&1
F_RC=$?
if [ "$F_RC" -ne 0 ] && [ -f "$EST_F" ] && [ ! -f "$EST_DIR/used/atx-fails.json" ] \
  && [ "$(cat "$MOCK_ATX_ARGV" 2>/dev/null)" = "custom def exec -n AWS/comprehensive-codebase-analysis -p . -x -t --limit 120" ] \
  && grep -q "atx exit=7" "$TMP/atx-run.log"; then
  echo "ok: run-atx fails when atx fails, and the estimate stays out of used/"
else
  echo "FAIL: run-atx with a failing atx (rc=$F_RC) must fail and keep the estimate" >&2
  echo "argv: $(cat "$MOCK_ATX_ARGV" 2>/dev/null)" >&2
  FAILURES=$((FAILURES + 1))
fi
# (3) atx succeeds -> estimate moved; regionSource comes from atx's log, not from AWS_REGION
EST_S="$(new_atx_estimate atx-succeeds)"
"${ATX_ENV[@]}" APPMOD_ATX_VERIFIED_RECORD="$VREC" MOCK_ATX_RC=0 \
  bash scripts/aimf/run-atx.sh --estimate "$EST_S" --approved-at "$APPROVED" >/dev/null 2>&1
S_RC=$?
if [ "$S_RC" -eq 0 ] && [ -f "$EST_DIR/used/atx-succeeds.json" ] \
  && grep -q "regionSource=mock-source-from-atx-log region=ap-northeast-1" "$TMP/atx-run.log" \
  && grep -q "aws_region_env=ap-northeast-1" "$TMP/atx-run.log"; then
  echo "ok: run-atx records regionSource from atx's own log and AWS_REGION separately"
else
  echo "FAIL: run-atx success path (rc=$S_RC) should move the estimate and record regionSource from atx" >&2
  cat "$TMP/atx-run.log" >&2
  FAILURES=$((FAILURES + 1))
fi
# (4) atx succeeds but writes no debug log, and the log directory does not exist (empty array under
# set -u; fatal on bash < 4.4): exit 0, estimate moved, regionSource recorded as unverified.
EST_N="$(new_atx_estimate atx-no-debug-log)"
: >"$TMP/atx-run-nolog.log"
env -u APPMOD_DRY_RUN PATH="$MOCK_BIN:$PATH" APPMOD_SEND_DIR="$ATX_SEND" \
  APPMOD_ATX_LOG_DIR="$TMP/atx-logs-absent" APPMOD_RUN_LOG="$TMP/atx-run-nolog.log" \
  APPMOD_ATX_VERIFIED_RECORD="$VREC" MOCK_ATX_RC=0 MOCK_ATX_NO_LOG=1 \
  bash scripts/aimf/run-atx.sh --estimate "$EST_N" --approved-at "$APPROVED" >/dev/null 2>&1
N_RC=$?
if [ "$N_RC" -eq 0 ] && [ -f "$EST_DIR/used/atx-no-debug-log.json" ] && [ ! -f "$EST_N" ] \
  && grep -q "^regionSource=unverified" "$TMP/atx-run-nolog.log" \
  && grep -q "^atx exit=0" "$TMP/atx-run-nolog.log"; then
  echo "ok: run-atx with no atx debug log exits 0, consumes the estimate, records regionSource=unverified"
else
  echo "FAIL: run-atx success without a debug log (rc=$N_RC) should exit 0, move the estimate, record unverified" >&2
  cat "$TMP/atx-run-nolog.log" >&2
  FAILURES=$((FAILURES + 1))
fi

# --- run-atx.sh: per-invocation record, exit 2 at the limit, and the dotnet send-dir gate ---------
# Temp repositories commit with hooks off and a placeholder identity, so neither the global
# pre-commit hook nor a real identity is involved.
tgit() {
  git -c core.hooksPath=/dev/null -c commit.gpgsign=false -c user.name=test \
    -c user.email=test@example.com "$@"
}
make_committed_copy() {  # make_committed_copy <dir>: a clean git work tree with one commit
  mkdir -p "$1" && printf 'class A {}\n' >"$1/A.cs" \
    && tgit -C "$1" init -q && tgit -C "$1" add -A && tgit -C "$1" commit -q -m init
}
# atx_real_refused <label> <estimate> <message> [VAR=value ...]: exit 2, atx never called, estimate
# kept, and the refusal message is the expected one (so the exit 2 comes from that gate).
atx_real_refused() {
  local label="$1" est="$2" needle="$3" out rc; shift 3
  rm -f "$MOCK_ATX_ARGV"
  out="$("${ATX_ENV[@]}" "$@" bash scripts/aimf/run-atx.sh --estimate "$est" \
    --approved-at "$APPROVED" 2>&1)"; rc=$?
  if [ "$rc" -eq 2 ] && [ ! -f "$MOCK_ATX_ARGV" ] && [ -f "$est" ] \
    && printf '%s' "$out" | grep -qF -- "$needle"; then
    echo "ok: $label"
  else
    echo "FAIL: $label (rc=$rc, atx called: $([ -f "$MOCK_ATX_ARGV" ] && echo yes || echo no))" >&2
    printf '%s\n' "$out" >&2
    FAILURES=$((FAILURES + 1))
  fi
}
DN_SEND="$TMP/atx-dotnet-send"
make_committed_copy "$DN_SEND"
DN_HEAD="$(git -C "$DN_SEND" rev-parse HEAD)"
VREC_DN="$TMP/atx-verified-dotnet.json"
cat >"$VREC_DN" <<'EOF'
{"invocation": "atx custom def exec -n AWS/dotnet-modernization -p . -x -t --limit 300",
 "transformation": "AWS/dotnet-modernization", "limit_minutes": 300,
 "region": "ap-northeast-1", "atx_version": "mock", "verified_at": "2026-01-01T00:00:00Z"}
EOF
# (5) the analysis record does not verify a dotnet-modernization run (per-invocation gate)
EST_X="$(new_atx_estimate atx-record-mismatch AWS/dotnet-modernization 300)"
atx_real_refused "run-atx refuses a dotnet run against the analysis record (per-transformation)" \
  "$EST_X" "no verification record for" APPMOD_ATX_VERIFIED_RECORD="$VREC" APPMOD_SEND_DIR="$DN_SEND"
# (6) a record for --limit 60 does not verify an estimate for 120
VREC60="$TMP/atx-verified-60.json"
cat >"$VREC60" <<'EOF'
{"invocation": "atx custom def exec -n AWS/comprehensive-codebase-analysis -p . -x -t --limit 60",
 "transformation": "AWS/comprehensive-codebase-analysis", "limit_minutes": 60,
 "region": "ap-northeast-1", "atx_version": "mock", "verified_at": "2026-01-01T00:00:00Z"}
EOF
EST_L="$(new_atx_estimate atx-limit-mismatch)"
atx_real_refused "run-atx refuses a record whose limit differs from the estimate" \
  "$EST_L" "limit_minutes 120" APPMOD_ATX_VERIFIED_RECORD="$VREC60"
# (7) atx exits 2 at the limit -> exit 3, the estimate is consumed, the run log says so
EST_R="$(new_atx_estimate atx-limit-reached)"
: >"$TMP/atx-run-limit.log"
R_OUT="$("${ATX_ENV[@]}" APPMOD_RUN_LOG="$TMP/atx-run-limit.log" APPMOD_ATX_VERIFIED_RECORD="$VREC" \
  MOCK_ATX_RC=2 bash scripts/aimf/run-atx.sh --estimate "$EST_R" --approved-at "$APPROVED" 2>&1)"
R_RC=$?
if [ "$R_RC" -eq 3 ] && [ -f "$EST_DIR/used/atx-limit-reached.json" ] && [ ! -f "$EST_R" ] \
  && grep -qF "atx exit=2" "$TMP/atx-run-limit.log" \
  && grep -qF "limit reached, resumable for 24 h, needs a new estimate to raise the limit" "$TMP/atx-run-limit.log" \
  && grep -q "^regionSource=" "$TMP/atx-run-limit.log" \
  && printf '%s' "$R_OUT" | grep -qF "limit reached"; then
  echo "ok: run-atx handles atx exit 2 as limit reached (exit 3, estimate consumed, logged)"
else
  echo "FAIL: run-atx with atx exit 2 (rc=$R_RC) should exit 3, consume the estimate and log the limit" >&2
  printf '%s\n' "$R_OUT" >&2; cat "$TMP/atx-run-limit.log" >&2
  FAILURES=$((FAILURES + 1))
fi
# (7b) a gitleaks finding (mock exit 1) refuses the send with the documented refusal code 2, not 1,
# and atx is never called.
GL_FIND="$MOCK_BIN/gitleaks"
printf '#!/bin/bash\nexit 1\n' >"$GL_FIND"; chmod +x "$GL_FIND"
EST_GL="$(new_atx_estimate atx-gitleaks-find)"
rm -f "$MOCK_ATX_ARGV"
GL_OUT="$("${ATX_ENV[@]}" APPMOD_ATX_VERIFIED_RECORD="$VREC" \
  bash scripts/aimf/run-atx.sh --estimate "$EST_GL" --approved-at "$APPROVED" 2>&1)"; GL_RC=$?
if [ "$GL_RC" -eq 2 ] && [ ! -f "$MOCK_ATX_ARGV" ] && [ -f "$EST_GL" ] \
  && printf '%s' "$GL_OUT" | grep -qF "gitleaks reported a finding or failed to run"; then
  echo "ok: run-atx refuses with exit 2 when gitleaks reports a finding (atx not called)"
else
  echo "FAIL: run-atx with a gitleaks finding (rc=$GL_RC) should exit 2 without calling atx" >&2
  printf '%s\n' "$GL_OUT" >&2
  FAILURES=$((FAILURES + 1))
fi
printf '#!/bin/bash\nexit 0\n' >"$GL_FIND"; chmod +x "$GL_FIND"
# (8) dotnet send dir that is a git repository with no commits
DN_EMPTY="$TMP/atx-dotnet-nocommit"; mkdir -p "$DN_EMPTY"; printf 'x\n' >"$DN_EMPTY/A.cs"
tgit -C "$DN_EMPTY" init -q
EST_E="$(new_atx_estimate atx-dotnet-nocommit AWS/dotnet-modernization 300)"
atx_real_refused "run-atx refuses a commit-less dotnet send dir" \
  "$EST_E" "needs a send directory with commits" APPMOD_ATX_VERIFIED_RECORD="$VREC_DN" \
  APPMOD_SEND_DIR="$DN_EMPTY"
# (9) a committed dotnet send dir with an untracked file
DN_DIRTY="$TMP/atx-dotnet-dirty"
make_committed_copy "$DN_DIRTY"
printf 'y\n' >"$DN_DIRTY/B.cs"
EST_D="$(new_atx_estimate atx-dotnet-dirty AWS/dotnet-modernization 300)"
atx_real_refused "run-atx refuses a dirty dotnet send dir" \
  "$EST_D" "uncommitted or untracked changes" APPMOD_ATX_VERIFIED_RECORD="$VREC_DN" \
  APPMOD_SEND_DIR="$DN_DIRTY"
# (10) the dotnet send dir resolves to the analysis copy (via a symlink). The analysis copy is a
# clean committed repository here, so only the same-path check can refuse it.
AN_SEND="$TMP/atx-analysis-copy"
make_committed_copy "$AN_SEND"
ln -s "$AN_SEND" "$TMP/atx-analysis-link"
EST_A="$(new_atx_estimate atx-dotnet-on-analysis AWS/dotnet-modernization 300)"
atx_real_refused "run-atx refuses dotnet-modernization on the analysis copy" \
  "$EST_A" "must not run on the analysis copy" APPMOD_ATX_VERIFIED_RECORD="$VREC_DN" \
  APPMOD_ANALYSIS_SEND_DIR="$AN_SEND" APPMOD_SEND_DIR="$TMP/atx-analysis-link"
# (11) clean committed dotnet send dir and its own record: atx runs with --limit 300, HEAD recorded
EST_OK="$(new_atx_estimate atx-dotnet-ok AWS/dotnet-modernization 300)"
: >"$TMP/atx-run-dotnet.log"
rm -f "$MOCK_ATX_ARGV"
"${ATX_ENV[@]}" APPMOD_RUN_LOG="$TMP/atx-run-dotnet.log" APPMOD_ATX_VERIFIED_RECORD="$VREC_DN" \
  APPMOD_SEND_DIR="$DN_SEND" APPMOD_ANALYSIS_SEND_DIR="$AN_SEND" MOCK_ATX_RC=0 \
  bash scripts/aimf/run-atx.sh --estimate "$EST_OK" --approved-at "$APPROVED" >/dev/null 2>&1
OK_RC=$?
if [ "$OK_RC" -eq 0 ] && [ -f "$EST_DIR/used/atx-dotnet-ok.json" ] \
  && [ "$(cat "$MOCK_ATX_ARGV" 2>/dev/null)" = "custom def exec -n AWS/dotnet-modernization -p . -x -t --limit 300" ] \
  && grep -qxF "send_head=$DN_HEAD" "$TMP/atx-run-dotnet.log" \
  && grep -qxF "limit_minutes=300" "$TMP/atx-run-dotnet.log" \
  && grep -qxF "transformation=AWS/dotnet-modernization" "$TMP/atx-run-dotnet.log"; then
  echo "ok: run-atx runs dotnet-modernization on a clean committed copy with --limit 300 and records HEAD"
else
  echo "FAIL: run-atx dotnet on a clean committed copy (rc=$OK_RC) should run with --limit 300 and log send_head" >&2
  echo "argv: $(cat "$MOCK_ATX_ARGV" 2>/dev/null)" >&2; cat "$TMP/atx-run-dotnet.log" >&2
  FAILURES=$((FAILURES + 1))
fi
# (12) a dotnet run that stops at the limit (atx exit 2) also prints that resuming it is a human
# decision, since its own clean-tree gate refuses the re-run the limit message otherwise suggests.
# A fresh committed copy is used so the earlier dotnet case's state does not matter.
DN_LIM="$TMP/atx-dotnet-limit-send"
make_committed_copy "$DN_LIM"
EST_RD="$(new_atx_estimate atx-dotnet-limit AWS/dotnet-modernization 300)"
: >"$TMP/atx-run-dnlimit.log"
RD_OUT="$("${ATX_ENV[@]}" APPMOD_RUN_LOG="$TMP/atx-run-dnlimit.log" \
  APPMOD_ATX_VERIFIED_RECORD="$VREC_DN" APPMOD_SEND_DIR="$DN_LIM" \
  APPMOD_ANALYSIS_SEND_DIR="$AN_SEND" MOCK_ATX_RC=2 \
  bash scripts/aimf/run-atx.sh --estimate "$EST_RD" --approved-at "$APPROVED" 2>&1)"
RD_RC=$?
if [ "$RD_RC" -eq 3 ] && [ -f "$EST_DIR/used/atx-dotnet-limit.json" ] \
  && printf '%s' "$RD_OUT" | grep -qF "resuming it is a human decision"; then
  echo "ok: run-atx tells the operator that resuming a limit-stopped dotnet run is a human decision"
else
  echo "FAIL: run-atx dotnet limit stop (rc=$RD_RC) should exit 3 and flag resume as a human decision" >&2
  printf '%s\n' "$RD_OUT" >&2
  FAILURES=$((FAILURES + 1))
fi

# (12) default paths: with neither APPMOD_ATX_VERIFIED_RECORD nor APPMOD_SEND_DIR set, the dry-run
# gate names the slug-based default record and the default send dir, so a regression in those
# defaults is caught. Dry-run so no real send dir is required; cwd is a temp dir so a stray
# .private/ cannot interfere. The verification gate only reports under dry-run, so the absence of a
# real record is fine.
# run-atx.sh resolves the default send dirs relative to cwd, so the gate runs in a temp cwd that
# holds the default .private/aimf layout: an analysis dir (no commits needed) and a committed dotnet
# dir. The repo-relative estimate/record paths still need the real repo, so they are passed absolute.
# (13) default paths. run-atx.sh resolves the default record and send-dir paths relative to cwd, so the gate runs in a
# temp cwd that holds the default .private layout: a verification record at the slug-based default
# path (so the "verified by" line prints that path), an analysis send dir (no commits needed), and a
# committed dotnet send dir. Neither APPMOD_ATX_VERIFIED_RECORD nor APPMOD_SEND_DIR is set, so this
# is the only case that exercises both defaults (review finding 4). The estimate path is absolute.
DEF_CWD="$TMP/atx-defaults"
mkdir -p "$DEF_CWD/.private/aimf/DocIntake" "$DEF_CWD/.private/runs"
make_committed_copy "$DEF_CWD/.private/aimf/DocIntake-atx-dotnet"
REPO_DIR="$(pwd)"
def_record() {  # def_record <slug> <transformation> <limit>
  cat >"$DEF_CWD/.private/runs/atx-invocation-verified-$1.json" <<EOF
{"invocation": "atx custom def exec -n $2 -p . -x -t --limit $3",
 "transformation": "$2", "limit_minutes": $3,
 "region": "ap-northeast-1", "atx_version": "mock", "verified_at": "2026-01-01T00:00:00Z"}
EOF
}
def_record comprehensive-codebase-analysis AWS/comprehensive-codebase-analysis 120
def_record dotnet-modernization AWS/dotnet-modernization 120
def_dryrun() {  # def_dryrun <label> <transformation> <slug> <send-dir-substr>
  local label="$1" tx="$2" slug="$3" send="$4" out rc est
  est="$EST_DIR/def-$slug.json"
  printf '{"target": "atx", "region": "ap-northeast-1", "created_at": "%s", "hours": 0, "parameters": %s}\n' \
    "$CREATED" "$(atx_params_json "$tx" "120")" >"$est"
  python3 - "$APPROVAL" "$est" "$APPROVED" <<'PY'
import json,sys
p,est,approved=sys.argv[1:4]
d=json.load(open(p)); d.append({"target":"atx","approved_at":approved,"estimate_file":est}); json.dump(d,open(p,"w"))
PY
  out="$(cd "$DEF_CWD" && env -u APPMOD_SEND_DIR -u APPMOD_ATX_VERIFIED_RECORD APPMOD_DRY_RUN=1 \
    bash "$REPO_DIR/scripts/aimf/run-atx.sh" --estimate "$est" --approved-at "$APPROVED" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] \
    && printf '%s' "$out" | grep -qF "atx-invocation-verified-$slug.json" \
    && printf '%s' "$out" | grep -qF "$send"; then
    echo "ok: $label"
  else
    echo "FAIL: $label (rc=$rc)" >&2; printf '%s\n' "$out" >&2
    FAILURES=$((FAILURES + 1))
  fi
}
def_dryrun "run-atx dry-run names the default analysis record and send dir" \
  AWS/comprehensive-codebase-analysis comprehensive-codebase-analysis ".private/aimf/DocIntake"
def_dryrun "run-atx dry-run names the default dotnet record and send dir" \
  AWS/dotnet-modernization dotnet-modernization ".private/aimf/DocIntake-atx-dotnet"

echo "----"
if [ "$FAILURES" -ne 0 ]; then
  echo "dryrun_shell_tests: $FAILURES failure(s)" >&2
  exit 1
fi
echo "dryrun_shell_tests: all cases passed"
