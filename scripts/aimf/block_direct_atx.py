#!/usr/bin/env python3
"""PreToolUse hook: block a direct `atx` invocation that does not go through run-atx.sh.

AWS Transform custom (the AIMF tool invoked as `atx`) starts a billed inference run and sends code
to it. The design requires every run to pass the shared entry check and the gitleaks send-scan in
scripts/aimf/run-atx.sh. AIMF Phase 0a otherwise runs `atx` directly, which would send code before
the task-4.2.2 approval. This hook stops that.

Reads the Kiro hook input JSON on stdin; the tool command is read from input.command,
input.tool_input.command or params.command (whichever the host supplies). It:

  - exits 2 (block) when the command starts with `atx` and does not route through run-atx.sh;
  - exits 0 (allow) otherwise, including when run-atx.sh is the thing invoking atx.

The Hub irreversible-ops guard only looks at WORM features, so it does not stop `atx`; this hook is
the atx-specific layer. Hub's guard_irreversible_ops.py is not edited.

  block_direct_atx.py            read a hook event on stdin
  block_direct_atx.py --selftest prove it blocks a direct atx and allows a run-atx.sh invocation
"""

from __future__ import annotations

import json
import re
import sys

# Command tokens that count as a direct atx call. `atx ...` as the first token, or an absolute path
# ending in /atx. A command that mentions run-atx.sh is allowed (that is the sanctioned entry).
DIRECT_ATX = re.compile(r"(?:^|[;&|]\s*)(?:\S*/)?atx(?:\s|$)")
ROUTED = re.compile(r"run-atx\.sh")


def extract_command(event: dict) -> str:
    """Pull the shell command out of whichever field the host populated."""
    for key in ("command",):
        if isinstance(event.get(key), str):
            return event[key]
    for container in ("tool_input", "params", "input"):
        inner = event.get(container)
        if isinstance(inner, dict) and isinstance(inner.get("command"), str):
            return inner["command"]
    return ""


def is_direct_atx(command: str) -> bool:
    if ROUTED.search(command):
        return False
    return bool(DIRECT_ATX.search(command))


def selftest() -> int:
    cases = [
        ("atx run --project DocIntake", True),
        ("/usr/local/bin/atx analyze", True),
        ("cd /tmp && atx run", True),
        (
            "bash scripts/aimf/run-atx.sh --estimate e.json --approved-at 2026-10-07T00:00:00Z",
            False,
        ),
        ("APPMOD_DRY_RUN=1 bash run-atx.sh --estimate e.json", False),
        ("echo atx", False),  # 'atx' is an argument to echo, not the command
        ("aws fsx describe-volumes", False),
        ("atxtool run", False),  # different binary name
    ]
    failures = []
    for command, want_block in cases:
        got_block = is_direct_atx(command)
        if got_block != want_block:
            failures.append(
                f"{command!r}: expected block={want_block}, got {got_block}"
            )
    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"selftest: {len(cases)} case(s) passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # A malformed event is not an atx call; do not block ordinary work on a parse error.
        return 0
    command = extract_command(event)
    if is_direct_atx(command):
        print(
            "block_direct_atx: direct `atx` is blocked; run it through scripts/aimf/run-atx.sh "
            "after the task-4.2.2 approval",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
