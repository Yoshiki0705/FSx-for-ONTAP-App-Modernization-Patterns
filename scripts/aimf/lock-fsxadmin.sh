#!/usr/bin/env bash
#
# During stage 2, make the appmod/fsxadmin secret unreadable from the Linux EC2 instance role, so an
# SSM-reachable AIMF agent cannot read fsxadmin and enable snapshot locking over ONTAP REST
# (snapshot locking has no AWS API, so cfn-guard cannot stop it; this closes the fsxadmin path).
#
#   lock-fsxadmin.sh on     attach a resource policy Denying GetSecretValue to the Linux role
#   lock-fsxadmin.sh off    remove that resource policy
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
LINUX_ROLE_ARN="${APPMOD_LINUX_ROLE_ARN:-arn:aws:iam::123456789012:role/appmod-linux-role}"

usage() { echo "usage: lock-fsxadmin.sh on|off" >&2; }

if [ $# -ne 1 ]; then usage; exit 2; fi
ACTION="$1"
case "$ACTION" in on|off) ;; *) echo "lock-fsxadmin: action must be on or off" >&2; usage; exit 2 ;; esac

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
