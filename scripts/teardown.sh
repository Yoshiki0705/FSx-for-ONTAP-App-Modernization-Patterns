#!/usr/bin/env bash
#
# Tear down the environment. Default is report-only: it prints the deletion order and what it would
# remove. --apply runs the deletion in the design's fixed order (1-11). --after-failed-create takes
# the path where the file system exists but there is no Linux EC2 to run check-no-locking.sh.
#
#   teardown.sh                      report only (default)
#   teardown.sh --apply              run the deletion order 1-11
#   teardown.sh --after-failed-create [--apply]
#
# Deletion order (design "削除の順序"):
#   1  check-no-locking.sh: all volumes free of snapshot locking / SnapLock (stop if any)
#   2  disable the Scheduler and delete appmod-stage3 (detaches the S3 Access Point)
#   3  confirm 0 S3 Access Point attachments on appdata (even if attach ended FAILED)
#   4  delete any leftover FlexClone appdata_it_* and manual snapshots it_* (integration-clone.sh)
#   5  confirm recovery-queue empty, clone list empty, no it_* snapshots
#   6  aws fsx delete-volume appdata --ontap-configuration SkipFinalBackup=true; confirm gone
#   7  empty the artifacts bucket
#   8  delete appmod-base
#   9  delete the four secrets with --force-delete-without-recovery
#   10 confirm absence by API enumeration (not the stack list)
#   11 next day: confirm in Cost Explorer that billing stopped
#
# Every mutating step runs only under --apply; without it each step is printed. When APPMOD_DRY_RUN
# is set, even under --apply the AWS calls are printed, so the order can be exercised without
# credentials.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="ap-northeast-1"
DRY_RUN="${APPMOD_DRY_RUN:-}"
APPLY=""
AFTER_FAILED_CREATE=""
BASE_STACK="appmod-base"
STAGE3_STACK="appmod-stage3"

usage() {
  echo "usage: teardown.sh [--apply] [--after-failed-create]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --after-failed-create) AFTER_FAILED_CREATE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "teardown: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# Print a mutating step; run it only under --apply, and only really call AWS when not dry-run.
step() {
  local description="$1"; shift
  echo "- $description"
  if [ -z "$APPLY" ]; then
    echo "    (report only; pass --apply to run)"
    return 0
  fi
  if [ -n "$DRY_RUN" ]; then
    echo "    DRY-RUN: $*"
    return 0
  fi
  "$@"
}

aws_r() { aws --region "$REGION" "$@"; }

teardown_after_failed_create() {
  echo "teardown: after-failed-create path (file system exists, no Linux EC2)"
  echo "- 1' no Linux EC2 to run check-no-locking.sh. Confirm no ONTAP operation ran"
  echo "     (.private/runs/ has no ONTAP execution record), then verify with the AWS API"
  step "1' describe-volumes shows no SnaplockConfiguration on appdata" \
    aws_r fsx describe-volumes --filters Name=file-system-id,Values=fs-0123456789abcdef0
  step "6 delete appdata with SkipFinalBackup=true" \
    aws_r fsx delete-volume --volume-id vol-0123456789abcdef0 \
      --ontap-configuration SkipFinalBackup=true
  step "8 delete $BASE_STACK" aws_r cloudformation delete-stack --stack-name "$BASE_STACK"
  step "9 delete the four secrets with --force-delete-without-recovery" \
    echo "for each secret: aws secretsmanager delete-secret --force-delete-without-recovery"
  echo "- 10 confirm absence by API enumeration (see the main path)"
}

teardown_full() {
  echo "teardown: full deletion order in $REGION (apply=${APPLY:-no})"
  step "1 check-no-locking.sh over all volumes (stop if any lock is found)" \
    bash "$HERE/ontap/check-no-locking.sh"
  step "2 disable Scheduler and delete $STAGE3_STACK (detaches the S3 Access Point)" \
    aws_r cloudformation delete-stack --stack-name "$STAGE3_STACK"
  step "3 confirm 0 S3 Access Point attachments on appdata (even if attach was FAILED)" \
    aws_r fsx describe-s3-access-point-attachments
  step "4 delete leftover FlexClone appdata_it_* and manual snapshots it_*" \
    echo "integration-clone.sh delete --step <n> for each leftover"
  step "5 confirm recovery-queue empty, clone list empty, no it_* snapshots" \
    echo "volume recovery-queue show / volume clone show / snapshots"
  step "6 delete appdata with SkipFinalBackup=true; confirm gone" \
    aws_r fsx delete-volume --volume-id vol-0123456789abcdef0 \
      --ontap-configuration SkipFinalBackup=true
  step "7 empty the artifacts bucket" \
    echo "aws s3 rm s3://appmod-artifacts-123456789012-ap-northeast-1 --recursive"
  step "8 delete $BASE_STACK" aws_r cloudformation delete-stack --stack-name "$BASE_STACK"
  step "9 delete the four secrets with --force-delete-without-recovery" \
    echo "for each secret: aws secretsmanager delete-secret --force-delete-without-recovery"
  echo "- 10 confirm absence by API enumeration (describe-file-systems, "
  echo "     describe-storage-virtual-machines, describe-volumes, describe-directories, tagged"
  echo "     EC2/ENI/endpoints, describe-backups 0, describe-s3-access-point-attachments 0,"
  echo "     describe-secret -> ResourceNotFoundException). Poll up to 10 times, 60s apart."
  echo "- 11 next day: confirm in Cost Explorer that billing stopped."
}

if [ -n "$AFTER_FAILED_CREATE" ]; then
  teardown_after_failed_create
else
  teardown_full
fi
