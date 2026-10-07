#!/usr/bin/env bash
#
# Create a stack (base or stage3) only against a fresh, unused, approved estimate. The entry check
# is shared with run-atx.sh via scripts/lib/entry-check.sh. On success the estimate is moved to
# estimates/used/, so the same estimate cannot drive a second operation; a rebuild or re-run needs a
# new estimate and approval.
#
#   deploy.sh <base|stage3> --estimate <file> --approved-at <ISO 8601>
#
# AWS CLI is always invoked with --region ap-northeast-1. When APPMOD_DRY_RUN is set, the create and
# the move are printed instead of run, and no AWS call is made (so the six entry-check cases in the
# test plan run without credentials).
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/entry-check.sh
. "$HERE/lib/entry-check.sh"

REGION="ap-northeast-1"
DRY_RUN="${APPMOD_DRY_RUN:-}"
ESTIMATES_DIR="${APPMOD_ESTIMATES_DIR:-.private/estimates}"

TARGET=""
ESTIMATE=""
APPROVED_AT=""

usage() {
  echo "usage: deploy.sh <base|stage3> --estimate <file> --approved-at <ISO 8601>" >&2
}

if [ $# -lt 1 ]; then usage; exit 2; fi
TARGET="$1"; shift
case "$TARGET" in
  base|stage3) ;;
  *) echo "deploy: target must be base or stage3" >&2; usage; exit 2 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --estimate) ESTIMATE="${2:-}"; shift 2 ;;
    --approved-at) APPROVED_AT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "deploy: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# target base -> estimate target base; target stage3 -> estimate target stage3.
if ! appmod_entry_check "$ESTIMATE" "$APPROVED_AT" "$TARGET"; then
  echo "deploy: entry check failed; not deploying" >&2
  exit 2
fi

run_aws() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION $*"
    return 0
  fi
  aws --region "$REGION" "$@"
}

# The approved estimate is the parameter source of truth: deploy.sh deploys exactly the
# CloudFormation parameters recorded under "parameters" in the estimate, so what a human approved
# and what create-stack receives cannot diverge. An estimate without a parameters object (an older
# estimate written before deploy-time parameter passing) is refused rather than deployed with
# template defaults, because the base.yaml CIDR defaults collide with existing VPCs in the shared
# account. Prints one "ParameterKey=...,ParameterValue=..." token per recorded parameter.
read_estimate_parameters() {
  local estimate_file="$1"
  APPMOD_DP_ESTIMATE="$estimate_file" python3 - <<'PY'
import json
import os
import sys

path = os.environ["APPMOD_DP_ESTIMATE"]
try:
    estimate = json.loads(open(path, encoding="utf-8").read())
except (OSError, json.JSONDecodeError) as exc:
    print(f"deploy: estimate is not readable JSON: {exc}", file=sys.stderr)
    sys.exit(2)

parameters = estimate.get("parameters")
if not isinstance(parameters, list) or not parameters:
    print(
        "deploy: estimate has no 'parameters' object; refusing to deploy with template "
        "defaults (re-run estimate.py to record the approved parameters)",
        file=sys.stderr,
    )
    sys.exit(2)

for entry in parameters:
    key = entry.get("ParameterKey")
    value = entry.get("ParameterValue")
    if not key or value is None:
        print(f"deploy: malformed parameter entry: {entry!r}", file=sys.stderr)
        sys.exit(2)
    # One token per line; the caller reads them into an array. CloudFormation's shorthand syntax
    # is ParameterKey=<key>,ParameterValue=<value>.
    print(f"ParameterKey={key},ParameterValue={value}")
PY
}

case "$TARGET" in
  base)
    STACK="appmod-base"
    TEMPLATE="templates/base.yaml"
    ;;
  stage3)
    STACK="appmod-stage3"
    TEMPLATE="templates/stage3-serverless.yaml"
    ;;
esac

# Resolve the approved CloudFormation parameters from the estimate before the create call. A
# missing parameters object exits 2 here (read_estimate_parameters), so an older estimate never
# reaches create-stack. One array element per recorded parameter.
PARAMETERS=()
while IFS= read -r token; do
  [ -n "$token" ] && PARAMETERS+=("$token")
done < <(read_estimate_parameters "$ESTIMATE")
if [ "${#PARAMETERS[@]}" -eq 0 ]; then
  echo "deploy: no deployable parameters resolved from estimate; not deploying" >&2
  exit 2
fi

echo "deploy: creating $STACK from $TEMPLATE in $REGION"
# --on-failure DELETE so a failed base create does not leave half a stack billing; appdata is
# DeletionPolicy Retain, so teardown.sh --after-failed-create handles a retained volume afterwards.
run_aws cloudformation create-stack \
  --stack-name "$STACK" \
  --template-body "file://$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM \
  --on-failure DELETE \
  --parameters "${PARAMETERS[@]}"

# Move the estimate to used/ so it cannot be reused.
mkdir -p "$ESTIMATES_DIR/used"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: mv $ESTIMATE $ESTIMATES_DIR/used/"
else
  mv "$ESTIMATE" "$ESTIMATES_DIR/used/"
fi
echo "deploy: $STACK create submitted; estimate moved to used/"
