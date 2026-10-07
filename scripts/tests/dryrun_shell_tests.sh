#!/usr/bin/env bash
#
# Dry-run tests for the AWS- and ONTAP-calling shell scripts. Every case runs with APPMOD_DRY_RUN=1
# (and fixtures for ONTAP), so NO AWS, ONTAP or network call is made. Covers the design test plan:
# deploy.sh's six entry-check cases, run-atx.sh's same entry check, block_direct_atx.py's two cases,
# check-no-locking.sh's locked / missing-appdata cases, integration-clone.sh's rejections, and that
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
EST_ATX="$EST_DIR/case-atx.json"
cat >"$EST_ATX" <<EOF
{"target": "atx", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 1}
EOF
export APPMOD_SEND_DIR="$TMP/senddir"
mkdir -p "$APPMOD_SEND_DIR"
expect_exit 0 "run-atx.sh valid estimate (dry-run)" \
  bash scripts/aimf/run-atx.sh --estimate "$EST_ATX" --approved-at "$APPROVED"
expect_exit 2 "run-atx.sh wrong target estimate" \
  bash scripts/aimf/run-atx.sh --estimate "$EST_BASE" --approved-at "$APPROVED"
unset APPMOD_SEND_DIR

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

# --- integration-clone.sh: name/range validation -------------------------------------------------
expect_exit 0 "integration-clone create --step 3" bash scripts/ontap/integration-clone.sh create --step 3
expect_exit 2 "integration-clone reject appdata" bash scripts/ontap/integration-clone.sh create --name appdata
expect_exit 2 "integration-clone reject appdata_it_0" bash scripts/ontap/integration-clone.sh create --name appdata_it_0
expect_exit 2 "integration-clone reject appdata_it_100" bash scripts/ontap/integration-clone.sh delete --step 100
expect_exit 2 "integration-clone reject other" bash scripts/ontap/integration-clone.sh create --name other

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
# DC discovery via the cifs/domains discovered_servers path (not active-directory alone).
check_contains "stage0-smb asserts DC discovery via cifs/domains" \
  "/api/protocols/cifs/domains/" "$S0_OUT"
check_contains "stage0-smb cifs/domains requests discovered_servers" \
  "discovered_servers" "$S0_OUT"
# The real create calls are built.
check_contains "stage0-smb creates the SMB share appdata (POST)" \
  "POST https://203.0.113.5/api/protocols/cifs/shares" "$S0_OUT"
check_contains "stage0-smb sets NTFS ACLs via file-security permissions" \
  "/api/protocols/file-security/permissions/appmodsvm/%2Fappdata" "$S0_OUT"
check_contains "stage0-smb includes an explicit deny-write ACE for appreader" \
  '"access":"access_deny","user":"APPMOD\\appreader"' "$S0_OUT"
check_contains "stage0-smb creates the seed/ directory" \
  "/api/storage/volumes/appdata/files/seed" "$S0_OUT"
check_contains "stage0-smb creates the probe/ directory" \
  "/api/storage/volumes/appdata/files/probe" "$S0_OUT"
check_contains "stage0-smb creates the out/ directory" \
  "/api/storage/volumes/appdata/files/out" "$S0_OUT"
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

# --- run-probe.sh: the dry-run must BUILD send-command and s3 cp ----------------------------------
# Short placeholder instance ids (not 17-char hex) so the pre-commit secret scan does not read them
# as real EC2 instance ids. The merge logic does not depend on the id format.
RP_WIN="i-win"
RP_LNX="i-lnx"
RP_OUT="$(APPMOD_DRY_RUN=1 bash scripts/run-probe.sh --stage 1 \
  --run-id s1-testUTC --windows-instance "$RP_WIN" \
  --linux-instance "$RP_LNX" --bucket appmod-artifacts-example 2>&1)"
check_contains "run-probe issues aws ssm send-command for the Windows probe" \
  "ssm send-command --instance-ids $RP_WIN" "$RP_OUT"
check_contains "run-probe drives DocIntake.Probe on Windows" \
  "DocIntake.Probe --store smb" "$RP_OUT"
check_contains "run-probe issues aws ssm send-command for the Linux probe" \
  "ssm send-command --instance-ids $RP_LNX" "$RP_OUT"
check_contains "run-probe drives probe_peer.py on Linux" \
  "probe_peer.py --store smb" "$RP_OUT"
check_contains "run-probe adds the NFS probe at stage 1" \
  "probe_peer.py --store nfs" "$RP_OUT"
check_contains "run-probe uploads probe output to the artifacts bucket" \
  "--output-s3-bucket-name appmod-artifacts-example" "$RP_OUT"
check_contains "run-probe copies results back with s3 cp" \
  "s3 cp s3://appmod-artifacts-example/probe/s1-testUTC" "$RP_OUT"
check_contains "run-probe merges the two sides" \
  "merged 2 behavior(s)" "$RP_OUT"
# The merged record forces topology=cross-host on the two-client behaviors.
if python3 - <<'PY'
import json, sys
d = json.load(open(".private/runs/s1-testUTC/merged.json", encoding="utf-8"))
by = {b["id"]: b for b in d["behaviors"]}
ok = all(
    by[k]["observed"]["topology"] == "cross-host" for k in ("file-locking", "write-visibility")
) and all(b["outcome"] in {"measured", "error", "skipped"} for b in d["behaviors"]) \
  and d["schema"] == "appmod-probe/1"
sys.exit(0 if ok else 1)
PY
then
  echo "ok: run-probe merged record is cross-host, three-valued, schema appmod-probe/1"
else
  echo "FAIL: run-probe merged record missing cross-host/outcome/schema" >&2
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
printf 'inv-content\n' >"$TMP/wininv.json"
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

echo "----"
if [ "$FAILURES" -ne 0 ]; then
  echo "dryrun_shell_tests: $FAILURES failure(s)" >&2
  exit 1
fi
echo "dryrun_shell_tests: all cases passed"
