#!/usr/bin/env bash
#
# During stage 2, make the appmod/fsxadmin secret unreadable from the Linux EC2 instance role, so an
# SSM-reachable AIMF agent cannot read fsxadmin and enable snapshot locking over ONTAP REST
# (snapshot locking has no AWS API, so cfn-guard cannot stop it; this closes the fsxadmin path).
#
#   APPMOD_LINUX_ROLE_ARN=<arn> lock-fsxadmin.sh on
#                           attach a resource policy Denying GetSecretValue to the Linux role
#   lock-fsxadmin.sh off    remove that resource policy
#
# APPMOD_LINUX_ROLE_ARN is the Linux EC2 instance role ARN (arn:aws:iam::<account>:role/<name>),
# required for `on`. Read it from the base stack output LinuxRoleArn:
#   aws cloudformation describe-stacks --region ap-northeast-1 --stack-name appmod-base \
#     --query "Stacks[0].Outputs[?OutputKey=='LinuxRoleArn'].OutputValue" --output text
# A real `on` without it, with a value that is not a whole IAM role ARN (12-digit account), or with
# the placeholder account 123456789012, exits 2 before any call: a Deny written for some other
# principal would report the lock as on while the Linux role could still read fsxadmin. Under
# APPMOD_DRY_RUN a placeholder ARN is used when unset.
#
# on at the start of stage 2 (task 4.1), off at the end (task 4.5). Boundary reads during stage 2
# use the read-only ONTAP role appmod_readonly, which does not need fsxadmin.
#
# Residual risk (accepted): the agent's SSO role can remove this Deny from the resource policy; that
# removal is not matched by the irreversible-ops guard and relies on the prose HOLD-6 rule.
#
# When APPMOD_DRY_RUN is set, the AWS calls are printed instead of run.
#
set -euo pipefail

REGION="ap-northeast-1"
DRY_RUN="${APPMOD_DRY_RUN:-}"
SECRET="appmod/fsxadmin"
LINUX_ROLE_ARN="${APPMOD_LINUX_ROLE_ARN:-}"

usage() { echo "usage: APPMOD_LINUX_ROLE_ARN=<linux-instance-role-arn> lock-fsxadmin.sh on | lock-fsxadmin.sh off" >&2; }

if [ $# -ne 1 ]; then usage; exit 2; fi
ACTION="$1"
case "$ACTION" in on|off) ;; *) echo "lock-fsxadmin: action must be on or off" >&2; usage; exit 2 ;; esac

if [ "$ACTION" = "on" ] && [ -z "$LINUX_ROLE_ARN" ]; then
  if [ -n "$DRY_RUN" ]; then
    LINUX_ROLE_ARN="arn:aws:iam::123456789012:role/appmod-linux-role"
  else
    echo "lock-fsxadmin: APPMOD_LINUX_ROLE_ARN (the Linux instance role ARN) is required for on" >&2
    usage
    exit 2
  fi
fi
if [ "$ACTION" = "on" ] && [ -z "$DRY_RUN" ]; then
  # A whole-string match: exactly 12 account digits, then a role path and name drawn from the IAM
  # name character set. A glob such as [0-9]* also accepted one digit followed by anything.
  if ! [[ "$LINUX_ROLE_ARN" =~ ^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$ ]]; then
    echo "lock-fsxadmin: APPMOD_LINUX_ROLE_ARN is not an IAM role ARN: $LINUX_ROLE_ARN" >&2; exit 2
  fi
  # 123456789012 is the documentation placeholder this script itself uses under dry-run. A Deny
  # written for it would report the lock as on while the real Linux role could still read fsxadmin.
  case "$LINUX_ROLE_ARN" in
    arn:aws:iam::123456789012:*)
      echo "lock-fsxadmin: APPMOD_LINUX_ROLE_ARN uses the placeholder account 123456789012; pass the real LinuxRoleArn stack output" >&2
      exit 2 ;;
  esac
fi

aws_call() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION $*"
    return 0
  fi
  aws --region "$REGION" "$@"
}

deny_policy() {
  cat <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Principal":{"AWS":"$LINUX_ROLE_ARN"},"Action":"secretsmanager:GetSecretValue","Resource":"*"}]}
EOF
}

case "$ACTION" in
  on)
    echo "lock-fsxadmin: Denying GetSecretValue on $SECRET for the Linux role"
    if [ -n "$DRY_RUN" ]; then
      echo "DRY-RUN: aws --region $REGION secretsmanager put-resource-policy --secret-id $SECRET --resource-policy <deny>"
    else
      aws --region "$REGION" secretsmanager put-resource-policy \
        --secret-id "$SECRET" \
        --resource-policy "$(deny_policy)"
    fi
    ;;
  off)
    echo "lock-fsxadmin: removing the resource policy from $SECRET"
    aws_call secretsmanager delete-resource-policy --secret-id "$SECRET"
    ;;
esac
