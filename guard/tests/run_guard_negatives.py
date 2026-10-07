#!/usr/bin/env python3
"""Negative tests for guard/appmod.guard: each fixture must fail its own rule and no other.

cfn-guard proves a rule rejects a bad template only if a bad template is run through it. For each
guard/tests/fail_<rule>.yaml, this runs `cfn-guard validate` against guard/ and asserts the FAILED
rules are exactly {<rule>}. It fails if a fixture passes, or fails extra rules, or fails a different
rule than its name claims.

Requires the cfn-guard binary (the same one `make cfn` uses). Run from the repository root:

  python3 guard/tests/run_guard_negatives.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_DIR = ROOT / "guard" / "tests"
RULES_DIR = ROOT / "guard"

# Rule names may contain digits (s3_scoped_resource); [a-z_]+ would silently read such a FAIL line
# as "failed nothing".
FAIL_LINE = re.compile(r"appmod\.guard/([a-z0-9_]+)\s+FAIL")


def failed_rules(fixture: Path) -> set[str]:
    result = subprocess.run(
        [
            "cfn-guard",
            "validate",
            "--data",
            str(fixture),
            "--rules",
            str(RULES_DIR),
            "--output-format",
            "single-line-summary",
            "--show-summary",
            "fail",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,  # a failing fixture is the expected case; we read the summary, not the code
    )
    output = result.stdout + result.stderr
    return {match.group(1) for match in FAIL_LINE.finditer(output)}


def main() -> int:
    if not any(TESTS_DIR.glob("fail_*.yaml")):
        print("run_guard_negatives: no fixtures found", file=sys.stderr)
        return 1
    failures: list[str] = []
    checked = 0
    for fixture in sorted(TESTS_DIR.glob("fail_*.yaml")):
        expected = fixture.stem[len("fail_") :]
        got = failed_rules(fixture)
        checked += 1
        if got != {expected}:
            failures.append(
                f"{fixture.name}: expected to fail only {{{expected}}}, failed {got or 'nothing'}"
            )
    for failure in failures:
        print(f"run_guard_negatives: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"run_guard_negatives: {checked} fixture(s) each fail exactly their own rule")
    return 0


if __name__ == "__main__":
    sys.exit(main())
