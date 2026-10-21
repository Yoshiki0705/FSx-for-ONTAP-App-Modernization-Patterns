#!/usr/bin/env python3
"""Protect the strings other repositories pin inside this repository's files (stdlib only).

Another repository's gate can depend on one sentence in one of our documents. Reword that sentence
and their gate fails on a tree we never see, and it reads there as a retraction of a claim rather
than as an edit here. This check reads docs/agent/inbound-probe-contract.txt, the strings sibling
repositories published as pinned here, and fails unless each occurs exactly once, in body text, in
the file it is pinned to. The rule is the Hub's; tools/probe_strength.py is a byte-identical copy.

  absent      their gate fails, and it reads as a retraction of the claim
  duplicated  their gate stays green while one copy can be reworded, so the probe guards nothing
  heading     only a heading matches: the section keeps its title while the content is replaced

Writing prose about a pinned string is the ordinary way to create a second copy. Describe it
instead of quoting it.

Coverage is a floor, not a ceiling. --refresh reads the contracts siblings publish, so a probe
nobody published is not in the file. A sibling that publishes none is recorded as unknown.

    python3 tools/check_inbound_probes.py --selftest   # the check can fail
    python3 tools/check_inbound_probes.py              # offline; this repository
    python3 tools/check_inbound_probes.py --refresh    # network (gh); rewrites the contract
    python3 tools/check_inbound_probes.py --root DIR   # contract and documents under DIR

--refresh stops without writing when a sibling's answer is undetermined (rate limit, 5xx, timeout,
no gh). Only a 404 means "publishes no contract". Recording a failed fetch as "publishes none"
would delete that sibling's rows from the file and read as a withdrawal.

Exit codes: 0 every pinned string is present exactly once, 1 at least one problem, 2 usage or
I/O error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from probe_strength import strip_code, verdict

OWNER = "Yoshiki0705"
THIS_REPO = "FSx-for-ONTAP-App-Modernization-Patterns"
# Where a sibling publishes the strings it pins elsewhere. The convention travels with the Hub's tool.
SIBLING_CONTRACT = "docs/agent/cross-repo-probe-contract.txt"
CONTRACT = "docs/agent/inbound-probe-contract.txt"
UNKNOWN_MARKER = (
    "# unknown (publishes no contract; absence of a contract is not absence of probes):"
)
REPO_REF = re.compile(rf"https://github\.com/{OWNER}/(?P<repo>[A-Za-z0-9._-]+)")

HEADER = """\
# Probe strings that other repositories pin inside THIS repository's files. Generated - do not
# hand-edit.
#
# Refresh with: python3 tools/check_inbound_probes.py --refresh   (make inbound-probes-refresh)
#
# Why this file is load-bearing: each string below is an interface. Editing the line it sits on
# breaks a gate in the repository named in the first column, and nothing in that repository can
# warn us first - the check runs there, against a tree they do not have.
#
# The gate here enforces more than presence. A string must occur EXACTLY ONCE, in body text:
#   - zero occurrences  their gate fails, and reads as a retraction rather than as our edit
#   - two or more       their gate stays green while one copy can be reworded, so it guards nothing
#   - heading only      the section keeps its title while the content is replaced
#
# Writing prose about a pinned string is the ordinary way to create a second copy. Describe it
# instead of quoting it. To change a pinned sentence, keep the quoted substring or tell the
# repository in the first column so it can re-pin, then refresh this file.
#
# COVERAGE IS A FLOOR, NOT A CEILING. Discovery reads each sibling's published contract. Siblings
# that publish none are listed at the end as unknown. A probe nobody published cannot be found here.
#
# role: retraction = expected to survive; reread = quotes a min/max, so firing means "read again".
#
# Format: <citing repo> TAB <our path> TAB <role> TAB <probe>, sorted.
"""

EXPLANATION = {
    "absent": (
        "and it is gone. Their gate will read this as a retraction of the claim, not as an edit "
        "on our side"
    ),
    "heading-only": (
        "and it now matches only a heading, so their probe survives the section being replaced. "
        "Keep the claim in body text"
    ),
    "duplicated": (
        "more than once, so one copy can be reworded with their gate still green. Describe the "
        "string instead of quoting it"
    ),
}


class Row(NamedTuple):
    citing: str
    path: str
    role: str
    probe: str

    def line(self) -> str:
        return "\t".join(self)


class FetchError(Exception):
    """A sibling's answer was undetermined: nothing was learned about whether it publishes."""


