#!/usr/bin/env bash
#
# The one sanctioned entry to AWS Transform custom (`atx`). A human runs this in task 4.2.3; the
# agent is given only the result, and block_direct_atx.py stops the agent from running atx directly.
#
#   run-atx.sh --estimate <file> --approved-at <ISO 8601>
#
# It performs the SAME entry check as deploy.sh (shared via scripts/lib/entry-check.sh) with
# target=atx, then:
#   - refuses the real run (exit 2) until the atx invocation has been verified (U8-U11, below);
#   - scans the directory it sends with gitleaks using scripts/aimf/gitleaks-send.toml (which does
#     NOT allow-list .private/, unlike the repo .gitleaks.toml), failing on any finding;
#   - sets AWS_REGION=ap-northeast-1 (and unsets ATX_CUSTOM_ENDPOINT, which would override it),
#     records that value as aws_region_env, and records regionSource as atx itself logged it;
#   - fails when atx fails (its exit status is read from PIPESTATUS, not from tee), and moves the
#     estimate to estimates/used/ only after a successful run, before anything else that could
#     fail afterwards (log write, regionSource capture, region check).
#
# The invocation is assembled from the public AWS Transform custom user guide: the non-interactive
# form `atx custom def exec -n <name> -p <path> -x -t` (Getting Started; Command Reference) and the
# managed transformation name AWS/comprehensive-codebase-analysis (Managed Transformations). Re-read
# on 2026-10-07: the Command Reference lists -n, -p, -x and -t and does not mark -c required, the
# March 2026 GA notice gives `atx custom def exec -n AWS/comprehensive-codebase-analysis -p` as the
# way to start, and Getting Started lists ap-northeast-1 among the service's Regions (U8, U11
# documented). Running that form for this transformation, without a build command (-c), has not been
# done: whether the Tokyo registry lists it (U8), whether $0.035 per agent minute applies to it (U9),
# and reachability and IAM (U10) stay open until task 2.4. Until then the real path fails closed. Task 2.4 writes the
# verification record (.private/runs/atx-invocation-verified.json) after confirming, with atx
# installed, `atx --version`, `atx custom def list --json` listing the transformation, and
# `atx custom def exec --help` accepting these flags. The record must name this exact invocation.
#
# regionSource: the user guide shows atx writing a DEBUG line "Initializing FrontendServiceClient
# with config" carrying "region" and "regionSource", and puts developer debug logs under
# ~/.aws/atx/logs/. This script reads regionSource from atx's own output and from the debug logs
# written during the run; it never derives regionSource from AWS_REGION. Whether the debug log is
# written without an extra flag is unverified, so when no regionSource is found the record says
# "unverified".
#
# The directory sent is .private/aimf/DocIntake/ (the AWS Transform custom requirement is a git
# repository of the self-written sample only). When APPMOD_DRY_RUN is set, the gitleaks scan, the
# atx call and the move are printed instead of run, and the verification gate only reports.
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
VERIFIED_RECORD="${APPMOD_ATX_VERIFIED_RECORD:-.private/runs/atx-invocation-verified.json}"
ATX_LOG_DIR="${APPMOD_ATX_LOG_DIR:-$HOME/.aws/atx/logs}"

ATX_TRANSFORMATION="AWS/comprehensive-codebase-analysis"
ATX_ARGS=(custom def exec -n "$ATX_TRANSFORMATION" -p . -x -t)
ATX_INVOCATION="atx ${ATX_ARGS[*]}"

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

