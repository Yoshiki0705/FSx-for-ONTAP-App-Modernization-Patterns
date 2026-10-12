#!/usr/bin/env python3
"""Report whether CI passed for the commit a pull request will merge (stdlib only, shells out to gh).

`gh pr checks` shows the latest results, so right after a push it can return the previous
commit's verdict with nothing marking it stale. This tool keys every lookup on the head SHA:
it reads the SHA from the PR, lists the workflow runs for exactly that SHA, and requires every
workflow that triggers on pull_request to have a completed run with conclusion success.
A workflow with no run for the SHA has not started and counts as a failure.

Exit codes
  0  every expected workflow completed with conclusion success for the head SHA
  1  a run failed, an expected workflow has no run for the SHA, a gh call failed, or bad usage
  2  nothing failed, but at least one run has not completed yet (the names are printed)

--wait SECONDS polls every 30 seconds. A completed non-success run ends the wait at once.
A workflow with no run yet, or a run still going, keeps the wait alive until the deadline;
at the deadline a missing workflow gives 1 and a running one gives 2. Each poll re-reads the
head SHA, so a push during the wait is judged on the new commit.

A workflow whose pull_request trigger has a `paths:` filter does not run when no changed file
matches. It is required only when a changed file matches, or when a run for the SHA exists.

Run:  python3 tools/pr_verify.py --pr 123 [--wait 600]    (or: make pr-verify PR=123)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
POLL_SECONDS = 30
GH_FILES_CAP = 100  # `gh pr view --json files` returns at most this many paths

Gh = Callable[[list[str]], object]


class PrVerifyError(Exception):
    """A gh call failed or returned something this tool cannot read."""


def gh_json(args: list[str]) -> object:
    """The only place that runs gh. Tests replace it with a function returning canned JSON."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=ROOT,
        )
    except FileNotFoundError as exc:
        raise PrVerifyError("gh is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise PrVerifyError(f"gh {' '.join(args[:2])} timed out") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        raise PrVerifyError(
            f"gh {' '.join(args[:2])} failed: {detail[0] if detail else proc.returncode}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PrVerifyError(f"gh {' '.join(args[:2])} did not return JSON") from exc


@dataclass(frozen=True)
class Workflow:
    name: str
    paths: (
        tuple[str, ...] | None
    )  # None: no path filter, or one this tool cannot evaluate


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _list_items(inline: str) -> list[str]:
    inner = inline.strip()
    if inner.startswith("["):
        inner = inner.rstrip()
        inner = inner[1 : inner.index("]")] if "]" in inner else inner[1:]
    return [_unquote(p) for p in inner.split(",") if p.strip()]


def parse_workflow(text: str, rel_path: str) -> Workflow | None:
    """Return the workflow when it triggers on pull_request, else None. Regex based, no yaml."""
    lines = [ln.split(" #")[0].rstrip() for ln in text.splitlines()]
    name = rel_path
    for ln in lines:
        m = re.match(r"^name:\s*(.+)$", ln)
        if m:
            name = _unquote(m.group(1))
            break
    start = next(
        (i for i, ln in enumerate(lines) if re.match(r"""^(on|"on"|'on'):""", ln)), None
    )
    if start is None:
        return None
    head = re.match(r"""^(?:on|"on"|'on'):\s*(.*)$""", lines[start]).group(1).strip()
    if head:  # `on: pull_request` or `on: [push, pull_request]`
        events = _list_items(head) if head.startswith("[") else [head]
        return Workflow(name, None) if "pull_request" in events else None
    block = []
    for ln in lines[start + 1 :]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        block.append(ln)
    indent = next((len(ln) - len(ln.lstrip()) for ln in block if ln.strip()), 0)
    event_at = next(
        (
            i
            for i, ln in enumerate(block)
            if re.match(rf"^ {{{indent}}}pull_request:", ln)
        ),
        None,
    )
    if event_at is None:
        return None
    sub = []
    for ln in block[event_at + 1 :]:
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break
        sub.append(ln)
    if any(re.match(r"^\s*paths-ignore:", ln) for ln in sub):
        return Workflow(name, None)
    for i, ln in enumerate(sub):
        m = re.match(r"^(\s*)paths:\s*(.*)$", ln)
        if not m:
            continue
        if m.group(2):
            return Workflow(name, tuple(_list_items(m.group(2))))
        item_indent = len(m.group(1))
        items = []
        for nxt in sub[i + 1 :]:
            mi = re.match(r"^\s*-\s*(.+)$", nxt)
            if not mi or (len(nxt) - len(nxt.lstrip())) < item_indent:
                break
            items.append(_unquote(mi.group(1)))
        return Workflow(name, tuple(items))
    return Workflow(name, None)


def expected_workflows(workflows_dir: Path) -> list[Workflow]:
    found = []
    for path in sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")]):
        wf = parse_workflow(
            path.read_text(encoding="utf-8"), f".github/workflows/{path.name}"
        )
        if wf is not None:
            found.append(wf)
    return found


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_filter_matches(paths: tuple[str, ...], changed: list[str]) -> bool:
    """True when the workflow would be triggered by these changes (or when that cannot be told)."""
    if any(p.startswith("!") for p in paths):
        return True
    regexes = [_glob_regex(p) for p in paths]
    return any(r.match(f) for r in regexes for f in changed)


@dataclass
class Report:
    sha: str
    states: list[tuple[str, str, str]]  # (workflow name, state, detail)

    @property
    def hard_failure(self) -> bool:
        return any(s == "failure" for _, s, _ in self.states)

    @property
    def code(self) -> int:
        states = {s for _, s, _ in self.states}
        if "failure" in states or "missing" in states:
            return 1
        if "running" in states:
            return 2
        return 0


def evaluate(pr: int, gh: Gh, expected: list[Workflow]) -> Report:
    view = gh(["pr", "view", str(pr), "--json", "headRefOid,files"])
    if not isinstance(view, dict) or not view.get("headRefOid"):
        raise PrVerifyError("gh pr view returned no headRefOid")
    sha = str(view["headRefOid"])
    changed = [str(f.get("path", "")) for f in view.get("files") or []]
    capped = len(changed) >= GH_FILES_CAP
    listing = gh(
        [
            "api",
            "-X",
            "GET",
            "repos/{owner}/{repo}/actions/runs",
            "-f",
            f"head_sha={sha}",
            "-f",
            "per_page=100",
        ]
    )
    if not isinstance(listing, dict) or not isinstance(
        listing.get("workflow_runs"), list
    ):
        raise PrVerifyError("gh api actions/runs returned no workflow_runs")
    latest: dict[str, dict] = {}
    for run in listing["workflow_runs"]:
        if run.get("head_sha") != sha or run.get("event") != "pull_request":
            continue
        key = (run.get("run_number") or 0, run.get("run_attempt") or 0)
        prev = latest.get(run.get("name"))
        if prev is None or key > (
            prev.get("run_number") or 0,
            prev.get("run_attempt") or 0,
        ):
            latest[run.get("name")] = run
    states = []
    for wf in expected:
        run = latest.get(wf.name)
        if run is None:
            if (
                wf.paths is not None
                and not capped
                and not path_filter_matches(wf.paths, changed)
            ):
                states.append(
                    (wf.name, "skipped-by-filter", "not required (path filter)")
                )
            else:
                states.append(
                    (wf.name, "missing", "no run for this commit (not started)")
                )
        elif run.get("status") != "completed":
            states.append((wf.name, "running", f"running ({run.get('status')})"))
        elif run.get("conclusion") == "success":
            states.append((wf.name, "success", "success"))
        else:
            states.append((wf.name, "failure", f"failure ({run.get('conclusion')})"))
    return Report(sha, states)


def format_report(pr: int, report: Report) -> str:
    verdict = {0: "PASS", 1: "FAIL", 2: "PENDING"}[report.code]
    lines = [f"pr-verify: PR #{pr} head {report.sha[:7]}"]
    lines += [f"  {name}: {detail}" for name, _, detail in report.states]
    waiting = [n for n, s, _ in report.states if s == "running"]
    if report.code == 2:
        lines.append(f"pr-verify: still running: {', '.join(waiting)}")
    lines.append(f"pr-verify: {verdict}")
    return "\n".join(lines)


def verify(
    pr: int,
    *,
    gh: Gh = gh_json,
    workflows_dir: Path = WORKFLOWS_DIR,
    wait: int = 0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    expected = expected_workflows(workflows_dir)
    if not expected:
        print(
            f"pr-verify: no pull_request workflow found under {workflows_dir}",
            file=sys.stderr,
        )
        return 1
    deadline = clock() + wait
    last_sha = None
    while True:
        try:
            report = evaluate(pr, gh, expected)
        except PrVerifyError as exc:
            print(f"pr-verify: error: {exc}", file=sys.stderr)
            return 1
        if last_sha and report.sha != last_sha:
            print(f"pr-verify: head moved {last_sha[:7]} -> {report.sha[:7]}")
        last_sha = report.sha
        print(format_report(pr, report))
        if report.code == 0 or report.hard_failure or clock() >= deadline:
            return report.code
        sleep(POLL_SECONDS)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 2 is reserved for "still running"
        self.print_usage(sys.stderr)
        print(f"pr-verify: {message}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="CI verdict for a pull request's head SHA")
    parser.add_argument("--pr", required=True, help="pull request number")
    parser.add_argument(
        "--wait", type=int, default=0, help="poll for up to this many seconds"
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]+", args.pr) or args.wait < 0:
        parser.error("--pr must be a number and --wait must not be negative")
    return verify(int(args.pr), wait=args.wait)


if __name__ == "__main__":
    sys.exit(main())
