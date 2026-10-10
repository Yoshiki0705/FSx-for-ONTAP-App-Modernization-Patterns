#!/usr/bin/env bash
#
# The one sanctioned entry to AWS Transform custom (`atx`). A human runs this; the agent is given
# only the result, and block_direct_atx.py stops the agent from running atx directly.
#
#   run-atx.sh --estimate <file> --approved-at <ISO 8601>
#
# Exit codes: 0 atx finished; 1 atx failed (estimate kept for a retry) or the run log/region check
# failed after a finished run; 2 refused before anything was sent; 3 atx stopped at the agent-minute
# limit (minutes were billed, so the estimate is consumed).
#
# What runs is decided by the approved estimate alone. estimate.py --target atx records two
# parameters (ParameterKey/ParameterValue pairs, the same shape the base estimate uses):
# Transformation and LimitMinutes. This script reads them from the estimate and from nowhere else,
# so there is no flag or environment variable that changes the transformation or raises the cap.
# An atx estimate without either, with a LimitMinutes that is not a positive integer, or naming a
# transformation outside the allow-list below is refused (exit 2). estimate.py's ATX_TRANSFORMATIONS
# holds the same two names; dryrun_shell_tests.sh fails when they differ.
#
#   Transformation                       Invocation (run inside the send directory)
#   AWS/comprehensive-codebase-analysis  atx custom def exec -n <name> -p . -x -t --limit <N>
#   AWS/dotnet-modernization             atx custom def exec -n <name> -p . -x -t --limit <N>
#
# Sources: the Command Reference and Getting Started pages of the AWS Transform custom user guide
# give the non-interactive form `-n <name> -p <path> -x -t`; `atx custom def exec --help` (atx
# 3.18.0, 2026-10-10) lists `--limit <minutes>`, an agent-minute budget at which atx exits 2 and can
# be resumed with a higher limit. The .NET page (dotnet-work-with-agent.html) gives
# `atx custom def exec -n AWS/dotnet-modernization -p <path-to-solution> [-q] [-x] [-t]` with no
# build command (-c) or configuration (-g); its default target is net10.0. The pricing page
# (https://aws.amazon.com/transform/pricing/) says an interrupted transformation can be resumed up
# to 24 hours later. On 2026-10-10 `atx custom def list --json` listed both names in ap-northeast-1.
#
# Per-invocation verification gate: a real run proceeds only when the verification record for THIS
# transformation names this exact invocation, including `--limit <N>`:
#   .private/runs/atx-invocation-verified-comprehensive-codebase-analysis.json
#   .private/runs/atx-invocation-verified-dotnet-modernization.json
# (APPMOD_ATX_VERIFIED_RECORD overrides the path, for tests). The record's invocation,
# transformation, limit_minutes and region must match, and atx_version and verified_at must be set.
# A different limit therefore needs a new record as well as a new estimate and approval.
#
# Send directories. comprehensive-codebase-analysis produces reports and does not modify code, so it
# runs on .private/aimf/DocIntake (a git repository with no commits; nothing is committed inside it).
# dotnet-modernization rewrites the code in place, so it never runs there: it runs on
# .private/aimf/DocIntake-atx-dotnet, a separate copy made from a committed state of app/legacy.
# For dotnet-modernization this script refuses (exit 2, before the scan and atx) a send directory
# that is not the top of its own git work tree, has no commits, has uncommitted or untracked
# changes, or resolves to the same physical path as the analysis directory. The HEAD sent is
# written to the run log as send_head. APPMOD_SEND_DIR and APPMOD_ANALYSIS_SEND_DIR override the
# two paths, for tests. The dotnet copy is created once, from the repository root:
#   mkdir -p .private/aimf/DocIntake-atx-dotnet
#   git archive --format=tar HEAD:app/legacy | tar -x -C .private/aimf/DocIntake-atx-dotnet
#   git -C .private/aimf/DocIntake-atx-dotnet init -q
#   git -C .private/aimf/DocIntake-atx-dotnet add -A
#   git -C .private/aimf/DocIntake-atx-dotnet -c user.name=appmod \
#     -c user.email=appmod@example.com commit -q -m "init: DocIntake sample from Spoke app/legacy"
#
# Before calling atx it performs the SAME entry check as deploy.sh (scripts/lib/entry-check.sh)
# with target=atx, then:
#   - scans the send directory with gitleaks using scripts/aimf/gitleaks-send.toml (which does NOT
#     allow-list .private/, unlike the repo .gitleaks.toml); a finding or a missing binary refuses
#     the send with exit 2, since nothing has been sent yet;
#   - sets AWS_REGION=ap-northeast-1 (and unsets ATX_CUSTOM_ENDPOINT, which would override it),
#     records that value as aws_region_env, and records regionSource as atx itself logged it;
#   - reads atx's exit status from PIPESTATUS, not from tee. On 0 the estimate moves to
#     estimates/used/ before anything else that could fail. On 2 (limit reached) the minutes were
#     billed, so the estimate is consumed the same way and the script exits 3. Any other failure
#     keeps the estimate and exits 1.
#
# regionSource: the user guide shows atx writing a DEBUG line "Initializing FrontendServiceClient
# with config" carrying "region" and "regionSource", under ~/.aws/atx/logs/. This script reads
# regionSource from atx's own output and from the debug logs written during the run; it never
# derives regionSource from AWS_REGION. When none is found the record says "unverified".
#
# When APPMOD_DRY_RUN is set, the gitleaks scan, the atx call and the move are printed instead of
# run, and the verification gate only reports. The send-directory check is read-only and runs.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=scripts/lib/entry-check.sh
. "$REPO_ROOT/scripts/lib/entry-check.sh"

REGION="ap-northeast-1"
DRY_RUN="${APPMOD_DRY_RUN:-}"
ESTIMATES_DIR="${APPMOD_ESTIMATES_DIR:-.private/estimates}"
ANALYSIS_SEND_DIR="${APPMOD_ANALYSIS_SEND_DIR:-.private/aimf/DocIntake}"
DOTNET_SEND_DIR=".private/aimf/DocIntake-atx-dotnet"
SEND_CONFIG="$REPO_ROOT/scripts/aimf/gitleaks-send.toml"
RUN_LOG="${APPMOD_RUN_LOG:-.private/runs/atx-run.log}"
ATX_LOG_DIR="${APPMOD_ATX_LOG_DIR:-$HOME/.aws/atx/logs}"

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

# Print Transformation and LimitMinutes from the estimate's parameters, one per line. Exits 2 with
# a message when the estimate has no parameters list or either key is missing.
read_atx_parameters() {
  APPMOD_EST="$ESTIMATE" python3 -c 'import json,os,sys
def fail(msg):
    print("run-atx: " + msg, file=sys.stderr)
    sys.exit(2)
try:
    est = json.load(open(os.environ["APPMOD_EST"], encoding="utf-8"))
except (OSError, ValueError) as exc:
    fail(f"estimate is not readable JSON: {exc}")
params = est.get("parameters")
if not isinstance(params, list):
    fail("the atx estimate has no parameters list; re-estimate with estimate.py --target atx "
         "--transformation <name> --limit-minutes <N>")
pairs = {p.get("ParameterKey"): p.get("ParameterValue") for p in params if isinstance(p, dict)}
for key in ("Transformation", "LimitMinutes"):
    if not isinstance(pairs.get(key), str) or not pairs[key]:
        fail(f"the atx estimate parameters have no {key}; re-estimate with estimate.py --target atx")
print(pairs["Transformation"])
print(pairs["LimitMinutes"])'
}

if ! ATX_PARAMS="$(read_atx_parameters)"; then
  echo "run-atx: not sending" >&2
  exit 2
fi
ATX_TRANSFORMATION="${ATX_PARAMS%%$'\n'*}"
ATX_LIMIT="${ATX_PARAMS#*$'\n'}"

# The allow-list (same two names as estimate.py ATX_TRANSFORMATIONS).
case "$ATX_TRANSFORMATION" in
  AWS/comprehensive-codebase-analysis)
    ATX_SLUG="comprehensive-codebase-analysis"
    SEND_DIR="${APPMOD_SEND_DIR:-$ANALYSIS_SEND_DIR}"
    REQUIRE_COMMITTED=""
    ;;
  AWS/dotnet-modernization)
    ATX_SLUG="dotnet-modernization"
    SEND_DIR="${APPMOD_SEND_DIR:-$DOTNET_SEND_DIR}"
    REQUIRE_COMMITTED=1
    ;;
  *)
    echo "run-atx: transformation '$ATX_TRANSFORMATION' is not in the allow-list" \
      "(AWS/comprehensive-codebase-analysis, AWS/dotnet-modernization); not sending" >&2
    exit 2
    ;;