# The verification gate. Passes only when the record exists and names this exact invocation, this
# transformation and ap-northeast-1, with an atx version and a verification time.
invocation_verified() {
  [ -f "$VERIFIED_RECORD" ] || return 1
  APPMOD_REC="$VERIFIED_RECORD" APPMOD_INV="$ATX_INVOCATION" APPMOD_TX="$ATX_TRANSFORMATION" \
  APPMOD_REGION="$REGION" python3 -c 'import json,os,sys
try:
    r = json.load(open(os.environ["APPMOD_REC"], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(1)
ok = (r.get("invocation") == os.environ["APPMOD_INV"]
      and r.get("transformation") == os.environ["APPMOD_TX"]
      and r.get("region") == os.environ["APPMOD_REGION"]
      and r.get("atx_version") and r.get("verified_at"))
sys.exit(0 if ok else 1)'
}

if invocation_verified; then
  echo "run-atx: atx invocation verified by $VERIFIED_RECORD: $ATX_INVOCATION"
elif [ -n "$DRY_RUN" ]; then
  echo "run-atx: atx invocation NOT verified (U8-U11); a real run would exit 2 here"
else
  echo "run-atx: the atx invocation is unverified (U8-U11): no verification record at $VERIFIED_RECORD" >&2
  echo "run-atx: '$ATX_INVOCATION' is assembled from the public user guide but has not been run;" >&2
  echo "run-atx: task 2.4 confirms it and writes the record. Not sending." >&2
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
# ATX_CUSTOM_ENDPOINT overrides every other region source (user guide), so it must not leak in.
unset ATX_CUSTOM_ENDPOINT
mkdir -p "$(dirname "$RUN_LOG")"
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Read regionSource (and region) from what atx itself wrote: its captured output, then the debug
# logs modified during this run. Prints "regionSource=<v> region=<r> (from <where>)" or the
# unverified line.
capture_region_source() {
  local out_file="$1" marker="$2"
  local logs=()
  if [ -d "$ATX_LOG_DIR" ]; then
    while IFS= read -r f; do logs+=("$f"); done < <(find "$ATX_LOG_DIR" -type f -newer "$marker")
  fi
  # ${logs[@]+...}: an empty array under set -u is fatal on bash < 4.4 (macOS /bin/bash is 3.2).
  python3 - "$out_file" ${logs[@]+"${logs[@]}"} <<'PY'
import re
import sys

pat_src = re.compile(r'"regionSource"\s*:\s*"([^"]*)"')
pat_reg = re.compile(r'"region"\s*:\s*"([^"]*)"')
for path in sys.argv[1:]:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    m = pat_src.search(text)
    if m:
        r = pat_reg.search(text)
        where = "atx output" if path == sys.argv[1] else "atx debug log " + path
        print(f"regionSource={m.group(1)} region={r.group(1) if r else 'unknown'} (from {where})")
        sys.exit(0)
print("regionSource=unverified (no regionSource line in atx output or its debug logs)")
PY
}

echo "run-atx: AWS_REGION=$AWS_REGION; recording aws_region_env and regionSource to $RUN_LOG"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: (cd $SEND_DIR && $ATX_INVOCATION) with AWS_REGION=$AWS_REGION, output tee'd to $RUN_LOG"
  echo "DRY-RUN: regionSource read from atx output and from $ATX_LOG_DIR (files newer than the run start)"
  echo "DRY-RUN: mv $ESTIMATE $ESTIMATES_DIR/used/   (only when atx exits 0)"
  echo "run-atx: dry-run done"
  exit 0
fi

echo "aws_region_env=$AWS_REGION at $(stamp)" >>"$RUN_LOG"
echo "invocation=$ATX_INVOCATION" >>"$RUN_LOG"
ATX_OUT="$(mktemp)"
MARKER="$(mktemp)"
trap 'rm -f "$ATX_OUT" "$MARKER"' EXIT

# Read atx's own exit status from PIPESTATUS; tee's status would otherwise stand in for it.
set +e
( cd "$SEND_DIR" && atx "${ATX_ARGS[@]}" ) 2>&1 | tee -a "$RUN_LOG" "$ATX_OUT"
statuses=("${PIPESTATUS[@]}")
set -e
atx_rc="${statuses[0]}"
tee_rc="${statuses[1]}"

# Never fatal: the region capture runs after atx, so a failure here must not skip consuming the
# estimate of a run that already happened (and was billed).
# The || sits on the assignment, outside the $(...) subshell, so even a shell error inside the
# capture (which ends that subshell) falls back to the unverified line.
region_of_run() {
  local line
  line="$(capture_region_source "$ATX_OUT" "$MARKER" 2>/dev/null)" \
    || line="regionSource=unverified (capture failed)"
  [ -n "$line" ] || line="regionSource=unverified (capture printed nothing)"
  printf '%s\n' "$line"
}

if [ "$atx_rc" -ne 0 ]; then
  region_line="$(region_of_run)"
  echo "$region_line" >>"$RUN_LOG"
  echo "$region_line"
  echo "atx exit=$atx_rc at $(stamp); estimate NOT moved to used/" >>"$RUN_LOG"
  echo "atx exit=$atx_rc; estimate NOT moved to used/" >&2
  exit 1
fi

# atx succeeded, so the run happened: consume the estimate FIRST, before anything else that could
# fail (log write, region capture, region check), so the entry check cannot pass a second time.
mkdir -p "$ESTIMATES_DIR/used"
mv "$ESTIMATE" "$ESTIMATES_DIR/used/"
echo "run-atx: estimate moved to used/"
echo "atx exit=0 at $(stamp)" >>"$RUN_LOG"

region_line="$(region_of_run)"
echo "$region_line" >>"$RUN_LOG"
echo "$region_line"

if [ "$tee_rc" -ne 0 ]; then
  echo "run-atx: writing the run log failed (tee exit $tee_rc); the run log is incomplete" >&2
  exit 1
fi

case "$region_line" in
  *" region=$REGION "*) echo "run-atx: done" ;;
  regionSource=unverified*)
    echo "run-atx: done; regionSource is unverified (not found in atx output or debug logs)" ;;
  *)
    echo "run-atx: atx reported a region other than $REGION: $region_line; stop and ask a human" >&2
    exit 1
    ;;
esac
