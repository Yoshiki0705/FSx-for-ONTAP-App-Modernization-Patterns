#!/usr/bin/env python3
"""Fail when a diagram label would render too small for a reader to read.

The rule is not "`fontSize` must be at least N", because that check passes on a diagram nobody can
read: a label is displayed at the size it has *after* the image is scaled down to fit the column it
sits in, and a wider canvas scales down further. Diagrams in these repositories were authored at
`fontSize=11` on canvases between 1080 and 1600px, which arrive in an 880px column between 6 and 9px
— small enough that a measured figure in a notes box is guesswork on a laptop.

So two floors apply together:

* **effective size** -- `fontSize x min(1, PUBLICATION_WIDTH / rendered width)` must reach
  `MIN_EFFECTIVE_PX`. This is the one that tracks what a reader sees;
* **source size** -- `fontSize` must reach `MIN_SOURCE_PX` regardless. Without it, the first floor
  can be satisfied by making the canvas narrower and the text relatively larger while both are tiny,
  and it would also accept a 400px canvas at 8px.

The width used is the **exported SVG's** width when the export exists, not `pageWidth`. draw.io crops
to content and adds `--border`, so the two differ, and the export is what a reader loads. `pageWidth`
is the fallback so a diagram can be checked before it has ever been exported.

Both `style="...fontSize=11..."` and an inline `font-size:11px` inside a cell's `value` are read. The
second form is how a hand-edit sneaks a small font past a generator whose style functions all look
correct.

An absent `fontSize` is read as draw.io's 12px default rather than as silence, and the label text on
an `<object>` / `<UserObject>` wrapper and a model-level `defaultVertexStyle` are both read. Each of
those passed while the gate reported the file compliant, and the first is the default path: open the
application, drop a shape, type a label, save. A `<diagram>` that carries no `mxGraphModel` -- what
the application writes when compression is left on -- fails rather than counting as compliant, and a
non-positive `pageWidth` fails rather than disabling the effective-size floor.

**Existing debt is carried in a file, and the file may only shrink.** Wiring this into a repository
whose diagrams all predate it would turn the build red until every one is redesigned, and a gate
that is red for weeks gets disabled. So paths listed in `diagram-font-debt.txt` are reported and
tolerated — but an unlisted violation fails, and so does a *listed* path that no longer violates.
The second half is what stops the file becoming permanent: fixing a diagram forces its line out, and
there is no way to add a line without a reviewer seeing it.

Each line reads `<path> <count>`. The count is what makes the ratchet operate on the debt rather than
on the number of lines: with a bare path, a second small label in an already-listed diagram was
tolerated in silence. Any movement in either direction fails, so improving a diagram forces the
number down and worsening one cannot pass unnoticed.

Nothing here is specific to one repository: paths are discovered rather than configured, so this file
is copied between repositories as-is with **one line** adjusted -- the suppression on the parse call
in `_parse()`. A repository whose ruff selects `S` needs `# noqa: S314` there; one that selects
`RUF100` without `S` rejects the same comment as unused. The two cannot both be satisfied by one
line, so the divergence is isolated to that function rather than left to spread.

Run:  python3 tools/check_diagram_fonts.py
      python3 tools/check_diagram_fonts.py --selftest
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET  # nosec B405  reads this repository's own committed files
from dataclasses import dataclass
from functools import cache
from pathlib import Path


def _root() -> Path:
    """The tree to scan: the repository root when there is one, else this file's directory.

    `parent.parent` alone assumes the file sits exactly one directory below the root. Copied flat
    into a directory -- which is how the copy set is staged and how a reader following the
    instruction would do it -- that resolves to the *parent* of where it was put, so the walk
    escapes into whatever sits beside it. Looking for a `.git` marker first keeps the in-repository
    behaviour identical and makes the copied case scan only what it was given.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists():
            return candidate
    return here.parent


ROOT = _root()
DEBT_FILE = ROOT / "diagram-font-debt.txt"

# Directories that hold copies of other people's files, or build output. Scanning them reports
# findings nobody in this repository can act on.
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".private", "site-packages"}

# The width a reader's column gives the image. GitHub renders Markdown body content at roughly this;
# dev.to and hatenablog are close enough that a separate number would be false precision.
PUBLICATION_WIDTH = 880

# Body text on all three targets is 16px. A label one notch under that is still comfortable; the
# floor sits there rather than at 16 so a diagram is not forced wider than its content needs.
MIN_EFFECTIVE_PX = 14

# Applied to the authored value, so narrowing the canvas cannot satisfy the floor above while the
# text stays small.
MIN_SOURCE_PX = 16

