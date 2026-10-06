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


def commands_of(site: dict) -> list[tuple[str, object]]:
    """Return (command_string, matcher) pairs from one wiring site.

    Accepts the workspace v1 shape {"hooks":[{"matcher":...,"command":...}]} and the agent-config
    shape {"preToolUse":[{"matcher":...,"command":...}]}; also tolerates a nested
    {"hooks":[{"type":"command","command":...}]}.
    """
    pairs: list[tuple[str, object]] = []
    for key in ("hooks", "preToolUse"):
        for entry in site.get(key, []):
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher")
            command = entry.get("command")
            if isinstance(command, str):
                pairs.append((command, matcher))
            # nested command list form
            for nested in (
                entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []
            ):
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
    regex = "^(execute_bash|shell|use_aws|aws)$"
    workspace = {
        "hooks": [
            {"matcher": regex, "command": f"python3 {guard_abs}"},
            {
                "matcher": regex,
                "command": "python3 scripts/aimf/hook_canary.py --source workspace-hook",
            },
            {"matcher": regex, "command": "python3 scripts/aimf/block_direct_atx.py"},
        ]
    }
    agent = {
        "preToolUse": [
            {
                "matcher": ["execute_bash", "shell", "use_aws", "aws"],
                "command": f"python3 {guard_abs}",
            },
            {
                "matcher": ["execute_bash", "shell", "use_aws", "aws"],
                "command": "python3 scripts/aimf/hook_canary.py --source agent-config",
            },
            {
                "matcher": ["execute_bash", "shell", "use_aws", "aws"],
                "command": "python3 scripts/aimf/block_direct_atx.py",
            },
        ]
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
        h for h in missing_ws["hooks"] if "block_direct_atx" not in h["command"]
    ]
    if not check_sites(missing_ws, good_agent, guard_abs):
        failures.append("a missing hook should fail")

    # Narrow matcher: agent config matcher only covers execute_bash.
    narrow_agent = json.loads(json.dumps(good_agent))
    for entry in narrow_agent["preToolUse"]:
        entry["matcher"] = ["execute_bash"]
    if not check_sites(good_ws, narrow_agent, guard_abs):
        failures.append("a matcher narrower than the Hub set should fail")

    # Guard referenced by a non-tracked path should fail.
    wrong_path_ws = json.loads(json.dumps(good_ws))
    for entry in wrong_path_ws["hooks"]:
        if "guard_irreversible_ops" in entry["command"]:
            entry["command"] = "python3 /home/x/.kiro/guard_irreversible_ops.py"
    if not check_sites(wrong_path_ws, good_agent, guard_abs):
        failures.append("guard referenced by an untracked path should fail")

    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(
        "selftest: wiring check passes a good pair and fails missing/narrow/untracked-path"
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
