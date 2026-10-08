#!/usr/bin/env bash
#
# Test fixture for check_no_stub_scripts.py. A stub: both the dry-run branch and the real branch
# only print the SSM call, so a real run sends nothing. The checker must reject it.
#
set -euo pipefail

DRY_RUN="${APPMOD_DRY_RUN:-}"
INSTANCE="${1:?usage: send-ssm.sh <instance-id>}"

if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: aws ssm send-command --instance-ids $INSTANCE --document-name AWS-RunShellScript"
else
  echo "aws ssm send-command --instance-ids $INSTANCE --document-name AWS-RunShellScript"
fi