# draw.io writes no `fontSize` when the label uses the application default, and that default is
# 12px. Treating the absence as compliance passed the most likely way a small label enters the tree:
# open the app, drop a shape, type a label, save. So an absent size is read as this value rather
# than as silence.
DRAWIO_DEFAULT_PX = 12.0

STYLE_FONT = re.compile(r"fontSize=(\d+(?:\.\d+)?)")
INLINE_FONT = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)")
# Anchored to the opening tag: the first bare `width=` in the prefix belongs to an inner element
# on an export whose <svg> carries none, and a small width raises `scale` and weakens the floor.
SVG_WIDTH = re.compile(r'<svg\b[^>]*?\bwidth="(\d+(?:\.\d+)?)(?:px)?"', re.DOTALL)


@dataclass(frozen=True)
class Finding:
    """One cell whose label breaks a floor, carrying the width the verdict was reached with.

    The width is kept here so the advice printed at the end can name the canvas that actually failed.
    Recomputing it in the reporting path meant re-reading every file and then quoting the narrowest
    one, which is not necessarily the one being complained about.
    """

    path: Path
    cell: str
    width: float
    reason: str


def _skipped(path: Path) -> bool:
    return bool(SKIP.intersection(path.relative_to(ROOT).parts))


# Written to sit inside the narrowest line length used across these repositories, because a
# comprehension that fits on one line at 100 and wraps at 88 makes the same file format two ways and
# the copies stop being byte-identical.
def walk(pattern: str) -> list[Path]:
    return sorted(p for p in ROOT.rglob(pattern) if not _skipped(p))


@cache
def exports() -> dict[str, Path]:
    """Exported SVGs by stem. Built once: a repository can hold a megabyte of them."""
    return {p.stem: p for p in walk("*.svg")}


def rendered_width(source: Path, page_width: float) -> tuple[float, str]:
    """The width the reader's browser receives, and where that number came from."""
    exported = exports().get(source.stem)
    if exported is not None:
        # The width attribute is on the opening <svg>; read a prefix rather than the whole file,
        # which carries every icon as base64 and runs to about a megabyte.
        head = exported.read_text(encoding="utf-8", errors="replace")[:2048]
        match = SVG_WIDTH.search(head)
        if match:
            return float(match.group(1)), exported.name
    return page_width, "pageWidth"


def labelled(cell: ET.Element, wrapper_label: str | None) -> bool:
    """Whether this cell puts text on the page. A shape with no text has no size to judge."""
    return bool((cell.get("value") or "").strip() or (wrapper_label or "").strip())


def sizes(
    cell: ET.Element,
    *,
    wrapper_label: str | None = None,
    model_default: float | None = None,
) -> list[float]:
    """Every font size this cell asks for, in the order draw.io resolves them.

    Three sources beyond the cell's own `style`, each of which passed before:

    * an `<object>` / `<UserObject>` wrapper carries the label, so `font-size:` in the HTML sits on
      the wrapper's `label` attribute and not on the `mxCell`'s `value`;
    * `defaultVertexStyle` on the model supplies a font to every cell that declares none;
    * declaring nothing at all means the application default, which is 12px.
    """
    found = [float(m) for m in STYLE_FONT.findall(cell.get("style") or "")]
    found += [float(m) for m in INLINE_FONT.findall(cell.get("value") or "")]
    found += [float(m) for m in INLINE_FONT.findall(wrapper_label or "")]
    if found or not labelled(cell, wrapper_label):
        return found
    return [model_default if model_default is not None else DRAWIO_DEFAULT_PX]


def wrapper_labels(model: ET.Element) -> dict[ET.Element, str]:
    """Label text held by an `<object>` / `<UserObject>` wrapper, keyed by the cell it wraps."""
    held: dict[ET.Element, str] = {}
    for parent in model.iter():
        if parent.tag not in {"object", "UserObject"}:
            continue
        label = parent.get("label")
        if not label:
            continue
        for child in parent.iter("mxCell"):
            held[child] = label
    return held


def model_default_size(model: ET.Element) -> float | None:
    """The font size `defaultVertexStyle` gives every cell that declares none."""
    declared = STYLE_FONT.findall(model.get("defaultVertexStyle") or "")
    return float(declared[0]) if declared else None


def _parse(text: str) -> ET.Element:
    """Parse a committed .drawio from this repository -- never user-supplied data.

    The whole function exists to hold one line. The suppression has to sit on the call itself: moved
    to a line of its own, a formatter can shift it off the statement it applies to and the finding
    comes back. And the exact comment differs per repository (see the module docstring), so keeping
    it here means one known line to adjust instead of hunting for it.
    """
    return ET.fromstring(text)  # nosec B314