Fetch = Callable[[str, str], str | None]


def parse_contract(
    text: str, name: str = CONTRACT
) -> tuple[list[Row], list[str], list[str]]:
    """Return (rows, unknown repos, problems)."""
    rows: list[Row] = []
    unknown: list[str] = []
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith(UNKNOWN_MARKER):
            unknown = raw[len(UNKNOWN_MARKER) :].split()
            continue
        if raw.startswith("#") or not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) != 4 or not all(parts):
            problems.append(
                f"{name}:{number}: expected 4 non-empty tab-separated fields, got {len(parts)}"
            )
            continue
        rows.append(Row(*parts))
    return rows, unknown, problems


def check(root: Path) -> tuple[int, list[str]]:
    """Return (exit code, output lines)."""
    contract = root / CONTRACT
    if not contract.is_file():
        return 1, [
            (
                f"inbound probes: {CONTRACT} is missing. Run "
                "`python3 tools/check_inbound_probes.py --refresh` to create it"
            )
        ]
    rows, unknown, problems = parse_contract(contract.read_text(encoding="utf-8"))
    for row in rows:
        target = root / row.path
        if not target.is_file():
            problems.append(
                f"{row.path}: does not exist, but {row.citing} pins {row.probe!r} in it. "
                "Moving or deleting the file breaks their gate; tell them before doing it"
            )
            continue
        try:
            body = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{row.path}: cannot be read ({exc})")
            continue
        result = verdict(row.probe, body)
        if result != "ok":
            problems.append(
                f"{row.path}: {row.citing} pins {row.probe!r} here {EXPLANATION[result]}"
            )
    if problems:
        return 1, [f"inbound probes failed ({len(problems)} issue(s)):"] + [
            f"  {p}" for p in problems
        ]
    note = (
        f"; {len(unknown)} sibling(s) publish no contract, so coverage is a floor"
        if unknown
        else ""
    )
    return 0, [
        f"inbound probes: {len(rows)} pinned string(s) present exactly once in body text{note}"
    ]


def repos_in(texts: Iterable[str]) -> set[str]:
    """Sibling repositories named by URL in prose. Fenced code is ignored, as it is for citations."""
    found: set[str] = set()
    for text in texts:
        for match in REPO_REF.finditer(strip_code(text)):
            repo = match.group("repo").rstrip(".").removesuffix(".git").rstrip(".")
            if repo and repo != THIS_REPO:
                found.add(repo)
    return found


def tracked_markdown(root: Path) -> list[str]:
    """Text of every tracked Markdown file (git, so .worktrees/ and .venv/ copies are not read)."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        (root / name).read_text(encoding="utf-8")
        for name in listing.stdout.split("\0")
        if name and (root / name).is_file()
    ]


def gh_fetch(repo: str, path: str) -> str | None:
    """Body of a sibling's file; None when it does not exist; FetchError when undetermined."""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{OWNER}/{repo}/contents/{path}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FetchError(f"{repo}: gh could not run ({exc})") from exc
    if result.returncode == 0:
        return result.stdout
    if "HTTP 404" in result.stderr:
        return None
    raise FetchError(f"{repo}: {result.stderr.strip() or 'gh api failed'}")


def published_rows(body: str, citing: str) -> list[Row]:
    """Rows a sibling's contract pins in THIS repository: <repo> TAB <path> TAB <role> TAB <probe>."""
    rows: list[Row] = []
    for raw in body.splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) == 4 and parts[0] == THIS_REPO:
            rows.append(Row(citing, parts[1], parts[2], parts[3]))
    return rows


def collect(siblings: Iterable[str], fetch: Fetch) -> tuple[list[Row], list[str]]:
    """Rows pinned here by each sibling, and the siblings that publish no contract."""
    rows: list[Row] = []
    unknown: list[str] = []
    for repo in sorted(siblings):
        body = fetch(repo, SIBLING_CONTRACT)
        if body is None:
            unknown.append(repo)
            continue
        rows.extend(published_rows(body, repo))
    return rows, unknown


