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

# The two Secrets Manager secrets the base stack resolves at create time, with their template
# defaults. Source of truth: templates/base.yaml Parameters.AdAdminSecretName.Default and
# Parameters.FsxAdminSecretName.Default. A defaults-parity test in scripts/tests/dryrun_shell_tests.sh
# fails the moment either constant diverges from base.yaml, so this second copy cannot rot unnoticed.
# These two only: appmod/app-users and appmod/ontap-itclone are consumed by later ONTAP scripts, not
# by the base stack, so checking them here would reject a valid base deploy.
AD_ADMIN_SECRET_DEFAULT="appmod/ad-admin"
FSX_ADMIN_SECRET_DEFAULT="appmod/fsxadmin"

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

# Resolve the account id for the deploy-bucket name. Under dry-run this is a fixed placeholder, so
# the six entry-check cases run without credentials and the printed --template-url is well-formed;
# in real mode it is a real sts call through run_aws.
resolve_account_id() {
  if [ -n "$DRY_RUN" ]; then
    echo "123456789012"
    return 0
  fi
  run_aws sts get-caller-identity --query Account --output text
}

# The approved estimate is the parameter source of truth for the base stack: deploy.sh base deploys
# exactly the CloudFormation parameters recorded under "parameters" in the estimate, so what a human
# approved and what create-stack receives cannot diverge. A base estimate without a parameters
# object (an older estimate written before deploy-time parameter passing) is refused rather than
# deployed with template defaults, because the base.yaml CIDR defaults collide with existing VPCs in
# the shared account. Prints one "ParameterKey=...,ParameterValue=..." token per recorded parameter.
#
# stage3 is a separate case: its required parameters (PrimarySubnetId, LambdaSecurityGroupId, VpcId)
# are resource IDs that only exist once the base stack has been created, and the Lambda package key
# and handler are decided in a later task (5.1 / 5.2). estimate.py does not record a stage3
# parameters object, and deploy.sh does not resolve or require one for stage3 — stage3 deploy-time
# parameter passing is deferred to that later task, not refused here.
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

# Resolve the value of a secret parameter: the estimate's recorded value if it overrode the default,
# else the base.yaml default. Scans the already-built PARAMETERS array for a
# "ParameterKey=<key>,ParameterValue=<value>" token. A name and an ARN both work as --secret-id, so
# the resolved value is passed through unchanged.
resolve_secret_value() {
  local default="$2" token prefix="ParameterKey=$1,ParameterValue="
  for token in "${PARAMETERS[@]}"; do
    case "$token" in
      "$prefix"*)
        printf '%s' "${token#"$prefix"}"
        return 0
        ;;
    esac
  done
  printf '%s' "$default"
}

