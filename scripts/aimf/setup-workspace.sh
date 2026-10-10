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
CANARY_ABS="$REPO_ROOT/scripts/aimf/hook_canary.py"
ATX_BLOCK_ABS="$REPO_ROOT/scripts/aimf/block_direct_atx.py"
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
  # On a re-run the clone already exists; git clone would refuse it, so only the commit is checked.
  if [ -d "$UPSTREAM/.git" ]; then
    echo "   $UPSTREAM exists; verifying its commit instead of cloning again"
  else
    run git clone --branch "$AIMF_TAG" --depth 1 "$AIMF_REPO" "$UPSTREAM"
  fi
  if [ -z "$DRY_RUN" ]; then
    local head
    head="$(git -C "$UPSTREAM" rev-parse HEAD)"
    if [ "$head" != "$AIMF_COMMIT" ]; then
      echo "setup-workspace: cloned commit $head != pinned $AIMF_COMMIT" >&2
      exit 1
    fi
    # Step 7 requires every repository under the workspace to have no remote, and the clone is only
    # read after this point, so its origin is removed once the commit is verified.
    local remotes name
    if ! remotes="$(git -C "$UPSTREAM" remote)"; then
      echo "setup-workspace: could not read the remotes of $UPSTREAM" >&2
      exit 1
    fi
    for name in $remotes; do
      git -C "$UPSTREAM" remote remove "$name"
    done
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
  # install.sh installs into its current directory (WORKSPACE="$(pwd)"), so it must run inside the
  # workspace; run from the Spoke root it would write .kiro/ and the records repository there.
  # It refuses an existing records repository, so a re-run reinstalls the rules only.
  local skip=()
  if [ -n "$(find "$WORKSPACE" -maxdepth 1 -type d -name 'DocIntake-migration-*' 2>/dev/null)" ]; then
    echo "   a DocIntake-migration-* records repository exists; re-running with --skip-project"
    skip=(--skip-project)
  fi
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: (cd $WORKSPACE && bash $UPSTREAM/install.sh --project DocIntake" \
      "--playbook dotnetfw-to-modern-dotnet --lang ja --tool kiro ${skip[*]+${skip[*]}})"
    return 0
  fi
  (cd "$WORKSPACE" && bash "$UPSTREAM/install.sh" --project DocIntake \
    --playbook dotnetfw-to-modern-dotnet --lang ja --tool kiro ${skip[@]+"${skip[@]}"})
}

step4_wire() {
  echo "4 write hook wiring at both sites and run check-hook-wiring.py"
  local agent_cfg="$WORKSPACE/.kiro/agents/migration.json"
  local hook_file="$WORKSPACE/.kiro/hooks/block-unilateral-worm-enablement.json"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: would write $hook_file and add 3 preToolUse hooks to $agent_cfg"
    return 0
  fi
  if [ ! -f "$agent_cfg" ]; then
    echo "setup-workspace: install.sh did not create $agent_cfg" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$hook_file")"
  # The same three hooks at both sites (design 案 C): the Hub regex in the workspace file, exact tool
  # names in the agent config (see the note in the Python below). install.sh rewrites the
  # agent config on every run, so our entries are re-added after AIMF's own preToolUse entry, and any
  # earlier copy of them is dropped first so a re-run does not duplicate them.
  python3 - "$agent_cfg" "$hook_file" "$GUARD_ABS" "$CANARY_ABS" "$ATX_BLOCK_ABS" <<'PY'
import json
import shlex
import sys

agent_path, hook_path, guard, canary, atx_block = sys.argv[1:6]
MATCHER = "^(execute_bash|shell|use_aws|aws)$"
OURS = ("guard_irreversible_ops.py", "hook_canary.py", "block_direct_atx.py")


def commands(source):
    return [
        ("Block unilateral WORM enablement", f"python3 {shlex.quote(guard)}"),
        ("AIMF hook canary", f"python3 {shlex.quote(canary)} --source {source}"),
        ("Block direct atx", f"python3 {shlex.quote(atx_block)}"),
    ]


workspace_doc = {
    "version": "v1",
    "hooks": [
        {
            "name": name,
            "trigger": "PreToolUse",
            "matcher": MATCHER,
            "action": {"type": "command", "command": command, "timeout": 30},
        }
        for name, command in commands("workspace-hook")
    ],
}
with open(hook_path, "w", encoding="utf-8") as handle:
    json.dump(workspace_doc, handle, indent=2)
    handle.write("\n")

with open(agent_path, encoding="utf-8") as handle:
    agent = json.load(handle)
hooks = agent.setdefault("hooks", {})
kept = [
    entry
    for entry in hooks.get("preToolUse") or []
    if not any(name in str(entry.get("command", "")) for name in OURS)
]
# The agent config takes one exact tool name per entry (U28, observed 2026-10-09 with kiro-cli
# 2.28.0): the V2 engine, which plain `kiro-cli chat` runs, matched neither the Hub regex nor
# "execute_bash|use_aws", and refused to load the agent when the matcher was a list. Each name
# also matches its documented alias (shell, aws).
hooks["preToolUse"] = kept + [
    {"matcher": tool, "command": command, "timeout_ms": 30000}
    for _, command in commands("agent-config")
    for tool in ("execute_bash", "use_aws")
]
with open(agent_path, "w", encoding="utf-8") as handle:
    json.dump(agent, handle, indent=2)
    handle.write("\n")
PY
  python3 "$REPO_ROOT/scripts/aimf/check-hook-wiring.py" --workspace "$WORKSPACE" \
    --workspace-hook "$hook_file" --agent-config "$agent_cfg" --guard "$GUARD_ABS"
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
