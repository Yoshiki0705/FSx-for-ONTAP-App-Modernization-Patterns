#!/usr/bin/env python3
"""Re-add the totals of Markdown tables that opt in with a sum-check marker.

A table is read only when the line directly above its header is a marker:

    <!-- sum-check: cols=2,3,4 -->
    <!-- sum-check: cols=4 tol=0.01 -->
    <!-- sum-check: cols=4 total=row:Subtotal -->

cols   1-based indexes of the numeric columns to verify (required).
tol    absolute tolerance; default is strict equality at the stated total's own
       number of decimals (the exact sum is rounded half-up to that precision).
total  `last` (default) or `row:<first-cell-text>`; must be the last parameter.

Rows summed are those between the header separator and the total row. A row whose
first cell is wrapped in ( ) or full-width parentheses is excluded. In a verified
column, empty, `-`, `n/a` and cells with no digit are skipped; a cell with a digit
that is not a number is an error. A marker with no table, an unparsable table, a
non-numeric stated total or a column with nothing to sum is an error, never a skip.

    python3 tools/check_table_sums.py                 # default set
    python3 tools/check_table_sums.py FILE [FILE...]  # these files only
    python3 tools/check_table_sums.py --root DIR      # default set under DIR
    python3 tools/check_table_sums.py --verbose       # print each verified column
    python3 tools/check_table_sums.py --selftest      # prove it can fail and pass

Default set: docs/**/*.md, README.md, README.en.md, app/modern/README.md.
Fenced code blocks and paths under a `_template*` component are not read.
Exit 0 when every marked column holds, 1 on a mismatch or a marker error.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = ("README.md", "README.en.md", "app/modern/README.md")

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
MARKER_LOOSE = re.compile(r"^\s*<!--.*sum-check", re.IGNORECASE)
MARKER = re.compile(r"^\s*<!--\s*sum-check\s*:(?P<params>.*?)\s*-->\s*$")
SEPARATOR_CELL = re.compile(r":?-+:?")
EMPHASIS = re.compile(r"[*`_]")
NUMBER = re.compile(
    r"(?P<s1>[+\-\u2212])?\s*(?P<cur>\$)?\s*(?P<s2>[+\-\u2212])?"
    r"(?P<int>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?P<frac>\.[0-9]+)?(?:\s*USD)?",
    re.IGNORECASE,
)
SKIP_TEXT = {"", "-", "\u2014", "\u2013", "n/a"}
PARAM_KEYS = ("cols", "tol", "total")
PRECISION = 60


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    verbose: list[str] = field(default_factory=list)
    tables: int = 0
    columns: int = 0

    def merge(self, other: Report) -> None:
        self.errors += other.errors
        self.failures += other.failures
        self.verbose += other.verbose
        self.tables += other.tables
        self.columns += other.columns

    @property
    def problems(self) -> int:
        return len(self.errors) + len(self.failures)


@dataclass
class Marker:
    cols: list[int]
    tol: Decimal | None
    total: str  # "last" or "row:<text>"


def fmt(value: Decimal) -> str:
    """Plain notation; str(Decimal) can switch to exponent form."""
    return format(value, "f")


def plain(cell: str) -> str:
    """A cell without surrounding emphasis, for first-cell comparisons."""
    return cell.strip().strip("*_` ").strip()


def split_cells(line: str) -> list[str]:
    """Split a table line on unescaped pipes outside backtick spans."""
    s = line.strip()[1:]
    cells: list[str] = []
    cur: list[str] = []
    in_code = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        cells.append(tail)
    return cells


def parse_marker(params: str) -> tuple[Marker | None, str | None]:
    """Parse the marker parameters; return (marker, error)."""
    text = params.strip()
    total = "last"
    m = re.search(r"(?:^|\s)total=", text)
    head = text
    if m:
        head = text[: m.start()]
        total = text[m.end() :].strip()
        if re.search(r"(?:^|\s)(?:cols|tol|total)=", total):
            return None, "total= must be the last parameter and appear once"
    seen: dict[str, str] = {}
    for token in head.split():
        key, sep, value = token.partition("=")
        if not sep or key not in PARAM_KEYS or key == "total":
            return None, f"unknown or malformed parameter {token!r}"
        if key in seen:
            return None, f"duplicate parameter {key!r}"
        seen[key] = value
    if "cols" not in seen:
        return None, "cols= is required"
    try:
        cols = [int(x) for x in seen["cols"].split(",")]
    except ValueError:
        return None, f"cols= must be comma-separated integers, got {seen['cols']!r}"
    if any(c < 1 for c in cols):
        return None, "cols= indexes are 1-based and must be >= 1"
    if len(set(cols)) != len(cols):
        return None, "cols= must not repeat an index"
    tol: Decimal | None = None
    if "tol" in seen:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", seen["tol"]):
            return None, f"tol= must be a non-negative decimal, got {seen['tol']!r}"
        tol = Decimal(seen["tol"])
    if total != "last":
        if not total.startswith("row:") or not total[4:].strip():
            return None, f"total= must be 'last' or 'row:<text>', got {total!r}"
        total = "row:" + total[4:].strip()
    return Marker(cols, tol, total), None


def classify(cell: str) -> tuple[str, Decimal | None]:
    """Return ('value', Decimal), ('skip', None) or ('error', None)."""
    text = EMPHASIS.sub("", cell).strip()
    if text.lower() in SKIP_TEXT:
        return "skip", None
    m = NUMBER.fullmatch(text)
    if m:
        if m.group("s1") and m.group("s2"):
            return "error", None
        sign = m.group("s1") or m.group("s2") or "+"
        value = Decimal(m.group("int").replace(",", "") + (m.group("frac") or ""))
        return "value", -value if sign in ("-", "\u2212") else value
    if re.search(r"\d", text):
        return "error", None
    return "skip", None


def places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def verify_column(
    name: str,
    marker_line: int,
    index: int,
    header: str,
    body: list[tuple[int, list[str]]],
    total_pos: int,
    tol: Decimal | None,
    report: Report,
) -> None:
    col = index - 1
    total_line, total_cells = body[total_pos]
    where = f"{name}:{total_line}"
    kind, stated = classify(total_cells[col])
    if kind != "value" or stated is None:
        report.errors.append(
            f"{where}: error: column {index} ({header}): stated total "
            f"{total_cells[col]!r} is not a number (marker at line {marker_line})"
        )
        return
    used: list[Decimal] = []
    skipped: list[str] = []
    failed = False
    for pos in range(total_pos):
        row_line, cells = body[pos]
        first = plain(cells[0])
        label = f'row {pos + 1} "{first}"'
        if (first.startswith("(") and first.endswith(")")) or (
            first.startswith("\uff08") and first.endswith("\uff09")
        ):
            skipped.append(f"{label}: excluded (parenthesised row)")
            continue
        kind, value = classify(cells[col])
        if kind == "error":
            report.errors.append(
                f"{name}:{row_line}: error: column {index} ({header}): {label}: "
                f"{cells[col]!r} contains a digit but is not a number"
            )
            failed = True
        elif kind == "skip":
            skipped.append(f"{label}: {cells[col]!r}")
        else:
            assert value is not None
            used.append(value)
    if failed:
        return
    if not used:
        report.errors.append(
            f"{where}: error: column {index} ({header}): no numeric cell to sum "
            f"(marker at line {marker_line})"
        )
        return
    with localcontext() as ctx:
        ctx.prec = PRECISION
        exact = sum(used, Decimal(0))
        digits = places(stated)
        rounded = exact.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
        if tol is None:
            ok = rounded == stated
        else:
            ok = abs(exact - stated) <= tol
    rows = ", ".join(fmt(v) for v in used)
    skips = "; ".join(skipped) if skipped else "none"
    computed = fmt(exact)
    if tol is None and rounded != exact:
        computed += f" (rounded to {digits} place(s): {fmt(rounded)})"
    if tol is not None:
        computed += f" (tol {fmt(tol)})"
    summary = (
        f"column {index} ({header}): stated total {fmt(stated)} "
        f"{'==' if ok else '!='} sum {computed} (rows used: {rows}; skipped: {skips})"
    )
    report.columns += 1
    if ok:
        report.verbose.append(f"{where}: {summary}")
    else:
        report.failures.append(f"{where}: {summary}")


def check_table(
    name: str, lines: list[str], at: int, marker: Marker, report: Report
) -> int:
    """Check the table below marker line `at` (0-based). Return the next index to scan."""
    marker_line = at + 1
    start = at + 1
    end = start
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    where = f"{name}:{marker_line}"
    if end == start:
        report.errors.append(f"{where}: error: marker has no table directly below it")
        return start
    report.tables += 1
    block = lines[start:end]
    header = split_cells(block[0])
    if len(block) < 2:
        report.errors.append(f"{where}: error: table has no header separator")
        return end
    sep = split_cells(block[1])
    if len(sep) != len(header) or not all(SEPARATOR_CELL.fullmatch(c) for c in sep):
        report.errors.append(
            f"{where}: error: unparsable table: line {start + 2} is not a header separator "
            f"with {len(header)} cell(s)"
        )
        return end
    body: list[tuple[int, list[str]]] = []
    for offset, raw in enumerate(block[2:], start=start + 3):
        cells = split_cells(raw)
        if len(cells) != len(header):
            report.errors.append(
                f"{where}: error: unparsable table: line {offset} has {len(cells)} cell(s), "
                f"header has {len(header)}"
            )
            return end
        body.append((offset, cells))
    if len(body) < 2:
        report.errors.append(
            f"{where}: error: table needs at least one summed row and a total row, "
            f"found {len(body)} row(s)"
        )
        return end
    bad_cols = [c for c in marker.cols if c > len(header)]
    if bad_cols:
        report.errors.append(
            f"{where}: error: cols= {bad_cols} outside the table's {len(header)} column(s)"
        )
        return end
    if marker.total == "last":
        total_pos = len(body) - 1
    else:
        wanted = marker.total[4:]
        hits = [i for i, (_, cells) in enumerate(body) if plain(cells[0]) == wanted]
        if len(hits) != 1:
            report.errors.append(
                f"{where}: error: total=row:{wanted} matched {len(hits)} row(s), expected 1"
            )
            return end
        total_pos = hits[0]
    if total_pos == 0:
        report.errors.append(f"{where}: error: no row above the total row to sum")
        return end
    for index in marker.cols:
        verify_column(
            name,
            marker_line,
            index,
            header[index - 1],
            body,
            total_pos,
            marker.tol,
            report,
        )
    return end


def scan_text(text: str, name: str) -> Report:
    """Check every marked table in `text`; `name` is only used in messages."""
    report = Report()
    lines = text.split("\n")
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        f = FENCE.match(line)
        if f:
            token = f.group(1)
            if fence is None:
                fence = token[0] * len(token)
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            i += 1
            continue
        if fence is not None or not MARKER_LOOSE.match(line):
            i += 1
            continue
        m = MARKER.match(line)
        if not m:
            report.errors.append(
                f"{name}:{i + 1}: error: malformed sum-check marker (expected "
                "'<!-- sum-check: cols=N -->' on one line)"
            )
            i += 1
            continue
        marker, error = parse_marker(m.group("params"))
        if marker is None:
            report.errors.append(f"{name}:{i + 1}: error: {error}")
            i += 1
            continue
        i = check_table(name, lines, i, marker, report)
    return report


def is_template(path: Path) -> bool:
    return any(part.startswith("_template") for part in path.parts)


def default_files(root: Path, report: Report) -> list[Path]:
    files: list[Path] = []
    docs = root / "docs"
    if docs.is_dir():
        files += [
            p
            for p in sorted(docs.rglob("*.md"))
            if not is_template(p.relative_to(root))
        ]
    else:
        report.errors.append("docs/: error: directory not found")
    for rel in DEFAULT_FILES:
        if (root / rel).is_file():
            files.append(root / rel)
        else:
            report.errors.append(f"{rel}: error: file not found")
    return files


def scan_files(files: list[Path], root: Path | None, report: Report) -> int:
    """Scan files into `report`; return the number of files that carried a marker."""
    with_markers = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{path}: error: cannot read: {exc}")
            continue
        try:
            shown = path.relative_to(root).as_posix() if root else str(path)
        except ValueError:
            shown = str(path)
        sub = scan_text(text, shown)
        if sub.tables or sub.errors or sub.failures:
            with_markers += 1
        report.merge(sub)
    return with_markers


GOOD = (
    "<!-- sum-check: cols=2 -->\n"
    "| Item | USD |\n|---|---|\n| a | 12.03 |\n| b | 6.36 |\n| Subtotal | 18.39 |\n"
)
BAD = GOOD.replace("18.39", "18.40")
FENCED = "```md\n" + BAD + "```\n"
CASES = [
    ("good strict table passes", GOOD, 0, ()),
    ("total off by 0.01 fails with numbers", BAD, 1, ("18.40", "18.39", "column 2")),
    (
        "marker without table fails",
        "<!-- sum-check: cols=2 -->\nprose\n",
        1,
        ("no table",),
    ),
    (
        "table without marker is ignored",
        GOOD.split("\n", 1)[1].replace("18.39", "99"),
        0,
        (),
    ),
    ("marker inside a fence is ignored", FENCED, 0, ()),
    ("tol accepts a 0.01 gap", BAD.replace("cols=2", "cols=2 tol=0.01"), 0, ()),
    (
        "non-numeric stated total fails",
        GOOD.replace("18.39", "n/a"),
        1,
        ("not a number",),
    ),
]


def selftest() -> int:
    failed: list[str] = []
    for label, text, want_problems, needles in CASES:
        report = scan_text(text, "selftest.md")
        problems = report.problems
        message = "\n".join(report.errors + report.failures)
        if bool(problems) != bool(want_problems) or any(
            n not in message for n in needles
        ):
            failed.append(label)
    for label in failed:
        print(f"selftest FAIL: {label}", file=sys.stderr)
    if failed:
        return 1
    print(f"selftest: {len(CASES)} case(s) passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        epilog="Usage and marker syntax are in the module docstring.",
    )
    parser.add_argument("files", nargs="*", type=Path, help="scan only these files")
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root for the default set"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print each verified column"
    )
    parser.add_argument(
        "--selftest", action="store_true", help="prove the check can fail and pass"
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()

    report = Report()
    if args.files:
        with_markers = scan_files(args.files, None, report)
    else:
        root = args.root.resolve()
        with_markers = scan_files(default_files(root, report), root, report)
    if args.verbose:
        for line in report.verbose:
            print(line)
    for line in report.errors + report.failures:
        print(line, file=sys.stderr)
    if report.problems:
        print(
            f"table sums: FAILED, {report.problems} problem(s) in "
            f"{report.tables} marked table(s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"table sums: {report.tables} marked table(s), {report.columns} column(s) "
        f"verified in {with_markers} file(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