def render(rows: Iterable[Row], unknown: Iterable[str]) -> str:
    lines = sorted({row.line() for row in rows})
    body = HEADER + "\n".join(lines) + ("\n" if lines else "")
    return body + f"\n{UNKNOWN_MARKER} " + " ".join(sorted(unknown)) + "\n"


def refresh(root: Path, fetch: Fetch = gh_fetch) -> int:
    siblings = repos_in(tracked_markdown(root))
    try:
        rows, unknown = collect(siblings, fetch)
    except FetchError as exc:
        print(
            f"inbound probes: refresh stopped, nothing written ({exc})", file=sys.stderr
        )
        return 2
    body = render(rows, unknown)
    path = root / CONTRACT
    previous = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(
        f"inbound probes: {len(set(rows))} pinned into this repository by "
        f"{len({row.citing for row in rows})} repositor(ies); {len(unknown)} sibling(s) publish "
        "no contract and are recorded as unknown"
    )
    if previous != body:
        print("  the contract changed; review the diff before committing")
    return 0


SELFTEST_CONTRACT = HEADER + (
    "Hub\tdoc-ok.md\tretraction\tPIN-OK\n"
    "Hub\tdoc-gone.md\tretraction\tPIN-GONE\n"
    "Hub\tdoc-twice.md\tretraction\tPIN-TWICE\n"
    "Hub\tdoc-heading.md\tretraction\tPIN-HEAD\n"
    "Hub\tdoc-fenced.md\treread\tPIN-FENCED\n"
    "Hub\tno-such-file.md\tretraction\tPIN-MISSING\n"
)
SELFTEST_DOCS = {
    "doc-ok.md": "# T\n\nbody says PIN-OK once.\n",
    "doc-gone.md": "# T\n\nthe sentence was reworded.\n",
    "doc-twice.md": "# T\n\nPIN-TWICE here.\n\nand PIN-TWICE again.\n",
    "doc-heading.md": "# PIN-HEAD\n\nthe claim was replaced.\n",
    "doc-fenced.md": "# T\n\n```\nPIN-FENCED\n```\n",
}


def selftest() -> int:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "docs" / "agent").mkdir(parents=True)
        (root / CONTRACT).write_text(SELFTEST_CONTRACT, encoding="utf-8")
        for name, text in SELFTEST_DOCS.items():
            (root / name).write_text(text, encoding="utf-8")
        code, lines = check(root)
        out = "\n".join(lines)
        if code != 1:
            problems.append(f"fixture should fail: {code}")
        for needle in (
            "PIN-GONE",
            "PIN-TWICE",
            "PIN-HEAD",
            "PIN-FENCED",
            "no-such-file.md: does not exist",
        ):
            if needle not in out:
                problems.append(f"fixture failure does not name {needle}")
        if "PIN-OK" in out:
            problems.append("a string present exactly once was reported")
        (root / CONTRACT).write_text(
            HEADER + "Hub\tdoc-ok.md\tretraction\tPIN-OK\n", encoding="utf-8"
        )
        code, _ = check(root)
        if code != 0:
            problems.append("a string present exactly once should pass")
    rows = published_rows(
        f"# c\n{THIS_REPO}\ta.md\tretraction\tP1\nOther\tb.md\tretraction\tP2\nbad line\n",
        "Hub",
    )
    if rows != [Row("Hub", "a.md", "retraction", "P1")]:
        problems.append(f"published_rows: {rows}")
    refs = repos_in(
        [
            (
                f"[a](https://github.com/{OWNER}/Alpha). "
                f"[b](https://github.com/{OWNER}/Beta.git) "
                f"[c](https://github.com/{OWNER}/{THIS_REPO})\n"
                f"```\nhttps://github.com/{OWNER}/InFence\n```\n"
            )
        ]
    )
    if refs != {"Alpha", "Beta"}:
        problems.append(f"repos_in: {sorted(refs)}")
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
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-read each sibling's published contract and rewrite the contract (network)",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.refresh:
        return refresh(args.root)
    code, lines = check(args.root)
    for line in lines:
        print(line, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
