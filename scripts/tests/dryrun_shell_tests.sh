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
EST_BASE="$EST_DIR/20260101T000000Z-base.json"
cat >"$EST_BASE" <<EOF
{"target": "base", "region": "ap-northeast-1", "created_at": "$CREATED", "hours": 72}
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
expect_exit 0 "preflight network dry-run" bash scripts/preflight.sh --phase network
expect_exit 0 "preflight secrets dry-run" bash scripts/preflight.sh --phase secrets
expect_exit 2 "preflight bad phase" bash scripts/preflight.sh --phase bogus
expect_exit 0 "create-secrets dry-run" bash scripts/create-secrets.sh
expect_exit 0 "lock-fsxadmin on dry-run" bash scripts/aimf/lock-fsxadmin.sh on
expect_exit 0 "lock-fsxadmin off dry-run" bash scripts/aimf/lock-fsxadmin.sh off
expect_exit 2 "lock-fsxadmin bad action" bash scripts/aimf/lock-fsxadmin.sh sideways
expect_exit 0 "teardown report-only" bash scripts/teardown.sh

echo "----"
if [ "$FAILURES" -ne 0 ]; then
  echo "dryrun_shell_tests: $FAILURES failure(s)" >&2
  exit 1
fi
echo "dryrun_shell_tests: all cases passed"
