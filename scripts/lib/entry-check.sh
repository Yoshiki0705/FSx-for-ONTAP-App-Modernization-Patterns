#!/usr/bin/env bash
#
# Shared entry check for the two scripts that perform a billed, approved operation: deploy.sh and
# scripts/aimf/run-atx.sh. One implementation, so the two cannot drift.
#
# appmod_entry_check <estimate_file> <approved_at> <expected_target>
#
# Fails (returns 2) when any of these holds:
#   - the estimate file does not exist
#   - the estimate was created more than 24 hours ago
#   - the estimate has already been used (a file of the same name under estimates/used/)
#   - approved_at is empty, or earlier than the estimate's created_at
#   - the estimate target does not equal the expected target
#   - the estimate region is not ap-northeast-1
#   - approval.json has no record matching this estimate file and approved_at
#
# It does NOT move the estimate to used/; the caller does that after the operation succeeds, so a
# failed operation can be retried against the same approval. Reading only; no AWS call.
#
# Requires: python3 (for JSON and time arithmetic; the repo is stdlib-only and ships no jq
# dependency). ESTIMATES_DIR and APPROVAL_FILE may be overridden by the environment for tests.

appmod_entry_check() {
  local estimate_file="$1" approved_at="$2" expected_target="$3"
  local estimates_dir="${APPMOD_ESTIMATES_DIR:-.private/estimates}"
  local approval_file="${APPMOD_APPROVAL_FILE:-.private/runs/approval.json}"

  if [ -z "$estimate_file" ] || [ ! -f "$estimate_file" ]; then
    echo "entry-check: estimate file not found: ${estimate_file:-<none>}" >&2
    return 2
  fi
  if [ -z "$approved_at" ]; then
    echo "entry-check: --approved-at is required" >&2
    return 2
  fi

  local used_file
  used_file="$estimates_dir/used/$(basename "$estimate_file")"
  if [ -f "$used_file" ]; then
    echo "entry-check: estimate already used: $used_file" >&2
    return 2
  fi

  APPMOD_EC_ESTIMATE="$estimate_file" \
  APPMOD_EC_APPROVED_AT="$approved_at" \
  APPMOD_EC_TARGET="$expected_target" \
  APPMOD_EC_APPROVAL="$approval_file" \
  python3 - <<'PY'
import datetime as dt
import json
import os
import sys

estimate_path = os.environ["APPMOD_EC_ESTIMATE"]
approved_at = os.environ["APPMOD_EC_APPROVED_AT"]
expected_target = os.environ["APPMOD_EC_TARGET"]
approval_path = os.environ["APPMOD_EC_APPROVAL"]


def parse_iso(value):
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def fail(message):
    print(f"entry-check: {message}", file=sys.stderr)
    sys.exit(2)


try:
    estimate = json.loads(open(estimate_path, encoding="utf-8").read())
except (OSError, json.JSONDecodeError) as exc:
    fail(f"estimate is not readable JSON: {exc}")

if estimate.get("target") != expected_target:
    fail(f"estimate target {estimate.get('target')!r} != expected {expected_target!r}")

if estimate.get("region") != "ap-northeast-1":
    fail(f"estimate region {estimate.get('region')!r} is not ap-northeast-1")

try:
    created = parse_iso(estimate["created_at"])
except (KeyError, ValueError) as exc:
    fail(f"estimate has no valid created_at: {exc}")

now = dt.datetime.now(dt.timezone.utc)
age_hours = (now - created).total_seconds() / 3600.0
if age_hours > 24:
    fail(f"estimate is {age_hours:.1f}h old (older than 24h); re-estimate")

try:
    approved = parse_iso(approved_at)
except ValueError as exc:
    fail(f"--approved-at is not an ISO 8601 time: {exc}")

if approved < created:
    fail("--approved-at is earlier than the estimate's created_at")

try:
    approvals = json.loads(open(approval_path, encoding="utf-8").read())
except (OSError, json.JSONDecodeError) as exc:
    fail(f"approval.json is not readable: {exc}")

if not isinstance(approvals, list):
    fail("approval.json must be a JSON array")

estimate_name = os.path.basename(estimate_path)
match = any(
    a.get("estimate_file") and os.path.basename(a["estimate_file"]) == estimate_name
    and a.get("approved_at", "").strip() == approved_at.strip()
    and a.get("target") == expected_target
    for a in approvals
)
if not match:
    fail("approval.json has no record matching this estimate file + approved_at + target")

print("entry-check: estimate is fresh, unused, approved, in-region, and target-matched")
PY
}
