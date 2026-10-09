#!/usr/bin/env python3
"""Verify the three PreToolUse hooks are wired at both sites before an AIMF session (stdlib only).

install.sh rewrites the agent config on every run, so wiring must be re-checked before every
session. Three hooks must each be present at BOTH wiring sites:

  1. the Hub irreversible-ops guard  (scripts/guard_irreversible_ops.py, by absolute path)
  2. hook_canary.py
  3. block_direct_atx.py

and the two sites are:

  - the workspace hook file  (.kiro/hooks/*.json, v1 format)
  - the agent config preToolUse  (.kiro/agents/migration.json)

A site's matcher must cover the same tools as the Hub matcher: execute_bash, shell, use_aws, aws.
Two matcher formats are accepted because the agent config's matcher may or may not take a regex
(U28): either a regex such as ^(execute_bash|shell|use_aws|aws)$, or an explicit list of tool names.
A matcher narrower than the Hub set fails.

The guard must be referenced by the tracked absolute path; a copy under .kiro/ or $HOME is invisible
to collaborators and drifts.

It also checks that every git repository under the workspace has an empty remote (M6): the record
repository must never be pushable.

  check-hook-wiring.py --workspace <dir> --guard <abs path to guard_irreversible_ops.py>
  check-hook-wiring.py --selftest     prove it fails on missing wiring and on a narrow matcher

Exit 0 when every hook is present at both sites with a wide-enough matcher and all remotes are
empty; exit 1 otherwise. No AWS or network call.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REQUIRED_TOOLS = {"execute_bash", "shell", "use_aws", "aws"}
REQUIRED_HOOKS = ("guard_irreversible_ops.py", "hook_canary.py", "block_direct_atx.py")


def matcher_covers(matcher: object) -> bool:
    """True when a matcher (regex string or list of tool names) covers every required tool."""
    if isinstance(matcher, list):
        return REQUIRED_TOOLS.issubset(set(matcher))
    if isinstance(matcher, str):
        try:
            pattern = re.compile(matcher)
        except re.error:
            return False
        return all(pattern.fullmatch(tool) for tool in REQUIRED_TOOLS)
    return False


def _preToolUse_entries(site: dict) -> list[dict]:
    """Collect the PreToolUse entries of one wiring site, whichever shape it uses.

    - workspace v1 file: {"version":"v1","hooks":[{"trigger":"PreToolUse","matcher":...,
      "action":{"type":"command","command":...}}]}. An entry with another trigger does not count.
    - agent config written by AIMF install.sh: {"hooks":{"preToolUse":[{"matcher":...,
      "command":...}]}}. The list sits under "hooks", keyed by event, not at the top level.
    - a top-level {"preToolUse":[...]} is also accepted.
    """
    entries: list[dict] = []
    hooks = site.get("hooks")
    if isinstance(hooks, list):
        entries += [
            e
            for e in hooks
            if isinstance(e, dict) and e.get("trigger", "PreToolUse") == "PreToolUse"
        ]
    elif isinstance(hooks, dict):
        entries += [e for e in hooks.get("preToolUse") or [] if isinstance(e, dict)]
    entries += [e for e in site.get("preToolUse") or [] if isinstance(e, dict)]
    return entries


def commands_of(site: dict) -> list[tuple[str, object]]:
    """Return (command_string, matcher) pairs from one wiring site's PreToolUse entries.

    The command is read from "command", from the v1 "action.command", or from a nested
    {"hooks":[{"type":"command","command":...}]} list.
    """
    pairs: list[tuple[str, object]] = []
    for entry in _preToolUse_entries(site):
        matcher = entry.get("matcher")
        command = entry.get("command")
        if isinstance(command, str):
            pairs.append((command, matcher))
        action = entry.get("action")
        if isinstance(action, dict) and isinstance(action.get("command"), str):
            pairs.append((action["command"], matcher))
        nested_list = entry.get("hooks") if isinstance(entry.get("hooks"), list) else []
        for nested in nested_list:
            if isinstance(nested, dict) and isinstance(nested.get("command"), str):
                pairs.append((nested["command"], matcher))
    return pairs


def site_problems(label: str, site: dict, guard_abs: str) -> list[str]:
    pairs = commands_of(site)
    problems: list[str] = []
    for hook in REQUIRED_HOOKS:
        matching = [(cmd, m) for cmd, m in pairs if hook in cmd]
        if not matching:
            problems.append(f"{label}: hook {hook} is not wired")
            continue
        if hook == "guard_irreversible_ops.py" and not any(
            guard_abs in cmd for cmd, _ in matching
        ):
            problems.append(
                f"{label}: guard is not referenced by the tracked path {guard_abs}"
            )
        if not any(matcher_covers(m) for _, m in matching):
            problems.append(
                f"{label}: {hook} matcher is narrower than {sorted(REQUIRED_TOOLS)}"
            )
    return problems


def check_sites(workspace_site: dict, agent_site: dict, guard_abs: str) -> list[str]:
    problems = site_problems("workspace-hook", workspace_site, guard_abs)
    problems += site_problems("agent-config", agent_site, guard_abs)
    return problems


def remote_problems(workspace: Path) -> list[str]:
    problems: list[str] = []
    for git_dir in workspace.rglob(".git"):
        repo = git_dir.parent
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "remote", "-v"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if result.stdout.strip():
            problems.append(f"git repository has a remote (must be empty): {repo}")
    return problems


def _good_sites(guard_abs: str) -> tuple[dict, dict]:
    """The shapes setup-workspace.sh writes: a v1 workspace file and AIMF's agent config."""
    regex = "^(execute_bash|shell|use_aws|aws)$"
    commands = (
        f"python3 {guard_abs}",
        "python3 scripts/aimf/hook_canary.py --source {source}",
        "python3 scripts/aimf/block_direct_atx.py",
    )
    workspace = {
        "version": "v1",
        "hooks": [
            {
                "name": f"hook {n}",
                "trigger": "PreToolUse",
                "matcher": regex,
                "action": {
                    "type": "command",
                    "command": c.format(source="workspace-hook"),
                },
            }
            for n, c in enumerate(commands)
        ],
    }
    agent = {
        "name": "migration",
        "hooks": {
            "userPromptSubmit": [{"command": "03-worklog/turn-log.sh"}],
            "preToolUse": [{"command": "03-worklog/work-declaration-guard.sh"}]
            + [
                {
                    "matcher": ["execute_bash", "shell", "use_aws", "aws"],
                    "command": c.format(source="agent-config"),
                }
                for c in commands
            ],
        },
    }
    return workspace, agent


