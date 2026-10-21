#!/usr/bin/env python3
"""tools/check_inbound_probes.py fails when a string a sibling pins here is reworded (stdlib only).

Cases: each way a pinned string stops guarding (absent, duplicated, heading only, only in a fence,
file gone) fails and names the string; a string present once passes; the real contract against the
real documents passes; the same real documents fail once a pinned string is removed or doubled,
which shows the real check can fail. --refresh writes nothing when a sibling's answer is
undetermined, and only a 404 records a sibling as unknown. No network, no AWS call.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
spec = importlib.util.spec_from_file_location(
    "check_inbound_probes", ROOT / "tools" / "check_inbound_probes.py"
)
checker = importlib.util.module_from_spec(spec)
sys.modules["check_inbound_probes"] = checker
spec.loader.exec_module(checker)

HUB = "Hub-Repo"


def make_root(directory: str, contract_rows: str, **docs: str) -> Path:
    root = Path(directory)
    (root / "docs" / "agent").mkdir(parents=True)
    (root / checker.CONTRACT).write_text(
        checker.HEADER + contract_rows, encoding="utf-8"
    )
    for name, text in docs.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def run_check(contract_rows: str, **docs: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as directory:
        root = make_root(directory, contract_rows, **docs)
        code, lines = checker.check(root)
    return code, "\n".join(lines)


def row(path: str, probe: str, role: str = "retraction") -> str:
    return f"{HUB}\t{path}\t{role}\t{probe}\n"


class VerdictTests(unittest.TestCase):
    def test_present_exactly_once_in_body_passes(self) -> None:
        code, out = run_check(row("a.md", "PIN"), **{"a.md": "# T\n\nsays PIN once.\n"})
        self.assertEqual(code, 0, out)
        self.assertIn("1 pinned string(s) present exactly once", out)

    def test_absent_fails_and_names_the_string_and_the_citing_repo(self) -> None:
        code, out = run_check(row("a.md", "PIN"), **{"a.md": "# T\n\nreworded.\n"})
        self.assertEqual(code, 1)
        self.assertIn("'PIN'", out)
        self.assertIn(HUB, out)
        self.assertIn("is gone", out)

    def test_duplicated_fails(self) -> None:
        code, out = run_check(
            row("a.md", "PIN"), **{"a.md": "# T\n\nPIN one.\n\nPIN two.\n"}
        )
        self.assertEqual(code, 1)
        self.assertIn("more than once", out)

    def test_two_hits_on_one_line_count_as_one_line_like_the_hub(self) -> None:
        """The Hub counts lines, not occurrences; this pins that the copy follows the Hub."""
        code, _ = run_check(row("a.md", "PIN"), **{"a.md": "# T\n\nPIN and PIN.\n"})
        self.assertEqual(code, 0)

    def test_heading_only_fails(self) -> None:
        code, out = run_check(
            row("a.md", "PIN"), **{"a.md": "# PIN\n\nthe claim was replaced.\n"}
        )
        self.assertEqual(code, 1)
        self.assertIn("only a heading", out)

    def test_string_only_inside_a_fence_counts_as_absent(self) -> None:
        code, out = run_check(row("a.md", "PIN"), **{"a.md": "# T\n\n```\nPIN\n```\n"})
        self.assertEqual(code, 1)
        self.assertIn("is gone", out)

    def test_missing_file_fails_and_asks_to_tell_the_citing_repo(self) -> None:
        code, out = run_check(row("gone.md", "PIN"))
        self.assertEqual(code, 1)
        self.assertIn("gone.md: does not exist", out)
        self.assertIn("tell them", out)

    def test_every_failure_is_reported_not_just_the_first(self) -> None:
        code, out = run_check(
            row("a.md", "ONE") + row("b.md", "TWO") + row("c.md", "THREE"),
            **{"a.md": "# T\n\nx\n", "b.md": "# T\n\nTWO\n", "c.md": "# T\n\ny\n"},
        )
        self.assertEqual(code, 1)
        self.assertIn("2 issue(s)", out)
        self.assertIn("'ONE'", out)
        self.assertIn("'THREE'", out)
        self.assertNotIn("'TWO'", out)

    def test_role_does_not_change_the_verdict(self) -> None:
        code, _ = run_check(row("a.md", "PIN", role="reread"), **{"a.md": "# T\n\nx\n"})
        self.assertEqual(code, 1)

    def test_missing_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, lines = checker.check(Path(directory))
        self.assertEqual(code, 1)
        self.assertIn("--refresh", "\n".join(lines))


class ContractParsingTests(unittest.TestCase):
    def test_header_and_blank_lines_yield_no_rows(self) -> None:
        rows, unknown, problems = checker.parse_contract(checker.render([], []))
        self.assertEqual((rows, unknown, problems), ([], [], []))

    def test_malformed_row_is_a_problem_with_a_line_number(self) -> None:
        _, _, problems = checker.parse_contract("a\tb\tc\n")
        self.assertEqual(len(problems), 1)
        self.assertIn(":1:", problems[0])

    def test_empty_field_is_a_problem(self) -> None:
        _, _, problems = checker.parse_contract("a\t\tretraction\tp\n")
        self.assertEqual(len(problems), 1)

    def test_a_malformed_row_fails_the_check(self) -> None:
        code, out = run_check("only two\tfields\n")
        self.assertEqual(code, 1)
        self.assertIn("expected 4", out)

    def test_render_and_parse_round_trip(self) -> None:
        rows = [
            checker.Row("B", "p2.md", "retraction", "q2"),
            checker.Row("A", "p1.md", "reread", "q1"),
        ]
        parsed, unknown, problems = checker.parse_contract(
            checker.render(rows, ["Z", "Y"])
        )
        self.assertEqual(sorted(parsed), sorted(rows))
        self.assertEqual(unknown, ["Y", "Z"])
        self.assertEqual(problems, [])

    def test_render_sorts_and_deduplicates(self) -> None:
        r = checker.Row("A", "p.md", "retraction", "q")
        text = checker.render([r, r], [])
        self.assertEqual(text.count(r.line()), 1)


class DiscoveryTests(unittest.TestCase):
    def test_repos_are_found_in_prose_and_this_repo_is_excluded(self) -> None:
        owner, this = checker.OWNER, checker.THIS_REPO
        found = checker.repos_in(
            [
                (
                    f"[a](https://github.com/{owner}/Alpha/blob/main/x.md) "
                    f"[b](https://github.com/{owner}/Beta.git) "
                    f"[c](https://github.com/{owner}/Gamma). "
                    f"[d](https://github.com/{owner}/{this}.git)\n"
                )
            ]
        )
        self.assertEqual(found, {"Alpha", "Beta", "Gamma"})

    def test_repos_inside_a_fence_are_ignored(self) -> None:
        owner = checker.OWNER
        text = f"```\nhttps://github.com/{owner}/InFence\n```\n"
        self.assertEqual(checker.repos_in([text]), set())

    def test_other_owners_are_ignored(self) -> None:
        self.assertEqual(
            checker.repos_in(["https://github.com/someone-else/Repo"]), set()
        )

    def test_published_rows_keep_only_lines_pinned_here(self) -> None:
        body = (
            "# comment\n"
            f"{checker.THIS_REPO}\ta.md\tretraction\tP1\n"
            "Other-Repo\tb.md\tretraction\tP2\n"
            "not a row\n"
            "\n"
        )
        self.assertEqual(
            checker.published_rows(body, HUB),
            [checker.Row(HUB, "a.md", "retraction", "P1")],
        )

    def test_collect_records_a_missing_contract_as_unknown(self) -> None:
        bodies = {
            "Has": f"{checker.THIS_REPO}\ta.md\tretraction\tP\n",
            "Silent": None,
        }
        rows, unknown = checker.collect(
            ["Silent", "Has"], lambda repo, path: bodies[repo]
        )
        self.assertEqual(rows, [checker.Row("Has", "a.md", "retraction", "P")])
        self.assertEqual(unknown, ["Silent"])

    def test_collect_asks_for_the_published_contract_path(self) -> None:
        asked: list[tuple[str, str]] = []

        def fetch(repo: str, path: str) -> str | None:
            asked.append((repo, path))
            return ""

        checker.collect(["R"], fetch)
        self.assertEqual(asked, [("R", checker.SIBLING_CONTRACT)])


def completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FetchTests(unittest.TestCase):
    def test_success_returns_the_body(self) -> None:
        with mock.patch.object(
            checker.subprocess, "run", return_value=completed(0, "body")
        ):
            self.assertEqual(checker.gh_fetch("R", "p"), "body")

    def test_404_means_the_file_does_not_exist(self) -> None:
        with mock.patch.object(
            checker.subprocess,
            "run",
            return_value=completed(1, "", "gh: Not Found (HTTP 404)"),
        ):
            self.assertIsNone(checker.gh_fetch("R", "p"))

    def test_rate_limit_is_undetermined_not_missing(self) -> None:
        with (
            mock.patch.object(
                checker.subprocess,
                "run",
                return_value=completed(1, "", "gh: API rate limit exceeded (HTTP 403)"),
            ),
            self.assertRaises(checker.FetchError),
        ):
            checker.gh_fetch("R", "p")

    def test_server_error_is_undetermined(self) -> None:
        with (
            mock.patch.object(
                checker.subprocess,
                "run",
                return_value=completed(1, "", "gh: Bad Gateway (HTTP 502)"),
            ),
            self.assertRaises(checker.FetchError),
        ):
            checker.gh_fetch("R", "p")

    def test_missing_gh_is_undetermined(self) -> None:
        with (
            mock.patch.object(
                checker.subprocess, "run", side_effect=FileNotFoundError("gh")
            ),
            self.assertRaises(checker.FetchError),
        ):
            checker.gh_fetch("R", "p")

    def test_timeout_is_undetermined(self) -> None:
        with (
            mock.patch.object(
                checker.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("gh", 60),
            ),
            self.assertRaises(checker.FetchError),
        ):
            checker.gh_fetch("R", "p")


class RefreshTests(unittest.TestCase):
    def refresh(self, root: Path, fetch) -> int:
        owner = checker.OWNER
        with (
            mock.patch.object(
                checker,
                "tracked_markdown",
                return_value=[f"https://github.com/{owner}/Sib\n"],
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return checker.refresh(root, fetch)

    def test_undetermined_answer_writes_nothing(self) -> None:
        def fetch(repo: str, path: str) -> str | None:
            raise checker.FetchError(f"{repo}: rate limit")

        with tempfile.TemporaryDirectory() as directory:
            root = make_root(directory, row("a.md", "KEEP"))
            before = (root / checker.CONTRACT).read_text(encoding="utf-8")
            self.assertEqual(self.refresh(root, fetch), 2)
            self.assertEqual(
                (root / checker.CONTRACT).read_text(encoding="utf-8"), before
            )

    def test_refresh_rewrites_the_contract_from_the_published_rows(self) -> None:
        body = f"{checker.THIS_REPO}\tnew.md\tretraction\tNEW\n"
        with tempfile.TemporaryDirectory() as directory:
            root = make_root(directory, row("a.md", "OLD"))
            self.assertEqual(self.refresh(root, lambda repo, path: body), 0)
            rows, unknown, problems = checker.parse_contract(
                (root / checker.CONTRACT).read_text(encoding="utf-8")
            )
        self.assertEqual(rows, [checker.Row("Sib", "new.md", "retraction", "NEW")])
        self.assertEqual((unknown, problems), ([], []))

    def test_refresh_creates_the_contract_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.refresh(root, lambda repo, path: None), 0)
            text = (root / checker.CONTRACT).read_text(encoding="utf-8")
        self.assertIn(checker.UNKNOWN_MARKER + " Sib", text)


class RealRepositoryTests(unittest.TestCase):
    def real_rows(self) -> list:
        text = (ROOT / checker.CONTRACT).read_text(encoding="utf-8")
        rows, _, problems = checker.parse_contract(text)
        self.assertEqual(problems, [])
        return rows

    def copy_of_real_tree(self, directory: str, rows: list) -> Path:
        """The real contract and the real documents it pins, copied so one can be edited."""
        root = Path(directory)
        (root / "docs" / "agent").mkdir(parents=True)
        (root / checker.CONTRACT).write_text(
            (ROOT / checker.CONTRACT).read_text(encoding="utf-8"), encoding="utf-8"
        )
        for r in rows:
            dest = root / r.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                (ROOT / r.path).read_text(encoding="utf-8"), encoding="utf-8"
            )
        return root

    def test_real_contract_against_real_documents_passes(self) -> None:
        code, lines = checker.check(ROOT)
        self.assertEqual(code, 0, "\n".join(lines))

    def test_real_document_fails_when_a_pinned_string_is_removed(self) -> None:
        """Negative control: the real check can fail on the real contract."""
        rows = self.real_rows()
        if not rows:
            self.skipTest("the real contract pins nothing")
        target = rows[0]
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_of_real_tree(directory, rows)
            body = (root / target.path).read_text(encoding="utf-8")
            (root / target.path).write_text(
                body.replace(target.probe, "(reworded)"), encoding="utf-8"
            )
            code, lines = checker.check(root)
        self.assertEqual(code, 1)
        self.assertIn(target.probe, "\n".join(lines))

    def test_real_document_fails_when_a_pinned_string_is_quoted_a_second_time(
        self,
    ) -> None:
        rows = self.real_rows()
        if not rows:
            self.skipTest("the real contract pins nothing")
        target = rows[0]
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_of_real_tree(directory, rows)
            body = (root / target.path).read_text(encoding="utf-8")
            (root / target.path).write_text(
                body + f"\n補足: {target.probe}\n", encoding="utf-8"
            )
            code, lines = checker.check(root)
        self.assertEqual(code, 1)
        self.assertIn("more than once", "\n".join(lines))

    def test_selftest_passes(self) -> None:
        self.assertEqual(checker.selftest(), 0)


if __name__ == "__main__":
    unittest.main()
