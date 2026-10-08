#!/usr/bin/env bash
#
# Test fixture for check_no_stub_scripts.py. The control for stub/scripts/send-ssm.sh: the same
# script with a real real-mode path. The checker must accept it.
#
set -euo pipefail

DRY_RUN="${APPMOD_DRY_RUN:-}"
INSTANCE="${1:?usage: send-ssm.sh <instance-id>}"

if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: aws ssm send-command --instance-ids $INSTANCE --document-name AWS-RunShellScript"
  exit 0
fi
aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript
