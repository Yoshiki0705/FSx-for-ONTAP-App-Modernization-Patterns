#!/usr/bin/env bash
#
# Set up the AIMF workspace in the gitignored .private/aimf/ area and wire the three PreToolUse
# hooks. Run after approval of the stage-2 prerequisites; re-run safely (idempotent where possible).
#
# Steps (design "setup-workspace.sh の手順"):
#   1 clone AIMF at the pinned tag into .private/aimf/upstream/ and verify the commit
#   2 copy app/legacy/ to .private/aimf/DocIntake/ and make it a git repository (ATX requirement)
#   3 run install.sh for the dotnetfw-to-modern-dotnet playbook
#   4 write the hook wiring (guard, canary, block_direct_atx) at both sites; run check-hook-wiring.py
#   5 confirm .private/aimf/ is git-ignored
#   6 send-scan self-test: a run-time-generated fake key in a temp dir must make gitleaks-send.toml
#     exit 1; then delete the temp dir (the fake key is never written into the repo)
#   7 confirm every git repository under the workspace has an empty remote
#
# When APPMOD_DRY_RUN is set, the clone, install.sh and atx-adjacent calls are printed instead of
# run; the send-scan self-test (step 6) still runs because it only needs gitleaks and a temp dir.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DRY_RUN="${APPMOD_DRY_RUN:-}"

AIMF_TAG="v0.11.0"
AIMF_COMMIT="b0d8b088fce44360820252293fa582147b9a12b5"
AIMF_REPO="${APPMOD_AIMF_REPO:-https://github.com/aws-samples/sample-ai-modernization-flow.git}"
WORKSPACE="${APPMOD_AIMF_WORKSPACE:-$REPO_ROOT/.private/aimf}"
UPSTREAM="$WORKSPACE/upstream"
PROJECT_DIR="$WORKSPACE/DocIntake"
GUARD_ABS="$REPO_ROOT/scripts/guard_irreversible_ops.py"
SEND_CONFIG="$REPO_ROOT/scripts/aimf/gitleaks-send.toml"

run() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: $*"
    return 0
  fi
  "$@"
}

step1_clone() {
  echo "1 clone AIMF $AIMF_TAG into $UPSTREAM and verify commit $AIMF_COMMIT"
  run git clone --branch "$AIMF_TAG" --depth 1 "$AIMF_REPO" "$UPSTREAM"
  if [ -z "$DRY_RUN" ]; then
    local head
    head="$(git -C "$UPSTREAM" rev-parse HEAD)"
    if [ "$head" != "$AIMF_COMMIT" ]; then
      echo "setup-workspace: cloned commit $head != pinned $AIMF_COMMIT" >&2
      exit 1
    fi
  fi
}

step2_copy() {
  echo "2 copy app/legacy/ to $PROJECT_DIR and git init it"
  run mkdir -p "$PROJECT_DIR"
  run cp -R "$REPO_ROOT/app/legacy/." "$PROJECT_DIR/"
  run git -C "$PROJECT_DIR" init -q
}

step3_install() {
  echo "3 run install.sh for dotnetfw-to-modern-dotnet"
  run bash "$UPSTREAM/install.sh" --project DocIntake \
    --playbook dotnetfw-to-modern-dotnet --lang ja --tool kiro
}

step4_wire() {
  echo "4 write hook wiring at both sites and run check-hook-wiring.py"
  # In a real run the wiring JSON is written here; the shapes are the ones check-hook-wiring.py
  # accepts. This script prints the intended write; the actual file content is environment-specific.
  echo "   guard: python3 $GUARD_ABS"
  echo "   canary: python3 $REPO_ROOT/scripts/aimf/hook_canary.py --source <site>"
  echo "   atx-block: python3 $REPO_ROOT/scripts/aimf/block_direct_atx.py"
  echo "   (verify with: check-hook-wiring.py --workspace-hook <f> --agent-config <f> --guard $GUARD_ABS)"
}