# Before the upload and create-stack, confirm the two secrets the base stack resolves at create time
# exist. This closes the 2026-10-11 incident: a base run submitted without create-secrets.sh had
# CloudFormation create resources, the Directory fail with "Secrets Manager can't find the specified
# secret", and the whole stack roll back via --on-failure DELETE — consuming the approved estimate on
# submission. The precheck fails while the estimate is still unused, so a re-run after
# create-secrets.sh reuses it. Under dry-run run_aws prints and returns 0 (treated as present), so no
# real AWS call is made and existing dry-run tests still reach the upload/create/mv prints.
precheck_base_secrets() {
  local ad_secret fsx_secret missing=()
  ad_secret="$(resolve_secret_value AdAdminSecretName "$AD_ADMIN_SECRET_DEFAULT")"
  fsx_secret="$(resolve_secret_value FsxAdminSecretName "$FSX_ADMIN_SECRET_DEFAULT")"
  local secret
  for secret in "$ad_secret" "$fsx_secret"; do
    if [ -n "$DRY_RUN" ]; then
      # run_aws prints a "DRY-RUN: aws ..." line and returns 0; treat as present, no real call.
      run_aws secretsmanager describe-secret --secret-id "$secret"
    elif ! run_aws secretsmanager describe-secret --secret-id "$secret" >/dev/null 2>&1; then
      missing+=("$secret")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    for secret in "${missing[@]}"; do
      echo "deploy: required secret not found: $secret" >&2
    done
    echo "deploy: run scripts/create-secrets.sh to create the base-stack secrets before deploying" >&2
    exit 2
  fi
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

# Resolve the approved CloudFormation parameters from the estimate before the create call. This is
# base-only: for base, a missing parameters object exits 2 here (read_estimate_parameters), so an
# older estimate never reaches create-stack with template defaults. For stage3 the parameters come
# from the base stack's resource IDs and a later task, so none are resolved from the estimate (see
# the comment on read_estimate_parameters); the array stays empty and no --parameters is passed.
PARAMETERS=()
if [ "$TARGET" = "base" ]; then
  # Read into a variable first and check the status: a process substitution's exit status is never
  # checked, so a reader that printed some tokens and then refused a malformed entry (exit 2) would
  # otherwise deploy with the tokens printed so far and template defaults for the rest.
  if ! TOKENS="$(read_estimate_parameters "$ESTIMATE")"; then
    echo "deploy: the estimate's parameters were refused; not deploying" >&2
    exit 2
  fi
  while IFS= read -r token; do
    [ -n "$token" ] && PARAMETERS+=("$token")
  done <<EOF
$TOKENS
EOF
  if [ "${#PARAMETERS[@]}" -eq 0 ]; then
    echo "deploy: no deployable parameters resolved from estimate; not deploying" >&2
    exit 2
  fi
  # The PARAMETERS array is now fully built, so an estimate that overrode AdAdminSecretName /
  # FsxAdminSecretName is honored. Verify the two base-stack secrets exist before any upload or
  # create; a missing secret exits 2 here with the estimate still unused.
  precheck_base_secrets
fi

# How the template bytes reach create-stack. base.yaml (52,466 bytes) is over CloudFormation's
# 51,200-byte inline --template-body limit, so a base create with --template-body fails with a
# ValidationError before creating anything. base uploads the template to a dedicated, in-region
# deploy bucket and passes --template-url. stage3-serverless.yaml (9,542 bytes) is under the limit
# and stays on --template-body unchanged. The deploy bucket is distinct from the stack's
# ArtifactsBucket (which the base stack creates, so it cannot hold the template before the stack
# exists) and is left in place across rebuilds; teardown.sh does not touch it.
TEMPLATE_ARG=(--template-body "file://$TEMPLATE")
if [ "$TARGET" = "base" ]; then
  ACCOUNT_ID="$(resolve_account_id)"
  DEPLOY_BUCKET="appmod-deploy-${ACCOUNT_ID}-ap-northeast-1"
  KEY="base/$(date -u +%Y%m%dT%H%M%SZ)-base.yaml"
  # Ensure the deploy bucket exists (idempotent). Under dry-run both the head and the create are
  # printed; in real mode the create runs only when the head misses, and BucketAlreadyOwnedByYou is
  # treated as success. run_aws prepends --region; create-bucket also needs the explicit
  # LocationConstraint to place the bucket in ap-northeast-1.
  if [ -n "$DRY_RUN" ]; then
    run_aws s3api head-bucket --bucket "$DEPLOY_BUCKET"
    run_aws s3api create-bucket --bucket "$DEPLOY_BUCKET" --region ap-northeast-1 \
      --create-bucket-configuration LocationConstraint=ap-northeast-1
  elif ! run_aws s3api head-bucket --bucket "$DEPLOY_BUCKET" 2>/dev/null; then
    bucket_err="$(mktemp)"
    if ! run_aws s3api create-bucket --bucket "$DEPLOY_BUCKET" --region ap-northeast-1 \
        --create-bucket-configuration LocationConstraint=ap-northeast-1 2>"$bucket_err"; then
      if ! grep -q "BucketAlreadyOwnedByYou" "$bucket_err"; then
        cat "$bucket_err" >&2
        rm -f "$bucket_err"
        echo "deploy: could not ensure the deploy bucket $DEPLOY_BUCKET; not deploying" >&2
        exit 1
      fi
    fi
    rm -f "$bucket_err"
  fi
  run_aws s3 cp "$TEMPLATE" "s3://$DEPLOY_BUCKET/$KEY"
  echo "deploy: base template uploaded to s3://$DEPLOY_BUCKET/$KEY"
  TEMPLATE_ARG=(--template-url "https://s3.ap-northeast-1.amazonaws.com/$DEPLOY_BUCKET/$KEY")
fi

echo "deploy: creating $STACK from $TEMPLATE in $REGION"
# --on-failure DELETE so a failed base create does not leave half a stack billing; appdata is
# DeletionPolicy Retain, so teardown.sh --after-failed-create handles a retained volume afterwards.
# base passes --parameters from the approved estimate; stage3 passes none (deferred).
if [ "${#PARAMETERS[@]}" -gt 0 ]; then
  run_aws cloudformation create-stack \
    --stack-name "$STACK" \
    "${TEMPLATE_ARG[@]}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --on-failure DELETE \
    --parameters "${PARAMETERS[@]}"
else
  run_aws cloudformation create-stack \
    --stack-name "$STACK" \
    "${TEMPLATE_ARG[@]}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --on-failure DELETE
fi

# Move the estimate to used/ so it cannot be reused.
mkdir -p "$ESTIMATES_DIR/used"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: mv $ESTIMATE $ESTIMATES_DIR/used/"
else
  mv "$ESTIMATE" "$ESTIMATES_DIR/used/"
fi
echo "deploy: $STACK create submitted; estimate moved to used/"
