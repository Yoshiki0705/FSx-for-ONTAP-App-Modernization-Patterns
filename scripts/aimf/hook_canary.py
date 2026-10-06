#!/usr/bin/env python3
"""Canary PreToolUse hook: prove which wiring site actually fires, and that a block takes effect.

Wiring that is only written to a file proves nothing was called. This hook, registered at both
wiring sites (the workspace .kiro/hooks/ and the agent config preToolUse), records every call so the
first AIMF session can confirm the hook is reached and that an exit-code-2 block stops a command.

On each call it appends one line to .private/aimf/hook-canary.log with --source, the UTC time and
the tool name read from the event. It exits 2 only when the command contains the harmless passphrase
`appmod-canary-block`; otherwise it exits 0. The passphrase is unrelated to any irreversible feature,
so routing it through a shell is harmless.

  hook_canary.py --source <workspace-hook|agent-config>   read a hook event on stdin
  hook_canary.py --selftest                               prove block/allow and that it logs
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG = ROOT / ".private" / "aimf" / "hook-canary.log"
BLOCK_PASSPHRASE = "appmod-canary-block"


def extract(event: dict) -> tuple[str, str]:
    """Return (tool_name, command) from whichever fields the host populated."""
    tool = event.get("tool_name") or event.get("tool") or "unknown"
    command = ""
    if isinstance(event.get("command"), str):
        command = event["command"]
    else:
        for container in ("tool_input", "params", "input"):
            inner = event.get(container)
            if isinstance(inner, dict) and isinstance(inner.get("command"), str):
                command = inner["command"]
                break
    return str(tool), command


def log_line(log_path: Path, source: str, tool: str, blocked: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    verdict = "block" if blocked else "allow"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now}\tsource={source}\ttool={tool}\tverdict={verdict}\n")


def handle_event(event: dict, source: str, log_path: Path) -> int:
    tool, command = extract(event)
    blocked = BLOCK_PASSPHRASE in command
    log_line(log_path, source, tool, blocked)
    if blocked:
        print(
            "hook_canary: canary block passphrase seen; stopping (exit 2)",
            file=sys.stderr,
        )
        return 2
    return 0


def selftest() -> int:
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as directory:
        log_path = Path(directory) / "canary.log"
        # ping: allowed, logged
        code = handle_event(
            {"tool_name": "execute_bash", "command": "echo appmod-canary-ping"},
            "workspace-hook",
            log_path,
        )
        if code != 0:
            failures.append("ping should exit 0")
        # block: blocked, logged
        code = handle_event(
            {"tool_name": "execute_bash", "command": "echo appmod-canary-block"},
            "agent-config",
            log_path,
        )
        if code != 2:
            failures.append("block passphrase should exit 2")
        lines = log_path.read_text(encoding="utf-8").splitlines()
        if len(lines) != 2:
            failures.append(f"expected 2 log lines, got {len(lines)}")
        if not any(
            "source=workspace-hook" in ln and "verdict=allow" in ln for ln in lines
        ):
            failures.append("ping line not logged from workspace-hook")
        if not any(
            "source=agent-config" in ln and "verdict=block" in ln for ln in lines
        ):
            failures.append("block line not logged from agent-config")
    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("selftest: canary logs both sources and blocks only the passphrase")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", default="unknown", help="which wiring site registered this hook"
    )
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="canary log path")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    return handle_event(event, args.source, Path(args.log))


if __name__ == "__main__":
    sys.exit(main())
