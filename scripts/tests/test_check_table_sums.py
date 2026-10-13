#!/usr/bin/env python3
"""tools/check_table_sums.py re-adds marked tables and fails on a wrong total (stdlib only).

Cases: strict pass and the 21.19-versus-21.18 fail, tolerance pass and fail, strict rounding,
skipped and unparsable cells, excluded rows, marker without a table, unparsable tables, number
formats, two tables in one file, total=row:, fenced code, marker parameter errors, Decimal
discipline, --selftest and the CLI. No network, no AWS call.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "check_table_sums", ROOT / "tools" / "check_table_sums.py"
)
checker = importlib.util.module_from_spec(spec)
sys.modules["check_table_sums"] = checker
spec.loader.exec_module(checker)

COST_ROWS = ["12.03", "6.36", "2.77", "0.02", "0"]


def table(
    total: str,
    rows: list[str] | None = None,
    marker: str = "cols=2",
    names: list[str] | None = None,
) -> str:
    """A marked two-column table: one line per row plus a last 'Subtotal' row."""
    rows = COST_ROWS if rows is None else rows
    names = names or [f"item{i}" for i in range(len(rows))]
    lines = [f"<!-- sum-check: {marker} -->", "| Service | Total (USD) |", "|---|---:|"]
    lines += [f"| {n} | {v} |" for n, v in zip(names, rows)]
    lines.append(f"| Subtotal | {total} |")
    return "\n".join(lines) + "\n"


def scan(text: str):
    return checker.scan_text(text, "doc.md")


def message(report) -> str:
    return "\n".join(report.errors + report.failures)


class StrictTests(unittest.TestCase):
    def test_strict_pass(self) -> None:
        report = scan(table("21.18"))
        self.assertEqual(report.problems, 0, message(report))
        self.assertEqual((report.tables, report.columns), (1, 1))

    def test_strict_fail_names_everything(self) -> None:
        report = scan("intro\n\n" + table("21.19"))
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.errors, [])
        text = report.failures[0]
        # file, line of the total row, column, stated total, computed sum, rows used
        self.assertTrue(text.startswith("doc.md:11:"), text)
        self.assertIn("column 2 (Total (USD))", text)
        self.assertIn("stated total 21.19", text)
        self.assertIn("sum 21.18", text)
        self.assertIn("rows used: 12.03, 6.36, 2.77, 0.02, 0", text)

    def test_strict_rounds_to_the_total_precision(self) -> None:
        rows = ["7.837", "4.191"]
        self.assertEqual(scan(table("12.03", rows)).problems, 0)
        failed = scan(table("12.02", rows))
        self.assertEqual(len(failed.failures), 1)
        self.assertIn("rounded to 2 place(s): 12.03", failed.failures[0])

    def test_strict_is_exact_at_zero_decimals(self) -> None:
        self.assertEqual(scan(table("1,234", ["1,000", "234"])).problems, 0)
        self.assertEqual(len(scan(table("1,235", ["1,000", "234"])).failures), 1)

    def test_decimal_not_float(self) -> None:
        self.assertEqual(scan(table("0.3", ["0.1", "0.2"])).problems, 0)
        source = (ROOT / "tools" / "check_table_sums.py").read_text(encoding="utf-8")
        self.assertNotIn("float(", source)


class ToleranceTests(unittest.TestCase):
    def test_tol_pass_at_exactly_the_tolerance(self) -> None:
        report = scan(table("21.19", marker="cols=2 tol=0.01"))
        self.assertEqual(report.problems, 0, message(report))

    def test_tol_fail_beyond_the_tolerance(self) -> None:
        report = scan(table("21.20", marker="cols=2 tol=0.01"))
        self.assertEqual(len(report.failures), 1)
        self.assertIn("tol 0.01", report.failures[0])

    def test_tol_zero_is_a_strict_difference_of_nothing(self) -> None:
        self.assertEqual(len(scan(table("21.19", marker="cols=2 tol=0")).failures), 1)


class CellTests(unittest.TestCase):
    def test_skipped_cells_are_listed(self) -> None:
        text = table(
            "3.00",
            ["1.00", "-", "n/a", "帰属不可", "2.00"],
            names=["a", "b", "c", "d", "e"],
        )
        report = scan(text)
        self.assertEqual(report.problems, 0, message(report))
        self.assertIn('row 2 "b"', "\n".join(report.verbose))
        self.assertIn('row 4 "d"', "\n".join(report.verbose))

    def test_digit_cell_that_is_not_a_number_is_an_error(self) -> None:
        report = scan(table("1.00", ["1.00", "約 0"], names=["a", "b"]))
        self.assertEqual(report.failures, [])
        self.assertEqual(len(report.errors), 1)
        self.assertIn("contains a digit but is not a number", report.errors[0])
        self.assertEqual(len(scan(table("1.00", ["1.00", "21.18%"])).errors), 1)

    def test_parenthesised_rows_are_excluded(self) -> None:
        names = ["a", "(control)", "（対照）", "b"]
        report = scan(table("3.00", ["1.00", "50.00", "60.00", "2.00"], names=names))
        self.assertEqual(report.problems, 0, message(report))
        self.assertIn("excluded", "\n".join(report.verbose))

    def test_number_formats(self) -> None:
        rows = ["$1,000.50", "12.03 USD", "**2.47**", "`3.00`", "\u22121.00", "-0.50"]
        # 1000.50 + 12.03 + 2.47 + 3.00 - 1.00 - 0.50 = 1016.50
        report = scan(table("$1,016.50", rows))
        self.assertEqual(report.problems, 0, message(report))
        self.assertEqual(scan(table("**1,016.50 USD**", rows)).problems, 0)

    def test_malformed_thousands_separator_is_an_error(self) -> None:
        self.assertEqual(len(scan(table("1.00", ["1,23", "1.00"])).errors), 1)


class StructureTests(unittest.TestCase):
    def test_marker_without_table(self) -> None:
        for tail in (
            "prose follows\n",
            "\n| A | B |\n|---|---|\n| x | 1 |\n| t | 1 |\n",
            "",
        ):
            report = scan("<!-- sum-check: cols=2 -->\n" + tail)
            self.assertEqual(len(report.errors), 1, tail)
            self.assertIn("no table", report.errors[0])
            self.assertEqual(report.tables, 0)

    def test_unparsable_tables(self) -> None:
        good = table("21.18")
        cases = {
            "ragged row": good.replace("| item2 | 2.77 |", "| item2 | 2.77 | extra |"),
            "no separator": good.replace("|---|---:|", "| x | y |"),
            "header only": "<!-- sum-check: cols=2 -->\n| A | B |\n",
            "cols out of range": good.replace("cols=2", "cols=3"),
            "total not numeric": table("n/a"),
            "total text": table("合計不明"),
            "nothing to sum": table("5.00", ["-", "n/a"]),
            "one row only": table("1.00", ["1.00"]).replace("| item0 | 1.00 |\n", ""),
        }
        for label, text in cases.items():
            report = scan(text)
            self.assertEqual(report.failures, [], label)
            self.assertGreaterEqual(len(report.errors), 1, label)

    def test_two_tables_in_one_file(self) -> None:
        both_good = table("21.18") + "\nbetween\n\n" + table("3.00", ["1.00", "2.00"])
        report = scan(both_good)
        self.assertEqual((report.problems, report.tables, report.columns), (0, 2, 2))
        one_bad = table("21.18") + "\nbetween\n\n" + table("3.01", ["1.00", "2.00"])
        report = scan(one_bad)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("doc.md:18:", report.failures[0])
        self.assertIn("stated total 3.01", report.failures[0])

    def test_several_columns(self) -> None:
        text = (
            "<!-- sum-check: cols=2,3 -->\n| K | A | B |\n|---|---|---|\n"
            "| x | 1.00 | 2.00 |\n| y | 2.00 | 3.00 |\n| Total | 3.00 | 5.01 |\n"
        )
        report = scan(text)
        self.assertEqual(report.columns, 2)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("column 3 (B)", report.failures[0])

    def test_escaped_pipe_and_code_span_do_not_split_cells(self) -> None:
        text = (
            "<!-- sum-check: cols=2 -->\n| K | A |\n|---|---|\n"
            "| a \\| b | 1.00 |\n| `x | y` | 2.00 |\n| Total | 3.00 |\n"
        )
        self.assertEqual(scan(text).problems, 0)

    def test_total_row_selector(self) -> None:
        text = (
            "<!-- sum-check: cols=2 total=row:Sub total -->\n| K | A |\n|---|---|\n"
            "| x | 1.00 |\n| y | 2.00 |\n| **Sub total** | 3.00 |\n| Estimate | 99.00 |\n"
        )
        self.assertEqual(scan(text).problems, 0)
        self.assertEqual(len(scan(text.replace("| 3.00 |", "| 3.01 |")).failures), 1)
        missing = scan(text.replace("row:Sub total", "row:Nothing"))
        self.assertEqual(len(missing.errors), 1)
        twice = scan(text.replace("| Estimate |", "| Sub total |"))
        self.assertEqual(len(twice.errors), 1)

    def test_total_row_cannot_be_the_first_row(self) -> None:
        text = (
            "<!-- sum-check: cols=2 total=row:x -->\n| K | A |\n|---|---|\n"
            "| x | 1.00 |\n| y | 2.00 |\n"
        )
        self.assertEqual(len(scan(text).errors), 1)

    def test_fenced_code_is_ignored(self) -> None:
        wrong = table("99.99")
        for fence in ("```", "~~~~"):
            report = scan(f"{fence}md\n{wrong}{fence}\n\ntext\n")
            self.assertEqual((report.problems, report.tables), (0, 0), fence)
        # the table after a closed fence is read again
        after = scan(f"```md\n{wrong}```\n\n{table('99.99')}")
        self.assertEqual(len(after.failures), 1)

    def test_unmarked_table_and_inline_marker_are_ignored(self) -> None:
        unmarked = "| K | A |\n|---|---|\n| x | 1 |\n| Total | 5 |\n"
        self.assertEqual(scan(unmarked).tables, 0)
        prose = "Write `<!-- sum-check: cols=2 -->` above the table.\n"
        self.assertEqual(scan(prose).problems, 0)

    def test_malformed_marker_is_an_error(self) -> None:
        report = scan("<!-- sum-check cols=2 -->\n" + table("21.18").split("\n", 1)[1])
        self.assertEqual(len(report.errors), 1)
        self.assertIn("malformed", report.errors[0])


class MarkerParameterTests(unittest.TestCase):
    def assert_param_error(self, marker: str) -> None:
        report = scan(table("21.18", marker=marker))
        self.assertEqual(report.failures, [], marker)
        self.assertEqual(len(report.errors), 1, f"{marker}: {message(report)}")

    def test_parameter_errors(self) -> None:
        for marker in (
            "cols=2 colour=3",
            "cols=2 cols=2",
            "cols=0",
            "cols=2,2",
            "cols=a",
            "cols=2 tol=abc",
            "cols=2 tol=-1",
            "tol=0.01",
            "cols=2 total=sum",
            "cols=2 total=row:",
            "cols=2 total=last tol=0.01",
            "cols=2 total=row:Subtotal total=last",
        ):
            with self.subTest(marker=marker):
                self.assert_param_error(marker)

    def test_total_last_is_the_default_and_explicit(self) -> None:
        self.assertEqual(scan(table("21.18", marker="cols=2 total=last")).problems, 0)
        self.assertEqual(
            scan(table("21.18", marker="cols=2 tol=0.01 total=last")).problems, 0
        )


class CommandLineTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = checker.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_selftest(self) -> None:
        code, out, err = self.run_cli(["--selftest"])
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("case(s) passed", out)

    def test_selftest_cases_discriminate(self) -> None:
        for label, text, want, _ in checker.CASES:
            self.assertEqual(
                bool(checker.scan_text(text, "x.md").problems), bool(want), label
            )

    def test_file_argument_both_ways(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.md"
            bad = Path(directory) / "bad.md"
            good.write_text(table("21.18"), encoding="utf-8")
            bad.write_text(table("21.19"), encoding="utf-8")
            code, out, err = self.run_cli([str(good)])
            self.assertEqual(code, 0, err)
            self.assertIn("1 marked table(s), 1 column(s) verified in 1 file(s)", out)
            code, out, err = self.run_cli([str(bad)])
            self.assertEqual(code, 1)
            self.assertIn("stated total 21.19", err)
            self.assertIn("sum 21.18", err)
            self.assertIn("FAILED", err)
            self.assertNotIn("verified", out)

    def test_marker_error_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lonely.md"
            path.write_text("<!-- sum-check: cols=2 -->\n", encoding="utf-8")
            code, _, err = self.run_cli([str(path)])
            self.assertEqual(code, 1)
            self.assertIn("no table", err)

    def test_default_set_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs" / "ja").mkdir(parents=True)
            (root / "docs" / "_template").mkdir()
            (root / "app" / "modern").mkdir(parents=True)
            for rel in ("README.md", "README.en.md", "app/modern/README.md"):
                (root / rel).write_text("# readme\n", encoding="utf-8")
            (root / "docs" / "ja" / "a.md").write_text(table("21.18"), encoding="utf-8")
            (root / "docs" / "_template" / "t.md").write_text(
                table("99"), encoding="utf-8"
            )
            code, out, err = self.run_cli(["--root", str(root), "--verbose"])
            self.assertEqual(code, 0, err)
            self.assertIn("docs/ja/a.md:", out)
            self.assertIn("1 marked table(s), 1 column(s) verified in 1 file(s)", out)
            (root / "docs" / "ja" / "b.md").write_text(table("21.19"), encoding="utf-8")
            code, _, err = self.run_cli(["--root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("docs/ja/b.md:", err)

    def test_missing_default_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            code, _, err = self.run_cli(["--root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("README.md: error: file not found", err)


class RealTreeTests(unittest.TestCase):
    """The markers in the published docs bite, in both languages."""

    MARKER = "<!-- sum-check: cols=4 -->"
    PAGES = ("docs/ja/environment.md", "docs/en/environment.md")

    def read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_both_languages_carry_the_identical_marker_once(self) -> None:
        for rel in self.PAGES:
            lines = [ln for ln in self.read(rel).splitlines() if "sum-check" in ln]
            self.assertEqual(lines, [self.MARKER], rel)

    def test_real_tree_passes(self) -> None:
        for rel in self.PAGES:
            report = checker.scan_text(self.read(rel), rel)
            self.assertEqual(report.problems, 0, message(report))
            self.assertEqual((report.tables, report.columns), (1, 1), rel)

    def test_a_wrong_total_in_either_language_fails(self) -> None:
        for rel in self.PAGES:
            text = self.read(rel)
            mutated = text.replace("| - | - | 21.18 |", "| - | - | 21.19 |", 1)
            self.assertNotEqual(mutated, text, rel)
            report = checker.scan_text(mutated, rel)
            self.assertEqual(report.problems, 1, rel)
            self.assertIn("stated total 21.19", message(report), rel)


if __name__ == "__main__":
    unittest.main()
