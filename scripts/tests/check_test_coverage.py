#!/usr/bin/env python3
"""Fail when a test file on disk is not reached by the Makefile `test` target (stdlib only).

A test that nothing runs is indistinguishable from a test that passes. This enumerates the test
files under the known test locations and checks that each is referenced in the `test:` recipe of the
Makefile, either by path (shell tests, guard negatives) or by its unittest dotted-module name
(test_*.py). It fails if any is unreferenced.

Covered test shapes:
  scripts/tests/test_*.py        -> unittest module scripts.tests.test_*
  scripts/tests/*_tests.sh       -> referenced by path
  scripts/tests/check_*.py       -> referenced by path (repository-wide checkers, this one included)
  guard/tests/run_*.py           -> referenced by path

Candidate app tests (app/candidate-*/tests/test_*.py, app/candidate-*/src/test/**/*.java) are checked
separately: each candidate directory holding tests must be named in a Makefile recipe whose target
`ci` depends on (`app-candidate*`), so a candidate test that no gate runs fails here.
Run from the repository root:  python3 scripts/tests/check_test_coverage.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MAKEFILE = ROOT / "Makefile"


def test_recipe() -> str:
    """Return the body of the `test:` target recipe (the indented lines after `test:`)."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    recipe: list[str] = []
    in_recipe = False
    for line in lines:
        if re.match(r"^test:", line):
            in_recipe = True
            continue
        if in_recipe:
            if line.startswith("\t") or line.strip() == "":
                recipe.append(line)
            else:
                break
    return "\n".join(recipe)


def discover() -> list[tuple[Path, str]]:
    """Return (path, reference_token) for each test file that must be referenced."""
    found: list[tuple[Path, str]] = []
    for path in sorted((ROOT / "scripts" / "tests").glob("test_*.py")):
        module = "scripts.tests." + path.stem
        found.append((path, module))
    for path in sorted((ROOT / "scripts" / "tests").glob("*_tests.sh")):
        found.append((path, str(path.relative_to(ROOT))))
    for path in sorted((ROOT / "scripts" / "tests").glob("check_*.py")):
        found.append((path, str(path.relative_to(ROOT))))
    for path in sorted((ROOT / "guard" / "tests").glob("run_*.py")):
        found.append((path, str(path.relative_to(ROOT))))
    return found


def candidate_gaps() -> tuple[int, list[str]]:
    """Return (candidate test dirs found, descriptions of those no ci-reachable target runs)."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    ci_line = next((x for x in lines if re.match(r"^ci:", x)), "")
    ci_deps = set(ci_line.split("##")[0].split(":", 1)[1].split())
    # target name -> recipe text
    recipes: dict[str, str] = {}
    current = None
    for line in lines:
        m = re.match(r"^([a-zA-Z0-9_-]+):", line)
        if m:
            current = m.group(1)
            recipes[current] = ""
        elif current and line.startswith("\t"):
            recipes[current] += line + "\n"
    reached = "\n".join(
        r for t, r in recipes.items() if t in ci_deps and t.startswith("app-candidate")
    )
    found = 0
    gaps: list[str] = []
    for cand in sorted((ROOT / "app").glob("candidate-*")):
        has_py = any((cand / "tests").glob("test_*.py"))
        has_java = any((cand / "src" / "test").rglob("*.java"))
        if not (has_py or has_java):
            continue
        found += 1
        rel = cand.relative_to(ROOT).as_posix()
        # The reference must be the test location itself: a requirements-dev.txt path does not run tests.
        needle = rel + ("/tests" if has_py else "/pom.xml")
        if needle not in reached:
            gaps.append(rel)
    return found, gaps


def main() -> int:
    cand_found, cand_gaps = candidate_gaps()
    for gap in cand_gaps:
        print(
            f"check_test_coverage: {gap} has tests but no app-candidate* target in `ci` runs them",
            file=sys.stderr,
        )
    if cand_gaps:
        return 1
    print(
        f"check_test_coverage: {cand_found} candidate test dir(s) are reached by `ci`"
    )
    recipe = test_recipe()
    if not recipe.strip():
        print("check_test_coverage: could not find the test: recipe", file=sys.stderr)
        return 1
    # unittest modules may be listed in a variable (PY_UNITTEST) that the recipe expands, so the
    # reference may live anywhere in the Makefile, not only in the literal recipe body. Shell and
    # guard tests are invoked by path in the recipe itself.
    whole = MAKEFILE.read_text(encoding="utf-8")
    missing: list[str] = []
    checked = 0
    for path, token in discover():
        checked += 1
        # A unittest module must be reached by the test recipe: it is referenced in PY_UNITTEST
        # (anywhere in the Makefile) AND that variable must be used in the recipe. A path-based test
        # must appear in the recipe body directly.
        if token.startswith("scripts.tests."):
            referenced = token in whole and "PY_UNITTEST" in recipe
        else:
            referenced = token in recipe
        if not referenced:
            missing.append(f"{path.relative_to(ROOT)} (expected reference {token!r})")
    for item in missing:
        print(
            f"check_test_coverage: not registered in `make test`: {item}",
            file=sys.stderr,
        )
    if missing:
        return 1
    print(
        f"check_test_coverage: all {checked} test file(s) are registered in `make test`"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
