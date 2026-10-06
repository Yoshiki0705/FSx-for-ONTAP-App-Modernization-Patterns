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

echo "deploy: creating $STACK from $TEMPLATE in $REGION"
# --on-failure DELETE so a failed base create does not leave half a stack billing; appdata is
# DeletionPolicy Retain, so teardown.sh --after-failed-create handles a retained volume afterwards.
run_aws cloudformation create-stack \
  --stack-name "$STACK" \
  --template-body "file://$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM \
  --on-failure DELETE

# Move the estimate to used/ so it cannot be reused.
mkdir -p "$ESTIMATES_DIR/used"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: mv $ESTIMATE $ESTIMATES_DIR/used/"
else
  mv "$ESTIMATE" "$ESTIMATES_DIR/used/"
fi
echo "deploy: $STACK create submitted; estimate moved to used/"
