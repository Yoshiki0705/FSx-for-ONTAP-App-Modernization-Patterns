#!/usr/bin/env bash
#
# The one sanctioned entry to AWS Transform custom (`atx`). A human runs this in task 4.2.3; the
# agent is given only the result, and block_direct_atx.py stops the agent from running atx directly.
#
#   run-atx.sh --estimate <file> --approved-at <ISO 8601>
#
# It performs the SAME entry check as deploy.sh (shared via scripts/lib/entry-check.sh) with
# target=atx, then:
#   - scans the directory it sends with gitleaks using scripts/aimf/gitleaks-send.toml (which does
#     NOT allow-list .private/, unlike the repo .gitleaks.toml), failing on any finding;
#   - sets AWS_REGION=ap-northeast-1 and records the regionSource in the run log;
#   - moves the estimate to estimates/used/ on success.
#
# The directory sent is .private/aimf/DocIntake/ (the AWS Transform custom requirement is a git
# repository of the self-written sample only). When APPMOD_DRY_RUN is set, the gitleaks scan, the
# atx call and the move are printed instead of run.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=scripts/lib/entry-check.sh
. "$REPO_ROOT/scripts/lib/entry-check.sh"

REGION="ap-northeast-1"
DRY_RUN="${APPMOD_DRY_RUN:-}"
ESTIMATES_DIR="${APPMOD_ESTIMATES_DIR:-.private/estimates}"
SEND_DIR="${APPMOD_SEND_DIR:-.private/aimf/DocIntake}"
SEND_CONFIG="$REPO_ROOT/scripts/aimf/gitleaks-send.toml"
RUN_LOG="${APPMOD_RUN_LOG:-.private/runs/atx-run.log}"

ESTIMATE=""
APPROVED_AT=""

usage() { echo "usage: run-atx.sh --estimate <file> --approved-at <ISO 8601>" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --estimate) ESTIMATE="${2:-}"; shift 2 ;;
    --approved-at) APPROVED_AT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "run-atx: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if ! appmod_entry_check "$ESTIMATE" "$APPROVED_AT" "atx"; then
  echo "run-atx: entry check failed; not sending" >&2
  exit 2
fi

# Send-scan with the send config, which has no .private/ allow-list, so a key placed anywhere is
# caught. The repo .gitleaks.toml would hide a finding under .private/.
echo "run-atx: scanning $SEND_DIR with gitleaks-send.toml before sending"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: gitleaks dir $SEND_DIR --no-banner --redact --exit-code 1 --config $SEND_CONFIG"
else
  gitleaks dir "$SEND_DIR" --no-banner --redact --exit-code 1 --config "$SEND_CONFIG"
fi

export AWS_REGION="$REGION"
mkdir -p "$(dirname "$RUN_LOG")"
record_region() {
  echo "regionSource=AWS_REGION=$AWS_REGION at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

echo "run-atx: AWS_REGION=$AWS_REGION; recording regionSource to $RUN_LOG"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: (cd $SEND_DIR && atx run) with AWS_REGION=$AWS_REGION"
  record_region
else
  record_region >>"$RUN_LOG"
  ( cd "$SEND_DIR" && atx run --playbook dotnetfw-to-modern-dotnet ) 2>&1 | tee -a "$RUN_LOG"
fi

# Move the estimate to used/ so it cannot drive a second run.
mkdir -p "$ESTIMATES_DIR/used"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: mv $ESTIMATE $ESTIMATES_DIR/used/"
else
  mv "$ESTIMATE" "$ESTIMATES_DIR/used/"
fi
echo "run-atx: done; estimate moved to used/"