def selftest() -> int:
    guard_abs = "/abs/scripts/guard_irreversible_ops.py"
    failures: list[str] = []

    good_ws, good_agent = _good_sites(guard_abs)
    if check_sites(good_ws, good_agent, guard_abs):
        failures.append("a fully-wired pair should pass")

    # Missing wiring: drop block_direct_atx.py from the workspace site.
    missing_ws = json.loads(json.dumps(good_ws))
    missing_ws["hooks"] = [
        h
        for h in missing_ws["hooks"]
        if "block_direct_atx" not in h["action"]["command"]
    ]
    if not check_sites(missing_ws, good_agent, guard_abs):
        failures.append("a missing hook should fail")

    # Missing wiring in the agent config: install.sh rewrote it and dropped our entries.
    rewritten_agent = json.loads(json.dumps(good_agent))
    rewritten_agent["hooks"]["preToolUse"] = rewritten_agent["hooks"]["preToolUse"][:1]
    if not check_sites(good_ws, rewritten_agent, guard_abs):
        failures.append("an agent config without our preToolUse entries should fail")

    # A hook under a trigger other than PreToolUse does not count as PreToolUse wiring.
    wrong_trigger_ws = json.loads(json.dumps(good_ws))
    for entry in wrong_trigger_ws["hooks"]:
        entry["trigger"] = "PostToolUse"
    if not check_sites(wrong_trigger_ws, good_agent, guard_abs):
        failures.append("a PostToolUse hook should not count as PreToolUse wiring")

    # Narrow matcher: agent config matcher only covers execute_bash.
    narrow_agent = json.loads(json.dumps(good_agent))
    for entry in narrow_agent["hooks"]["preToolUse"]:
        entry["matcher"] = ["execute_bash"]
    if not check_sites(good_ws, narrow_agent, guard_abs):
        failures.append("a matcher narrower than the Hub set should fail")

    # Guard referenced by a non-tracked path should fail.
    wrong_path_ws = json.loads(json.dumps(good_ws))
    for entry in wrong_path_ws["hooks"]:
        if "guard_irreversible_ops" in entry["action"]["command"]:
            entry["action"]["command"] = (
                "python3 /home/x/.kiro/guard_irreversible_ops.py"
            )
    if not check_sites(wrong_path_ws, good_agent, guard_abs):
        failures.append("guard referenced by an untracked path should fail")

    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(
        "selftest: wiring check passes a good pair and fails "
        "missing/rewritten/wrong-trigger/narrow/untracked-path"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", help="AIMF workspace directory")
    parser.add_argument(
        "--guard", help="absolute path to the tracked guard_irreversible_ops.py"
    )
    parser.add_argument(
        "--workspace-hook", help="path to the workspace .kiro/hooks/*.json"
    )
    parser.add_argument("--agent-config", help="path to .kiro/agents/migration.json")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()

    if not (args.workspace_hook and args.agent_config and args.guard):
        print(
            "check-hook-wiring: --workspace-hook, --agent-config and --guard are required",
            file=sys.stderr,
        )
        return 2
    workspace_site = json.loads(Path(args.workspace_hook).read_text(encoding="utf-8"))
    agent_site = json.loads(Path(args.agent_config).read_text(encoding="utf-8"))
    problems = check_sites(workspace_site, agent_site, args.guard)
    if args.workspace:
        problems += remote_problems(Path(args.workspace))
    if problems:
        print("check-hook-wiring: wiring is not complete:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        "check-hook-wiring: all three hooks wired at both sites with a wide-enough matcher"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