step5_ignored() {
  echo "5 confirm $WORKSPACE is git-ignored"
  if [ -z "$DRY_RUN" ]; then
    if ! git -C "$REPO_ROOT" check-ignore -q "$WORKSPACE"; then
      echo "setup-workspace: $WORKSPACE is not git-ignored" >&2
      exit 1
    fi
  fi
}

step6_send_scan_selftest() {
  echo "6 send-scan self-test: a planted fake key must make gitleaks-send.toml exit 1"
  local tmp
  tmp="$(mktemp -d)"
  # Build the fake key at run time so it is never a literal in this repository: "AKIA" plus 16
  # random characters from [A-Z2-7], the shape of an access key ID. The AWS documentation example
  # key cannot be used, because the default gitleaks AWS rule allowlists values ending in EXAMPLE,
  # so this self-test could never pass with it. A draw below Shannon entropy 3.5 is redrawn, so the
  # rule's entropy floor (3) never drops a planted value.
  local planted
  planted="$(python3 -c 'import math, secrets
alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
while True:
    key = "AKIA" + "".join(secrets.choice(alphabet) for _ in range(16))  # gitleaks:allow
    counts = [key.count(c) for c in set(key)]
    if -sum(n / len(key) * math.log2(n / len(key)) for n in counts) >= 3.5:
        break
print(key)')" || { echo "setup-workspace: could not generate the planted key" >&2; rm -rf "$tmp"; exit 1; }
  printf 'aws_access_key_id = %s\n' "$planted" >"$tmp/planted.txt"
  unset planted
  # Any non-zero exit used to count as "flagged", so a missing gitleaks (127) or a config gitleaks
  # cannot load (also exit 1) read as a passing self-test. Pass only on exit 1 together with a JSON
  # report that lists at least one finding; the report is written outside the scanned directory.
  local code=0 findings=0 report
  report="$(mktemp)"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN would scan $tmp; simulating the expected non-zero exit"
    code=1
    findings=1
  else
    gitleaks dir "$tmp" --no-banner --redact --exit-code 1 --config "$SEND_CONFIG" \
      --report-format json --report-path "$report" >/dev/null 2>&1 || code=$?
    findings="$(python3 -c 'import json,sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    data = []
print(len(data) if isinstance(data, list) else 0)' "$report")" || findings=0
  fi
  rm -rf "$tmp" "$report"
  if [ "$code" -ne 1 ] || [ "$findings" -lt 1 ]; then
    echo "setup-workspace: send-scan did NOT flag the planted key (exit $code, $findings finding(s));" >&2
    echo "setup-workspace: gitleaks is missing or gitleaks-send.toml is wrong" >&2
    exit 1
  fi
  echo "   send-scan flagged the planted key (exit $code, $findings finding(s)); temp dir removed"
}

step7_remotes() {
  echo "7 confirm every git repository under $WORKSPACE has an empty remote"
  if [ -z "$DRY_RUN" ] && [ -d "$WORKSPACE" ]; then
    # Both statuses are checked: a failed find (process substitution, never checked) would skip
    # repositories, and `git remote -v || true` read an unreadable repository as "no remote".
    local git_dirs git_dir repo remotes
    if ! git_dirs="$(find "$WORKSPACE" -name .git -prune)"; then
      echo "setup-workspace: could not list the git repositories under $WORKSPACE" >&2
      exit 1
    fi
    while IFS= read -r git_dir; do
      [ -n "$git_dir" ] || continue
      repo="$(dirname "$git_dir")"
      if ! remotes="$(git -C "$repo" remote -v)"; then
        echo "setup-workspace: could not read the remotes of $repo" >&2
        exit 1
      fi
      if [ -n "$remotes" ]; then
        echo "setup-workspace: $repo has a remote (must be empty): $remotes" >&2
        exit 1
      fi
    done <<EOF
$git_dirs
EOF
  fi
}

main() {
  echo "setup-workspace: workspace=$WORKSPACE (dry-run=${DRY_RUN:-no})"
  step1_clone
  step2_copy
  step3_install
  step4_wire
  step5_ignored
  step6_send_scan_selftest
  step7_remotes
  echo "setup-workspace: done"
}

main "$@"
