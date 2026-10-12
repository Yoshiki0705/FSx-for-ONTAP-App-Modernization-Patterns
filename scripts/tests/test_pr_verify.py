#!/usr/bin/env python3
"""tools/pr_verify.py judges CI on the head SHA, never on the latest results (stdlib only).

Every gh call goes through one seam, so these tests hand it canned JSON. The fake answers the
run listing by filtering a prepared run list on the head_sha it is asked for, which proves the
lookup is keyed on the PR's head SHA. No network and no gh binary are used.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "pr_verify", ROOT / "tools" / "pr_verify.py"
)
pr_verify = importlib.util.module_from_spec(spec)
sys.modules["pr_verify"] = pr_verify
spec.loader.exec_module(pr_verify)

SHA_NEW = "0123456789abcdef0123456789abcdef01234567"
SHA_OLD = "89abcdef0123456789abcdef0123456789abcdef"

WORKFLOWS = {
    "ci.yml": "name: CI\non:\n  push:\n    branches: [main]\n  pull_request:\n    branches: [main]\n",
    "gitleaks.yml": "name: gitleaks\non:\n  pull_request:\n    branches: [main]\n",
    "title.yml": "name: PR title check\non:\n  pull_request:\n    types: [opened, edited]\n",
    "zizmor.yml": (
        "name: zizmor\non:\n  push:\n    branches: [main]\n    paths: ['.github/workflows/**']\n"
        "  pull_request:\n    branches: [main]\n    paths: ['.github/workflows/**']\n"
    ),
    "scorecard.yml": "name: OpenSSF Scorecard\non:\n  push:\n    branches: [main]\n  schedule:\n    - cron: '0 3 * * 1'\n",
}
PR_WORKFLOWS = ["CI", "gitleaks", "PR title check"]  # zizmor is path-filtered


def run(
    name,
    sha=SHA_NEW,
    status="completed",
    conclusion="success",
    number=1,
    attempt=1,
    event="pull_request",
):
    return {
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "run_number": number,
        "run_attempt": attempt,
        "event": event,
    }


def all_success(sha=SHA_NEW):
    return [run(n, sha) for n in PR_WORKFLOWS]


class FakeGh:
    """Canned gh: `pr view` returns the current head and files; `api` filters runs on head_sha."""

    def __init__(self, runs, files=("docs/x.md",), head=SHA_NEW):
        self.runs = runs
        self.files = list(files)
        self.head = head
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            return {"headRefOid": self.head, "files": [{"path": p} for p in self.files]}
        if args[0] == "api":
            asked = next(a.split("=", 1)[1] for a in args if a.startswith("head_sha="))
            return {"workflow_runs": [r for r in self.runs if r["head_sha"] == asked]}
        raise AssertionError(f"unexpected gh call: {args}")


class PrVerifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wf_dir = Path(self.tmp.name)
        for name, text in WORKFLOWS.items():
            (self.wf_dir / name).write_text(text, encoding="utf-8")

    def verify(self, fake, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pr_verify.verify(7, gh=fake, workflows_dir=self.wf_dir, **kw)
        return code, out.getvalue(), err.getvalue()


class VerdictTests(PrVerifyBase):
    def test_all_success_exits_0(self):
        code, out, _ = self.verify(FakeGh(all_success()))
        self.assertEqual(code, 0)
        self.assertIn("pr-verify: PASS", out)
        self.assertIn("zizmor: not required (path filter)", out)

    def test_one_failure_exits_1_and_names_it(self):
        runs = all_success()
        runs[1] = run("gitleaks", conclusion="failure")
        code, out, _ = self.verify(FakeGh(runs))
        self.assertEqual(code, 1)
        self.assertIn("gitleaks: failure (failure)", out)

    def test_cancelled_and_skipped_are_not_success(self):
        for conclusion in ("cancelled", "skipped", "timed_out", "neutral"):
            runs = all_success()
            runs[0] = run("CI", conclusion=conclusion)
            self.assertEqual(self.verify(FakeGh(runs))[0], 1, conclusion)

    def test_one_missing_exits_1_as_not_started(self):
        runs = [r for r in all_success() if r["name"] != "gitleaks"]
        code, out, _ = self.verify(FakeGh(runs))
        self.assertEqual(code, 1)
        self.assertIn("gitleaks: no run for this commit (not started)", out)

    def test_one_in_progress_exits_2_and_names_it(self):
        runs = all_success()
        runs[0] = run("CI", status="in_progress", conclusion=None)
        code, out, _ = self.verify(FakeGh(runs))
        self.assertEqual(code, 2)
        self.assertIn("still running: CI", out)
        self.assertIn("pr-verify: PENDING", out)

    def test_failure_beats_in_progress(self):
        runs = all_success()
        runs[0] = run("CI", status="in_progress", conclusion=None)
        runs[1] = run("gitleaks", conclusion="failure")
        self.assertEqual(self.verify(FakeGh(runs))[0], 1)

    def test_stale_run_for_an_older_sha_does_not_count(self):
        fake = FakeGh(all_success(SHA_OLD), head=SHA_NEW)
        code, out, _ = self.verify(fake)
        self.assertEqual(code, 1)
        for name in PR_WORKFLOWS:
            self.assertIn(f"{name}: no run for this commit (not started)", out)
        api = next(c for c in fake.calls if c[0] == "api")
        self.assertIn(f"head_sha={SHA_NEW}", api)

    def test_run_of_another_sha_in_the_response_is_dropped(self):
        class Leaky(FakeGh):
            def __call__(self, args):
                if args[0] == "api":
                    return {
                        "workflow_runs": self.runs
                    }  # ignores head_sha, like a bad proxy
                return super().__call__(args)

        code, _, _ = self.verify(Leaky(all_success(SHA_OLD)))
        self.assertEqual(code, 1)

    def test_other_events_do_not_count(self):
        runs = [run(n, event="push") for n in PR_WORKFLOWS]
        self.assertEqual(self.verify(FakeGh(runs))[0], 1)

    def test_rerun_uses_the_newest_attempt(self):
        runs = all_success()
        runs.append(run("CI", conclusion="failure", number=1, attempt=1))
        runs[0] = run("CI", conclusion="success", number=1, attempt=2)
        self.assertEqual(self.verify(FakeGh(runs))[0], 0)
        runs[0] = run("CI", conclusion="failure", number=1, attempt=2)
        runs[-1] = run("CI", conclusion="success", number=1, attempt=1)
        self.assertEqual(self.verify(FakeGh(runs))[0], 1)

    def test_path_filtered_workflow_is_required_when_a_changed_file_matches(self):
        fake = FakeGh(all_success(), files=[".github/workflows/ci.yml"])
        code, out, _ = self.verify(fake)
        self.assertEqual(code, 1)
        self.assertIn("zizmor: no run for this commit (not started)", out)

    def test_path_filtered_run_that_exists_must_succeed(self):
        runs = [*all_success(), run("zizmor", conclusion="failure")]
        self.assertEqual(self.verify(FakeGh(runs))[0], 1)

    def test_gh_error_exits_1_without_a_traceback(self):
        def broken(args):
            raise pr_verify.PrVerifyError("gh pr view failed: not found")

        code, out, err = self.verify(broken)
        self.assertEqual(code, 1)
        self.assertIn("pr-verify: error: gh pr view failed", err)
        self.assertEqual(out, "")


class WaitTests(PrVerifyBase):
    def clock_and_sleep(self):
        state = {"t": 0.0, "sleeps": []}

        def sleep(seconds):
            state["sleeps"].append(seconds)
            state["t"] += seconds

        return state, (lambda: state["t"]), sleep

    def scripted(self, *run_lists, heads=None):
        """A fake whose run list changes per poll (one entry of run_lists per `pr view`)."""
        fake = FakeGh([])
        polls = {"n": -1}
        base = fake.__call__

        def gh(args):
            if args[:2] == ["pr", "view"]:
                polls["n"] += 1
                i = min(polls["n"], len(run_lists) - 1)
                fake.runs = run_lists[i]
                fake.head = (heads or [SHA_NEW] * len(run_lists))[i]
            return base(args)

        return gh

    def running(self):
        runs = all_success()
        runs[0] = run("CI", status="in_progress", conclusion=None)
        return runs

    def test_running_then_success_polls_every_30_seconds(self):
        state, clock, sleep = self.clock_and_sleep()
        gh = self.scripted(self.running(), self.running(), all_success())
        code, _, _ = self.verify(gh, wait=600, sleep=sleep, clock=clock)
        self.assertEqual(code, 0)
        self.assertEqual(state["sleeps"], [30, 30])

    def test_running_until_the_deadline_exits_2(self):
        state, clock, sleep = self.clock_and_sleep()
        code, _, _ = self.verify(
            self.scripted(self.running()), wait=60, sleep=sleep, clock=clock
        )
        self.assertEqual(code, 2)
        self.assertEqual(state["sleeps"], [30, 30])

    def test_missing_is_retried_then_fails_at_the_deadline(self):
        state, clock, sleep = self.clock_and_sleep()
        gh = self.scripted([r for r in all_success() if r["name"] != "CI"])
        code, _, _ = self.verify(gh, wait=60, sleep=sleep, clock=clock)
        self.assertEqual(code, 1)
        self.assertEqual(state["sleeps"], [30, 30])

    def test_missing_then_started_then_success(self):
        state, clock, sleep = self.clock_and_sleep()
        gh = self.scripted([], self.running(), all_success())
        code, _, _ = self.verify(gh, wait=600, sleep=sleep, clock=clock)
        self.assertEqual(code, 0)
        self.assertEqual(len(state["sleeps"]), 2)

    def test_completed_failure_ends_the_wait_at_once(self):
        state, clock, sleep = self.clock_and_sleep()
        runs = all_success()
        runs[0] = run("CI", conclusion="failure")
        code, _, _ = self.verify(
            self.scripted(runs), wait=600, sleep=sleep, clock=clock
        )
        self.assertEqual(code, 1)
        self.assertEqual(state["sleeps"], [])

    def test_without_wait_there_is_one_look_and_no_sleep(self):
        state, clock, sleep = self.clock_and_sleep()
        code, _, _ = self.verify(
            self.scripted(self.running()), wait=0, sleep=sleep, clock=clock
        )
        self.assertEqual(code, 2)
        self.assertEqual(state["sleeps"], [])

    def test_a_push_during_the_wait_is_judged_on_the_new_head(self):
        _, clock, sleep = self.clock_and_sleep()
        # first poll: head is OLD and one run is pending; second poll: head is NEW and all pass
        first = [
            run("CI", SHA_OLD, status="in_progress", conclusion=None),
            *all_success(SHA_OLD)[1:],
        ]
        gh = self.scripted(first, all_success(SHA_NEW), heads=[SHA_OLD, SHA_NEW])
        code, out, _ = self.verify(gh, wait=600, sleep=sleep, clock=clock)
        self.assertEqual(code, 0)
        self.assertIn(f"head moved {SHA_OLD[:7]} -> {SHA_NEW[:7]}", out)


class WorkflowDerivationTests(unittest.TestCase):
    def names(self, directory):
        return [w.name for w in pr_verify.expected_workflows(Path(directory))]

    def test_real_workflows_expect_ci_and_not_scorecard(self):
        names = self.names(ROOT / ".github" / "workflows")
        self.assertIn("CI", names)
        self.assertNotIn("OpenSSF Scorecard", names)

    def test_trigger_shapes(self):
        cases = {
            "on: pull_request\n": True,
            "on: [push, pull_request]\n": True,
            "on:\n  pull_request:\n": True,
            "on:\n  push:\n    branches: [main]\n": False,
            "on:\n  push:\n  schedule:\n    - cron: '0 3 * * 1'\n": False,
        }
        for body, expected in cases.items():
            wf = pr_verify.parse_workflow("name: X\n" + body, ".github/workflows/x.yml")
            self.assertEqual(wf is not None, expected, body)

    def test_unnamed_workflow_uses_its_path(self):
        wf = pr_verify.parse_workflow("on: pull_request\n", ".github/workflows/x.yml")
        self.assertEqual(wf.name, ".github/workflows/x.yml")

    def test_paths_filter_forms(self):
        inline = pr_verify.parse_workflow(
            "name: X\non:\n  pull_request:\n    paths: ['a/**', 'b.md']\n", "x.yml"
        )
        block = pr_verify.parse_workflow(
            "name: X\non:\n  pull_request:\n    paths:\n      - 'a/**'\n      - b.md\n",
            "x.yml",
        )
        self.assertEqual(inline.paths, ("a/**", "b.md"))
        self.assertEqual(block.paths, ("a/**", "b.md"))

    def test_path_glob_matching(self):
        match = pr_verify.path_filter_matches
        self.assertTrue(match((".github/workflows/**",), [".github/workflows/ci.yml"]))
        self.assertFalse(match((".github/workflows/**",), ["docs/x.md"]))
        self.assertTrue(match(("*.md",), ["README.md"]))
        self.assertFalse(match(("*.md",), ["docs/x.md"]))
        self.assertTrue(match(("**/*.md",), ["docs/a/x.md"]))
        self.assertTrue(
            match(("!docs/**",), ["anything"])
        )  # negation: cannot judge, so required


class CliTests(unittest.TestCase):
    def test_make_without_pr_fails_before_any_gh_call(self):
        if shutil.which("make") is None:
            self.skipTest("make is not installed")
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PR", "MAKEFLAGS", "MAKELEVEL")
        }
        proc = subprocess.run(
            ["make", "pr-verify"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PR is required", proc.stdout + proc.stderr)

    def test_bad_arguments_exit_1_not_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            pr_verify.main(["--pr", "abc"])
        self.assertEqual(ctx.exception.code, 1)
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            pr_verify.main([])
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