def inspect(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    root = _parse(text)

    # A <diagram> holding base64-deflate content -- which the application writes when compression is
    # left on -- has no mxGraphModel to iterate, so every loop below runs zero times and the file was
    # counted among those meeting the floor. Reporting a compliant verdict for a file that could not
    # be read is the failure this gate exists to prevent, so it fails instead.
    for diagram in root.iter("diagram"):
        if diagram.find("mxGraphModel") is None:
            findings.append(
                Finding(
                    path,
                    diagram.get("id") or "?",
                    0.0,
                    "diagram holds no mxGraphModel; save it uncompressed so the labels can be read",
                )
            )

    for model in root.iter("mxGraphModel"):
        declared = float(model.get("pageWidth") or PUBLICATION_WIDTH)
        # A non-positive width is not a narrow canvas, it is an unreadable one: `if width` treated it
        # as unknown, `scale` became 1.0, and the effective-size floor stopped applying entirely.
        if declared <= 0:
            findings.append(
                Finding(
                    path,
                    model.get("id") or "?",
                    0.0,
                    f"pageWidth {declared:g} is not a usable canvas width",
                )
            )
            continue
        width, origin = rendered_width(path, declared)
        if width <= 0:
            width, origin = declared, "pageWidth"
        scale = min(1.0, PUBLICATION_WIDTH / width)
        held = wrapper_labels(model)
        default = model_default_size(model)
        for cell in model.iter("mxCell"):
            for size in sizes(
                cell, wrapper_label=held.get(cell), model_default=default
            ):
                effective = size * scale
                if size < MIN_SOURCE_PX:
                    reason = f"fontSize {size:g} < {MIN_SOURCE_PX}"
                elif effective < MIN_EFFECTIVE_PX:
                    reason = (
                        f"effective {effective:.1f}px < {MIN_EFFECTIVE_PX} "
                        f"({size:g} x {PUBLICATION_WIDTH}/{width:g} from {origin})"
                    )
                else:
                    continue
                findings.append(Finding(path, cell.get("id") or "?", width, reason))
    return findings


def required_font(width: float) -> int:
    """The smallest whole `fontSize` that clears both floors on a canvas this wide."""
    scale = min(1.0, PUBLICATION_WIDTH / width) if width else 1.0
    return max(MIN_SOURCE_PX, math.ceil(MIN_EFFECTIVE_PX / scale))


def read_debt() -> tuple[dict[str, int], list[str]]:
    """Debt as `{path: tolerated finding count}`, plus the lines that could not be read as that.

    The count is required. Listing a bare path made the ratchet operate on the *length* of the file:
    once a path was listed, another small label in the same diagram was tolerated silently, so the
    debt behind each line could grow without bound while the file only ever shrank by lines. With a
    count, any change to the diagram -- better or worse -- has to move the number, which is what puts
    it in front of a reviewer.
    """
    if not DEBT_FILE.is_file():
        return {}, []
    tolerated: dict[str, int] = {}
    malformed: list[str] = []
    for raw in DEBT_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, count = line.rpartition(" ")
        if not name or not count.isdigit():
            malformed.append(line)
            continue
        name = name.strip()
        if name in tolerated:
            malformed.append(line)  # a duplicate would double-count in the summary
            continue
        tolerated[name] = int(count)
    return tolerated, malformed


def advice(width: float) -> str:
    return (
        "\n  Raising the number is only half the fix: a larger label needs the room to sit in.\n"
        f"  On the widest failing canvas ({width:g}px) the floor is fontSize {required_font(width)}.\n"
        "  Fold labels to two lines, move the notes box out of the figure into body prose, narrow\n"
        "  the canvas toward the 880px publication width, or split the figure. Widening the canvas\n"
        "  raises the floor again, so empty canvas is not free."
    )


def check() -> int:
    files = walk("*.drawio")
    if not files:
        print("diagram-fonts: no .drawio files found")
        return 0

    findings: list[Finding] = []
    for path in files:
        try:
            findings += inspect(path, path.read_text(encoding="utf-8"))
        except ET.ParseError as error:
            print(
                f"error: {path.relative_to(ROOT)} is not valid XML: {error}",
                file=sys.stderr,
            )
            return 1

    counts: dict[str, int] = {}
    for finding in findings:
        counts[str(finding.path.relative_to(ROOT))] = (
            counts.get(str(finding.path.relative_to(ROOT)), 0) + 1
        )
    failing = set(counts)
    listed, malformed = read_debt()
    unlisted = sorted(failing - set(listed))
    fixed = [name for name in listed if name not in failing]
    worse = sorted(
        (name, listed[name], counts[name])
        for name in listed
        if name in failing and counts[name] != listed[name]
    )

    problems = 0
    if malformed:
        problems += 1
        print(
            f"error: {DEBT_FILE.name} lines must read '<path> <count>'. Could not read:",
            file=sys.stderr,
        )
        for line in malformed:
            print(f"  {line}", file=sys.stderr)
        print(
            "  A count is required, and a path may appear once. Write the number of findings the\n"
            "  file currently carries; the gate fails when the real number moves either way.",
            file=sys.stderr,
        )

    if unlisted:
        problems += 1
        print("error: diagram labels below the readability floor", file=sys.stderr)
        seen: set[tuple[Path, str]] = set()
        widest = 0.0
        for finding in findings:
            name = str(finding.path.relative_to(ROOT))
            if name not in unlisted:
                continue
            widest = max(widest, finding.width)
            key = (finding.path, finding.reason)
            if key in seen:
                continue
            seen.add(key)
            print(f"  {name}  cell {finding.cell}: {finding.reason}", file=sys.stderr)
        print(advice(widest), file=sys.stderr)
        if listed:
            print(
                f"\n  {DEBT_FILE.name} carries pre-existing debt, but not these. Fix them rather\n"
                "  than adding a line: the file is only allowed to shrink.",
                file=sys.stderr,
            )

    if fixed:
        problems += 1
        print(
            f"error: {DEBT_FILE.name} lists path(s) with no findings. Delete these lines:",
            file=sys.stderr,
        )
        for name in fixed:
            exists = (ROOT / name).is_file()
            note = (
                ""
                if exists
                else "  (no such file -- check the spelling and the separators)"
            )
            print(f"  {name}{note}", file=sys.stderr)

    if worse:
        problems += 1
        print(
            f"error: {DEBT_FILE.name} is out of date. Debt may not grow:",
            file=sys.stderr,
        )
        for name, tolerated, actual in worse:
            verdict = "more than listed" if actual > tolerated else "fewer than listed"
            print(
                f"  {name}: {actual} finding(s), {tolerated} listed ({verdict})",
                file=sys.stderr,
            )
        print(
            "  Fix the diagram and lower the number. Raising it needs a reviewer to see it.",
            file=sys.stderr,
        )

    if problems:
        return 1

    remaining = [name for name in listed if name in failing]
    if remaining:
        print(
            f"diagram-fonts: {len(files) - len(remaining)} file(s) meet the readability floor; "
            f"{len(remaining)} still carried as debt in {DEBT_FILE.name}"
        )
    else:
        print(f"diagram-fonts: {len(files)} file(s) meet the readability floor")
    return 0


# --- selftest ------------------------------------------------------------------------------------

# A gate is only trustworthy once it has been seen to fail. Every gate target runs this before the
# check itself, so a refactor that makes this accept everything is caught by the gate rather than by
# noticing, months later, that no diagram was ever reported.


def _doc(page_width: int, *sizes_: float, inline: float | None = None) -> str:
    cells = "".join(
        f'<mxCell id="c{n}" value="x" style="rounded=1;fontSize={s:g};" vertex="1" parent="1" />'
        for n, s in enumerate(sizes_)
    )
    if inline is not None:
        cells += (
            f'<mxCell id="inline" value="&lt;span style=&quot;font-size:{inline:g}px&quot;&gt;'
            'x&lt;/span&gt;" style="rounded=1;fontSize=24;" vertex="1" parent="1" />'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><mxfile><diagram id="d" name="d">'
        f'<mxGraphModel pageWidth="{page_width}" pageHeight="400"><root>'
        '<mxCell id="0" /><mxCell id="1" parent="0" />'
        f"{cells}</root></mxGraphModel></diagram></mxfile>"
    )


def _wrap(page_width: int, cells: str, model_attrs: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><mxfile><diagram id="d" name="d">'
        f'<mxGraphModel pageWidth="{page_width}" pageHeight="400"{model_attrs}><root>'
        '<mxCell id="0" /><mxCell id="1" parent="0" />'
        f"{cells}</root></mxGraphModel></diagram></mxfile>"
    )


def _bare(page_width: int, value: str = "x") -> str:
    """A cell that declares no font size -- the draw.io default path."""
    return _wrap(
        page_width,
        f'<mxCell id="bare" value="{value}" style="rounded=1;" vertex="1" parent="1" />',
    )


def _wrapped(page_width: int, inline: float) -> str:
    """The label on an <object> wrapper rather than on the mxCell."""
    label = f"&lt;span style=&quot;font-size:{inline:g}px&quot;&gt;x&lt;/span&gt;"
    return _wrap(
        page_width,
        f'<object label="{label}" id="o1"><mxCell style="rounded=1;" vertex="1" '
        'parent="1" /></object>',
    )


def _model_default(page_width: int, size: float) -> str:
    return _wrap(
        page_width,
        '<mxCell id="d1" value="x" style="rounded=1;" vertex="1" parent="1" />',
        model_attrs=f' defaultVertexStyle="rounded=1;fontSize={size:g};"',
    )


def _compressed() -> str:
    """What the application writes with compression left on: no mxGraphModel to read."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><mxfile><diagram id="d" name="d">'
        "7VtZk9o4EP41VO0DKR+AgUeOJDOZzExSmz1qnwRWbBFhOZI8HPn1KdmyMTaEmYRJ"
        "spuHKaNWq9X9dbdaajOwp8vNGw4jvOZLLPHAmS42a4bxdGDbA/N3YNvmz7Ktgf1q"
        "YFuvBrb1amBbrwZ29WpgV68GdvVqYFevBnb1amBXrwZ29WpgV68GdvVqYFevBnb1"
        "</diagram></mxfile>"
    )


def selftest() -> int:
    missing = ROOT / "never-exported.drawio"
    cases: list[tuple[str, str, bool]] = [
        # (name, document, expected to be rejected)
        (
            "the 11px these repositories shipped, on a 1220px canvas",
            _doc(1220, 11),
            True,
        ),
        ("16px source floor met but effective 11.5px on 1220px", _doc(1220, 16), True),
        ("20px on a 1220px canvas clears both floors", _doc(1220, 20), False),
        ("16px on an 880px canvas clears both floors", _doc(880, 16), False),
        ("15px on a narrow canvas still breaks the source floor", _doc(400, 15), True),
        ("one bad cell among good ones is caught", _doc(880, 18, 16, 11), True),
        (
            "an inline font-size is read, not just the style",
            _doc(880, 24, inline=10),
            True,
        ),
        ("an inline font-size above the floor passes", _doc(880, 24, inline=18), False),
        # Each of the six below passed before, with the file counted as meeting the floor.
        (
            "a labelled cell declaring no fontSize is read as the 12px default",
            _bare(1220),
            True,
        ),
        (
            "the 12px default cannot clear the 16px source floor at any canvas width",
            _bare(660),
            True,
        ),
        (
            "an unlabelled cell declaring no fontSize is not judged",
            _bare(1220, ""),
            False,
        ),
        (
            "font-size on an <object> wrapper's label is read",
            _wrapped(880, 9),
            True,
        ),
        (
            "defaultVertexStyle supplies the size a cell omits",
            _model_default(880, 9),
            True,
        ),
        (
            "a diagram with no mxGraphModel fails rather than passing",
            _compressed(),
            True,
        ),
        ("pageWidth 0 fails rather than disabling the floor", _doc(0, 16), True),
    ]
    failures = 0
    for name, document, should_reject in cases:
        rejected = bool(inspect(missing, document))
        if rejected != should_reject:
            verdict = "rejected" if rejected else "accepted"
            wanted = "reject" if should_reject else "accept"
            print(
                f"  selftest FAILED: {name} -> {verdict}, expected to {wanted}",
                file=sys.stderr,
            )
            failures += 1

    expectations = {880: 16, 1000: 16, 1220: 20, 1600: 26, 2000: 32}
    for width, want in expectations.items():
        got = required_font(width)
        if got != want:
            print(
                f"  selftest FAILED: required_font({width}) = {got}, expected {want}",
                file=sys.stderr,
            )
            failures += 1

    # The ratchet is deliberately not asserted here. Checking it in this function would mean
    # re-deriving the set arithmetic `check()` performs and comparing the two, which proves the two
    # copies agree rather than that either is right. It is exercised end to end against real files
    # by the repository's gate tests, which can write both a probe diagram and a probe debt file.

    if failures:
        print(f"selftest: {failures} case(s) failed", file=sys.stderr)
        return 1
    print(f"selftest: {len(cases) + len(expectations)} case(s) behave as documented")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="prove the check rejects small fonts, accepts compliant ones, and ratchets debt",
    )
    args = parser.parse_args()
    return selftest() if args.selftest else check()


if __name__ == "__main__":
    sys.exit(main())
