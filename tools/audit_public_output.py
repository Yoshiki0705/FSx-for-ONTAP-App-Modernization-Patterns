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

# Each rule: (category, compiled pattern, message). Patterns are case-sensitive unless noted.
RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("naming", re.compile(r"\bFSxN\b"), "use 'FSx for ONTAP'"),
    ("naming", re.compile(r"\bFSx (?:ONTAP|NetApp)\b"), "use 'FSx for ONTAP'"),
    (
        "naming",
        # Bare "FSx": allowed only as part of a product name, a CloudFormation/IAM identifier,
        # or a hyphenated repository name such as FSx-for-ONTAP-App-Modernization-Patterns.
        re.compile(
            r"(?<![:\w/-])FSx\b(?!::|-| for (?:NetApp )?ONTAP| for Windows File Server"
            r"| for Lustre| for OpenZFS)"
        ),
        "bare 'FSx'; write 'FSx for ONTAP' (or the full product name)",
    ),
    ("naming", re.compile(r"\bS3 APs?\b"), "write 'S3 Access Points' in full"),
    (
        "naming",
        re.compile(r"Workload Factory|NetApp Console|BlueXP", re.IGNORECASE),
        "do not propose this product; use the native equivalent",
    ),
    (
        "neutrality",
        re.compile(
            r"\bbest\b(?! practices?)|\bbeats?\b|\binferior\b|competing tools?|game-changer"
            r"|競合ツール|より優れている|優位性",
            re.IGNORECASE,
        ),
        "vendor-versus or superiority framing",
    ),
    (
        "pii",
        re.compile(r"(?<![\d.])(?!123456789012)\d{12}(?![\d.])"),
        "12-digit number (AWS account ID?); use 123456789012",
    ),
    (
        "pii",
        re.compile(
            r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}"
            r"(?![\d/])"
        ),
        "private IP address; use 10.0.x.x or <management-ip>",
    ),
    (
        "pii",
        re.compile(
            r"[\w.+-]+@(?!example\.(?:com|org|net)\b)[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b",
            re.IGNORECASE,
        ),
        "e-mail address; use (internal reviewer) or name@example.com",
    ),
    (
        "pii",
        re.compile(r"/Users/[A-Za-z]|/home/[a-z]"),
        "personal path; use a relative path",
    ),
    (
        "pii",
        re.compile(r"\bcase\s*(?:#|no\.?|number)?\s*\d{5,}", re.IGNORECASE),
        "support case number; say 'filed with the vendor (no number)'",
    ),
    (
        "pii",
        re.compile(r"\b[A-Z]{2,4}-I-\d{3,}\b"),
        "vendor-internal ticket ID; say 'an internal product request (tracked)'",
    ),
]


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
    findings: list[tuple[int, str, str, str]] = []
    for lineno, line in enumerate(lines, start=1):
        line_allowed: set[str] = set()
        for match in LINE_ALLOW.finditer(line):
            line_allowed |= set(match.group(1).split(","))
        for category, pattern, message in RULES:
            if (
                category in file_allowed
                or category in line_allowed
                or "all" in line_allowed
            ):
                continue
            for hit in pattern.finditer(line):
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
]


def selftest() -> int:
    failures = []
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
