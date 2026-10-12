#!/usr/bin/env python3
"""Check that every make target a document names is a target the Makefile defines (stdlib only).

AGENTS.md told readers to run `make pr-verify PR=<n>` while no such target existed. A gate that
is only described never fails, so the description can drift from the Makefile unnoticed. This
check reads AGENTS.md and CONTRIBUTING.md and fails when either names a target the Makefile
does not define.

What counts as a mention: `make <name>` inside a fenced code block, or inside single backticks
in prose. Prose outside code ("make sure", "make a stub") is not read. <name> matches
[a-z][a-z0-9-]*, so `make $(X)` and `make -C dir` are ignored.

What counts as defined: a rule line `name:` or `a b:` in the Makefile. A name that appears only
in `.PHONY` is not defined; it is reported as a warning so the Makefile stays honest, but it
does not fail the check and does not make a mention pass.

A file that does not exist is skipped with a message.

    python3 tools/check_make_targets.py --selftest   # the check can fail
    python3 tools/check_make_targets.py              # this repository
    python3 tools/check_make_targets.py --root DIR   # Makefile and documents under DIR

Exit codes: 0 every mention is defined, 1 at least one is missing, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

DOCS = ("AGENTS.md", "CONTRIBUTING.md")

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
MENTION = re.compile(r"(?<![\w-])make\s+([a-z][a-z0-9-]*)\b")
# `a b: deps` defines a and b. `X := v`, `X = v`, `.PHONY:` and `define` do not match.
RULE = re.compile(r"^([a-z][a-z0-9_-]*(?:[ \t]+[a-z][a-z0-9_-]*)*)[ \t]*:(?!=)")
PHONY = re.compile(r"^\.PHONY[ \t]*:(.*)$")


def makefile_targets(text: str) -> set[str]:
    """Names that have a rule line."""
    found: set[str] = set()
    for line in text.splitlines():
        m = RULE.match(line)
        if m:
            found.update(m.group(1).split())
    return found


def phony_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        m = PHONY.match(line)
        if m:
            names.update(m.group(1).split())
    return names


def phony_without_rule(text: str) -> set[str]:
    return phony_names(text) - makefile_targets(text)


def mentioned_targets(md_text: str) -> dict[str, list[int]]:
    """target -> line numbers of `make <target>` inside code. Prose outside code is ignored."""
    found: dict[str, list[int]] = {}
    in_fence = False
    for number, line in enumerate(md_text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        spans = [line] if in_fence else INLINE_CODE.findall(line)
        for span in spans:
            for m in MENTION.finditer(span):
                found.setdefault(m.group(1), []).append(number)
    return found


def check(root: Path) -> tuple[int, list[str]]:
    """Return (exit code, output lines)."""
    makefile = root / "Makefile"
    if not makefile.is_file():
        return 2, [f"drift: {makefile} not found"]
    make_text = makefile.read_text(encoding="utf-8")
    defined = makefile_targets(make_text)
    lines: list[str] = []
    missing: list[str] = []
    references = 0
    files = 0
    for doc in DOCS:
        path = root / doc
        if not path.is_file():
            lines.append(f"drift: {doc}: not present, skipped")
            continue
        files += 1
        for target, numbers in sorted(
            mentioned_targets(path.read_text(encoding="utf-8")).items()
        ):
            references += len(numbers)
            if target not in defined:
                for number in numbers:
                    missing.append(
                        f"drift: {doc}:{number}: make {target} is not a target in Makefile"
                    )
    for name in sorted(phony_without_rule(make_text)):
        lines.append(f"drift: warning: .PHONY names {name} but no rule defines it")
    if missing:
        return 1, lines + missing
    lines.append(
        f"drift: {references} make target reference(s) in {files} file(s), all defined"
    )
    return 0, lines


SELFTEST_MAKEFILE = """\
RUFF := ruff
.PHONY: all lint ghost
all: lint
lint a-b: ## two targets on one rule
\t@true
"""

SELFTEST_DOC = """\
Run `make all` and `make a-b`; then make sure nothing else is needed.

```bash
make lint       # defined
make nope       # missing
make -C sub
```

Prose such as make a stub is not a mention. `make ghost` is only in .PHONY.
"""


def selftest() -> int:
    problems: list[str] = []
    if makefile_targets(SELFTEST_MAKEFILE) != {"all", "lint", "a-b"}:
        problems.append(
            f"makefile_targets: {sorted(makefile_targets(SELFTEST_MAKEFILE))}"
        )
    if phony_without_rule(SELFTEST_MAKEFILE) != {"ghost"}:
        problems.append("phony_without_rule did not report ghost")
    mentions = mentioned_targets(SELFTEST_DOC)
    if set(mentions) != {"all", "a-b", "lint", "nope", "ghost"}:
        problems.append(f"mentioned_targets: {sorted(mentions)}")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "Makefile").write_text(SELFTEST_MAKEFILE, encoding="utf-8")
        (root / "AGENTS.md").write_text(SELFTEST_DOC, encoding="utf-8")
        code, out = check(root)
        text = "\n".join(out)
        if code != 1 or "make nope" not in text or "make ghost" not in text:
            problems.append(f"fixture should fail on nope and ghost: {code} {text}")
        (root / "AGENTS.md").write_text("Run `make all`.\n", encoding="utf-8")
        code, _ = check(root)
        if code != 0:
            problems.append("fixture with only defined targets should pass")
    for problem in problems:
        print(f"selftest FAIL: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("selftest: passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    code, lines = check(args.root)
    for line in lines:
        print(line, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
