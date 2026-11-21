#!/usr/bin/env python3
"""Pre-publication audit for this public repository (stdlib only).

Four concerns, each of which reaches git history once and then cannot be taken back:

  1. naming      - "Amazon FSx for NetApp ONTAP" on first mention, "FSx for ONTAP" after.
                   Short forms are rejected, "S3 Access Points" is written in full, and three
                   products are never proposed.
  2. neutrality  - vendor-versus framing and superiority claims.
  3. pii         - account IDs, private IPs, e-mail addresses, personal paths, support case
                   numbers, and vendor-internal ticket IDs.
  4. hostnames   - is left to gitleaks (`make secrets`), which owns that rule.

Role-labeled callouts are checked by tools/check_role_labels.py, not here.

Files are listed with `git ls-files --cached --others --exclude-standard`, so anything the
.gitignore excludes (.private/, .kiro/, AIMF record directories, .venv) is never scanned and
anything that would be committed always is.

Two escape hatches:

    a line that quotes a forbidden form verbatim   <!-- allow:naming -->
    <!-- audit-file-allow: naming,neutrality,pii -->   (within the first 40 lines; for a file
                                                       whose job is to define these rules)

Run:  python3 tools/audit_public_output.py [--selftest]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# tools/ necessarily contains the patterns it searches for. scripts/guard_irreversible_ops.py is
# a verbatim copy from the Hub and is kept byte-identical rather than edited to satisfy this audit.
SKIP_PREFIXES = ("tools/", "scripts/guard_irreversible_ops.py")
SCAN_SUFFIXES = {".md", ".txt", ".yml", ".yaml", ".json", ".sh", ".ps1", ".toml", ".cs"}

FILE_ALLOW = re.compile(r"<!--\s*audit-file-allow:\s*([a-z,\s-]+?)\s*-->")
LINE_ALLOW = re.compile(r"(?:<!--|#|//)\s*allow:([a-z,-]+)")

# Word boundaries for product names. Python's \b treats Japanese characters as word characters,
# so \bFSxN\b misses "FSxNを使う" and "構成でFSxN". A boundary here means "not next to an ASCII
# letter, digit or underscore"; the underscore stays in the class so a configuration name such as
# FSxN_OnPre is not a prose violation. Do not go back to \b.
LEFT = r"(?<![A-Za-z0-9_])"
RIGHT = r"(?![A-Za-z0-9_])"

# Each rule: (category, compiled pattern, message, wrapped). Patterns are case-sensitive unless
# noted. A wrapped rule is judged on the line together with the lines its sentence continues onto
# (see context_window), because its pattern looks at the text before or after the name.
RULES: list[tuple[str, re.Pattern[str], str, bool]] = [
    ("naming", re.compile(LEFT + r"FSxN" + RIGHT), "use 'FSx for ONTAP'", False),
    (
        "naming",
        re.compile(LEFT + r"FSx (?:ONTAP|NetApp)" + RIGHT),
        "use 'FSx for ONTAP'",
        False,
    ),
    (
        "naming",
        # Bare "FSx": allowed only as part of a product name (including the "Amazon FSx" family
        # name, as in "Amazon FSx API"), a CloudFormation/IAM identifier, or a hyphenated
        # repository name such as FSx-for-ONTAP-App-Modernization-Patterns.
        re.compile(
            r"(?<![:/\-A-Za-z0-9_])(?<!Amazon )FSx"
            + RIGHT
            + r"(?!::|-| for (?:NetApp )?ONTAP| for Windows File Server"
            r"| for Lustre| for OpenZFS)"
        ),
        "bare 'FSx'; write 'FSx for ONTAP' (or the full product name)",
        True,
    ),
    ("naming", re.compile(r"\bS3 APs?\b"), "write 'S3 Access Points' in full", False),
    (
        "naming",
        re.compile(r"Workload Factory|NetApp Console|BlueXP", re.IGNORECASE),
        "do not propose this product; use the native equivalent",
        False,
    ),
    (
        "neutrality",
        re.compile(
            r"\bbest\b(?! practices?)|\bbeats?\b|\binferior\b|competing tools?|game-changer"
            r"|競合ツール|より優れている|優位性",
            re.IGNORECASE,
        ),
        "vendor-versus or superiority framing",
        False,
    ),
    (
        "pii",
        # A standalone 12-digit run. The hex-letter lookarounds skip a digit run that sits inside a
        # longer hex token such as a commit SHA pinned with `uses: ...@<sha>`: a 40-char SHA often
        # contains 12 consecutive digits that are not an account ID. A real account ID is bounded by
        # non-hex characters on both sides.
        re.compile(r"(?<![\d.a-fA-F])(?!123456789012)\d{12}(?![\d.a-fA-F])"),
        "12-digit number (AWS account ID?); use 123456789012",
        False,
    ),
    (
        "pii",
        re.compile(
            r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}"
            r"(?![\d/])"
        ),
        "private IP address; use 10.0.x.x or <management-ip>",
        False,
    ),
    (
        "pii",
        re.compile(
            r"[\w.+-]+@(?!example\.(?:com|org|net)\b)(?!\d+x\.(?:png|jpe?g|webp|gif)\b)[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b",
            re.IGNORECASE,
        ),
        "e-mail address; use (internal reviewer) or name@example.com",
        False,
    ),
    (
        "pii",
        re.compile(r"/Users/[A-Za-z]|/home/[a-z]"),
        "personal path; use a relative path",
        False,
    ),
    (
        "pii",
        re.compile(r"\bcase\s*(?:#|no\.?|number)?\s*\d{5,}", re.IGNORECASE),
        "support case number; say 'filed with the vendor (no number)'",
        False,
    ),
    (
        "pii",
        re.compile(r"\b[A-Z]{2,4}-I-\d{3,}\b"),
        "vendor-internal ticket ID; say 'an internal product request (tracked)'",
        False,
    ),
]

FENCE = re.compile(r"^\s*(?:```|~~~)")
# Leading comment or quote markers (`#`, `>`) do not stop a sentence from continuing.
COMMENT_PREFIX = re.compile(r"^(?:\s*[#>])*\s*")
LIST_START = re.compile(r"^(?:[-*+]|\d+[.)])\s")
# "FSx for NetApp ONTAP" folded at every space leaves 3 lines after "FSx".
MAX_LOOKAHEAD = 3


def sentence_continuations(lines: list[str]) -> tuple[list[str], list[bool]]:
    """Per line: its text without comment markers, and whether it continues the previous line.

    A line continues the previous one only when both are running text. A blank line, a list item
    start, a table row, and a code fence marker end the sentence.
    """
    contents: list[str] = []
    kinds: list[str] = []
    for line in lines:
        content = COMMENT_PREFIX.sub("", line).rstrip()
        contents.append(content)
        if FENCE.match(line):
            kinds.append("fence")
        elif not content:
            kinds.append("blank")
        elif content.startswith("|"):
            kinds.append("table")
        elif LIST_START.match(content):
            kinds.append("list")
        else:
            kinds.append("text")
    continues = [
        i > 0 and kinds[i] == "text" and kinds[i - 1] in ("text", "list")
        for i in range(len(lines))
    ]
    return contents, continues


def context_window(
    index: int, lines: list[str], contents: list[str], continues: list[bool]
) -> tuple[int, int, str]:
    """Return (start, end, window): line `index` with the lines its sentence continues onto.

    Only a match that starts in window[start:end] belongs to this line, so a name that starts on
    another line is reported there and never twice.
    """
    prefix = contents[index - 1] + " " if continues[index] else ""
    line = lines[index].rstrip()
    suffix = ""
    following = index + 1
    while (
        following < len(lines)
        and following - index <= MAX_LOOKAHEAD
        and continues[following]
    ):
        suffix += " " + contents[following]
        following += 1
    return len(prefix), len(prefix) + len(line), prefix + line + suffix


def file_allowances(lines: list[str]) -> set[str]:
    allowed: set[str] = set()
    for line in lines[:40]:
        match = FILE_ALLOW.search(line)
        if match:
            allowed |= {
                part.strip() for part in match.group(1).split(",") if part.strip()
            }
    return allowed


def audit_text(text: str) -> list[tuple[int, str, str, str]]:
    """Return (lineno, category, matched text, message) for every finding in text."""
    lines = text.splitlines()
    file_allowed = file_allowances(lines)
    contents, continues = sentence_continuations(lines)
    findings: list[tuple[int, str, str, str]] = []
    for lineno, line in enumerate(lines, start=1):
        line_allowed: set[str] = set()
        for match in LINE_ALLOW.finditer(line):
            line_allowed |= set(match.group(1).split(","))
        for category, pattern, message, wrapped in RULES:
            if (
                category in file_allowed
                or category in line_allowed
                or "all" in line_allowed
            ):
                continue
            if wrapped:
                start, end, window = context_window(
                    lineno - 1, lines, contents, continues
                )
                hits = [m for m in pattern.finditer(window) if start <= m.start() < end]
            else:
                hits = list(pattern.finditer(line))
            for hit in hits:
                findings.append((lineno, category, hit.group(0), message))
    return findings


def candidate_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    files = []
    for rel in sorted(set(listed)):
        if rel.startswith(SKIP_PREFIXES):
            continue
        path = ROOT / rel
        if path.suffix in SCAN_SUFFIXES and path.is_file():
            files.append(path)
    return files


SELFTEST_REJECT = [
    ("naming", "We moved the share to FSxN last week."),
    ("naming", "The FSx ONTAP volume was resized."),
    ("naming", "Create the FSx file system first."),
    ("naming", "Read objects through the S3 AP alias."),
    ("naming", "Provision it from BlueXP."),
    ("neutrality", "This is the best option for every team."),
    ("neutrality", "競合ツールとの比較"),
    ("pii", "arn:aws:iam::210987654321:role/x"),  # gitleaks:allow (negative control)
    ("pii", "Mount from 10.1.23.45 over NFS."),  # gitleaks:allow (negative control)
    ("pii", "Ask al@corp-mail.co.jp."),  # gitleaks:allow (negative control)
    ("pii", "Logs are in /Users/alice/work."),
    ("pii", "Opened support case 1234567890."),
]
SELFTEST_ACCEPT = [
    "Amazon FSx for NetApp ONTAP stores the data; FSx for ONTAP serves SMB and NFS.",
    "Type: AWS::FSx::FileSystem",
    "FSx for ONTAP S3 Access Points expose the volume to AWS Lambda.",
    "Use account 123456789012 and CIDR 10.0.0.0/16, host 10.0.x.x.",
    "Contact name@example.com.",
    "Follow AWS best practices for IAM.",
    "Quoted title FSxN tips <!-- allow:naming -->",
    "See github.com/Yoshiki0705/FSx-for-ONTAP-App-Modernization-Patterns.",
    "npm install -g markdownlint-cli2@0.22.1",
    "[PNG](../_assets/images/png/diagram@2x.png)",  # image scale suffix, not an address
    # 12-digit run inside a 40-char commit SHA pin, not an account ID.
    "uses: ossf/scorecard-action@fa7117db83c52d787ae5a89f123456789011d78e # v2.5.0",  # gitleaks:allow
]


# (text, expected number of naming findings). The first group pins the word boundary: a Japanese
# particle or a Japanese word next to the name is not a boundary-breaking character, while an
# ASCII letter, digit or underscore is.
SELFTEST_NAMING_COUNTS = [
    ("FSxNを使う", 1),
    ("構成でFSxN", 1),
    ("Use FSxN here.", 1),
    ("FSxN_OnPre", 0),
    ("XFSxN", 0),
    ("FSxNN", 0),
    ("FSxを使う", 1),
    ("構成でFSx", 1),
    ("Use FSx here.", 1),
    ("XFSx", 0),
    ("FSx_OnPre", 0),
    # A name wrapped over lines is judged as one sentence.
    ("The FSx for\nONTAP volume.", 0),
    ("Amazon\nFSx API", 0),
    ("Amazon FSx API", 0),
    ("The FSx for NetApp\nONTAP volume.", 0),
    # A blank line, a list item, a table row or a fence ends the sentence.
    ("The FSx for\n\nONTAP volume.", 1),
    ("uses FSx\n- for ONTAP", 1),
    ("uses FSx\n| for ONTAP |", 1),
    ("uses FSx\n```\nfor ONTAP\n```", 1),
    # A comment or quote marker does not end the sentence.
    ("# The FSx for\n# ONTAP volume.", 0),
    ("> The FSx for\n> ONTAP volume.", 0),
    # One violation is counted once, on the line where the name starts.
    ("Use FSx\nAPI calls here.", 1),
]
# Folding a sentence at any whitespace must not change the count.
SELFTEST_FOLD_COUNTS = [
    ("Use Amazon FSx API calls here.", 0),
    ("Use FSx API calls here.", 1),
    ("The FSx for ONTAP volume is mounted.", 0),
    ("The FSx for NetApp ONTAP volume is mounted.", 0),
]


def naming_count(text: str) -> int:
    return sum(1 for found in audit_text(text) if found[1] == "naming")


def fold_variants(text: str) -> list[str]:
    """The sentence folded at each single space, then at every space."""
    words = text.split(" ")
    variants = [
        " ".join(words[:i]) + "\n" + " ".join(words[i:]) for i in range(1, len(words))
    ]
    variants.append("\n".join(words))
    return variants


def selftest_naming() -> list[str]:
    failures = []
    for text, expected in SELFTEST_NAMING_COUNTS:
        got = naming_count(text)
        if got != expected:
            failures.append(f"naming count {got}, expected {expected}: {text!r}")
    for text, expected in SELFTEST_FOLD_COUNTS:
        for variant in [text, *fold_variants(text)]:
            got = naming_count(variant)
            if got != expected:
                failures.append(f"fold count {got}, expected {expected}: {variant!r}")
    return failures


def selftest() -> int:
    failures = selftest_naming()
    for category, text in SELFTEST_REJECT:
        if not any(found[1] == category for found in audit_text(text)):
            failures.append(f"not rejected ({category}): {text}")
    for text in SELFTEST_ACCEPT:
        hits = audit_text(text)
        if hits:
            failures.append(f"false positive {hits}: {text}")
    if audit_text("<!-- audit-file-allow: naming -->\nFSxN here"):
        failures.append("file-level allowance ignored")
    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"selftest: {len(SELFTEST_REJECT)} rejected, {len(SELFTEST_ACCEPT)} accepted")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--selftest", action="store_true", help="prove the rules fire")
    args = parser.parse_args()
    if args.selftest:
        return selftest()

    files = candidate_files()
    total = 0
    for path in files:
        rel = path.relative_to(ROOT)
        for lineno, category, hit, message in audit_text(
            path.read_text(encoding="utf-8")
        ):
            print(f"{rel}:{lineno}: [{category}] {hit!r}: {message}", file=sys.stderr)
            total += 1
    if total:
        print(f"audit: {total} finding(s)", file=sys.stderr)
        return 1
    print(f"audit: {len(files)} file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