esac
case "$ATX_LIMIT" in
  ''|*[!0-9]*|0*)
    echo "run-atx: LimitMinutes '$ATX_LIMIT' is not a positive integer; not sending" >&2
    exit 2
    ;;
esac

VERIFIED_RECORD="${APPMOD_ATX_VERIFIED_RECORD:-.private/runs/atx-invocation-verified-$ATX_SLUG.json}"
ATX_ARGS=(custom def exec -n "$ATX_TRANSFORMATION" -p . -x -t --limit "$ATX_LIMIT")
ATX_INVOCATION="atx ${ATX_ARGS[*]}"
echo "run-atx: transformation=$ATX_TRANSFORMATION limit_minutes=$ATX_LIMIT (from the estimate)"

# The per-invocation verification gate. Passes only when the record names this exact invocation
# (including --limit), this transformation, this limit and ap-northeast-1, with an atx version and a
# verification time.
invocation_verified() {
  [ -f "$VERIFIED_RECORD" ] || return 1
  APPMOD_REC="$VERIFIED_RECORD" APPMOD_INV="$ATX_INVOCATION" APPMOD_TX="$ATX_TRANSFORMATION" \
  APPMOD_LIMIT="$ATX_LIMIT" APPMOD_REGION="$REGION" python3 -c 'import json,os,sys
try:
    r = json.load(open(os.environ["APPMOD_REC"], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(1)
lim = r.get("limit_minutes")
ok = (r.get("invocation") == os.environ["APPMOD_INV"]
      and r.get("transformation") == os.environ["APPMOD_TX"]
      and isinstance(lim, int) and not isinstance(lim, bool)
      and str(lim) == os.environ["APPMOD_LIMIT"]
      and r.get("region") == os.environ["APPMOD_REGION"]
      and r.get("atx_version") and r.get("verified_at"))
sys.exit(0 if ok else 1)'
}

if invocation_verified; then
  echo "run-atx: atx invocation verified by $VERIFIED_RECORD: $ATX_INVOCATION"
elif [ -n "$DRY_RUN" ]; then
  echo "run-atx: atx invocation NOT verified for $ATX_TRANSFORMATION; a real run would exit 2 here"
else
  echo "run-atx: no verification record for '$ATX_INVOCATION' at $VERIFIED_RECORD" >&2
  echo "run-atx: the record must name this transformation, limit_minutes $ATX_LIMIT and this exact" >&2
  echo "run-atx: invocation (a different limit needs a new record). Not sending." >&2
  exit 2
fi

# Physical path of a directory, or empty when it cannot be entered.
physical_dir() { (cd "$1" 2>/dev/null && pwd -P) || true; }

# HEAD of the send directory when it is the top of its own git work tree, else a "none (...)" note.
send_head_of() {
  local dir="$1" top head
  top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$top" ] || [ "$(physical_dir "$top")" != "$(physical_dir "$dir")" ]; then
    echo "none (not a git work tree)"
  elif head="$(git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null)"; then
    echo "$head"
  else
    echo "none (no commits)"
  fi
}

# dotnet-modernization rewrites files, so it only runs on its own clean, committed copy.
check_send_dir() {
  local dir="$1" phys analysis_phys head status
  phys="$(physical_dir "$dir")"
  if [ -z "$phys" ]; then
    echo "run-atx: send directory $dir does not exist; not sending" >&2
    return 2
  fi
  [ -n "$REQUIRE_COMMITTED" ] || return 0
  analysis_phys="$(physical_dir "$ANALYSIS_SEND_DIR")"
  if [ -n "$analysis_phys" ] && [ "$phys" = "$analysis_phys" ]; then
    echo "run-atx: $ATX_TRANSFORMATION changes code and must not run on the analysis copy" \
      "($ANALYSIS_SEND_DIR); use $DOTNET_SEND_DIR. Not sending." >&2
    return 2
  fi
  head="$(send_head_of "$dir")"
  case "$head" in
    none*)
      echo "run-atx: $ATX_TRANSFORMATION needs a send directory with commits; $dir: $head." \
        "Not sending." >&2
      return 2
      ;;
  esac
  if ! status="$(git -C "$dir" status --porcelain 2>/dev/null)"; then
    echo "run-atx: git status failed in $dir; not sending" >&2
    return 2
  fi
  if [ -n "$status" ]; then
    echo "run-atx: $dir has uncommitted or untracked changes; commit or discard them first." \
      "Not sending." >&2
    return 2
  fi
  return 0
}

if ! check_send_dir "$SEND_DIR"; then
  exit 2
fi
SEND_HEAD="$(send_head_of "$SEND_DIR")"
if [ -n "$REQUIRE_COMMITTED" ]; then
  echo "run-atx: send directory $SEND_DIR is a clean git work tree at $SEND_HEAD"
else
  echo "run-atx: send directory $SEND_DIR (send_head=$SEND_HEAD)"
fi

# Send-scan with the send config, which has no .private/ allow-list, so a key placed anywhere is
# caught. The repo .gitleaks.toml would hide a finding under .private/.
echo "run-atx: scanning $SEND_DIR with gitleaks-send.toml before sending"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: gitleaks dir $SEND_DIR --no-banner --redact --exit-code 1 --config $SEND_CONFIG"
else
  # A finding (or a missing binary) must refuse the send with the documented refusal code (2),
  # not fall through set -e as 1/127, which the exit-code contract reads as "atx failed". Nothing
  # has been sent yet at this point, so 2 is correct.
  set +e
  gitleaks dir "$SEND_DIR" --no-banner --redact --exit-code 1 --config "$SEND_CONFIG"
  gitleaks_rc=$?
  set -e
  if [ "$gitleaks_rc" -ne 0 ]; then
    echo "run-atx: gitleaks reported a finding or failed to run (exit $gitleaks_rc);" \
      "nothing was sent. Not sending." >&2
    exit 2
  fi
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

LIMIT_MESSAGE="limit reached, resumable for 24 h, needs a new estimate to raise the limit"

echo "run-atx: AWS_REGION=$AWS_REGION; recording aws_region_env and regionSource to $RUN_LOG"
if [ -n "$DRY_RUN" ]; then
  echo "DRY-RUN: (cd $SEND_DIR && $ATX_INVOCATION) with AWS_REGION=$AWS_REGION, output tee'd to $RUN_LOG"
  echo "DRY-RUN: regionSource read from atx output and from $ATX_LOG_DIR (files newer than the run start)"
  echo "DRY-RUN: mv $ESTIMATE $ESTIMATES_DIR/used/   (when atx exits 0, or 2 = $LIMIT_MESSAGE -> exit 3)"
  echo "run-atx: dry-run done"
  exit 0
fi

{
  echo "aws_region_env=$AWS_REGION at $(stamp)"
  echo "transformation=$ATX_TRANSFORMATION"
  echo "limit_minutes=$ATX_LIMIT"
  echo "send_dir=$SEND_DIR"
  echo "send_head=$SEND_HEAD"
  echo "invocation=$ATX_INVOCATION"
} >>"$RUN_LOG"
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

if [ "$atx_rc" -eq 2 ]; then
  # atx stopped at --limit: the minutes up to the limit were billed, so consume the estimate FIRST
  # (as on success), then report. Raising the limit needs a new estimate, approval and record.
  mkdir -p "$ESTIMATES_DIR/used"
  mv "$ESTIMATE" "$ESTIMATES_DIR/used/"
  echo "run-atx: estimate moved to used/ (the limit was reached, so the minutes were billed)"
  echo "atx exit=2 at $(stamp): $LIMIT_MESSAGE (limit_minutes=$ATX_LIMIT)" >>"$RUN_LOG"
  region_line="$(region_of_run)"
  echo "$region_line" >>"$RUN_LOG"
  echo "$region_line"
  # This run was billed to the cap, so an incomplete run log must be reported here too, as on the
  # success path. The exit code stays 3 (the run reached the limit); the warning is advisory.
  if [ "$tee_rc" -ne 0 ]; then
    echo "run-atx: writing the run log failed (tee exit $tee_rc); the run log is incomplete" >&2
  fi
  echo "run-atx: atx exit=2: $LIMIT_MESSAGE (limit_minutes=$ATX_LIMIT)" >&2
  # dotnet-modernization rewrites the code in place, so after a limit stop the send copy is dirty
  # and the clean-tree gate refuses a re-run. Resuming it is a human decision (recreate the copy
  # from a committed state, or keep the partial result) and is outside this script.
  case "$ATX_TRANSFORMATION" in
    AWS/dotnet-modernization)
      echo "run-atx: $ATX_TRANSFORMATION rewrote $SEND_DIR, so a re-run is refused by the" \
        "clean-tree gate; resuming it is a human decision, not run-atx.sh" >&2
      ;;
  esac
  exit 3
fi

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
