#!/usr/bin/env python3
"""Compare the output of two Worker runs (for example the legacy out/s0 and the migrated out/s2m).

Each of --a and --b is a directory that holds index.json and reports/ (a stage folder rebuilt by
collect_out.py). The criterion is ADR-4 of the migration: the index must be JSON-equivalent (same
keys, same values, same array order), not byte-identical; a byte-length difference is reported.
Reports are compared by SHA-256, file by file.

Printed lines (one value per line, the order the guide shows):
  <sha256[:16]> <bytes> <label>/<path>        one per file
  json equal (ordered keys/arrays): True|False | py == : True|False
  top keys: [...] [...]
  names order: [...] [...]                    document names, in array order
  whitespace-stripped bytes equal: True|False
  len <a> <b> diff bytes <n>
  <report path> True|False <sha256[:12]>      one per report name found in either side
  seed expected==got: True|False <count>      with --seed-expected and --dump
  aggregate: <16 hex>                         with --dump

--seed-expected FILE holds lines "SEED <path> <size> <sha256>" (backslashes in the path are read
as slashes); --dump is the host/read-out.sh output (text or ssm_run.py JSON) whose "== seed sha256"
section is compared with it.

Exit codes: 0 every check holds; 1 a check failed (the lines above say which); 2 bad argument or a
missing file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect_out
import common


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_stage(root: Path) -> dict[str, bytes]:
    """Relative path -> content for index.json and every file under reports/."""
    files: dict[str, bytes] = {}
    index = root / "index.json"
    if not index.is_file():
        raise FileNotFoundError(f"{index} not found")
    files["index.json"] = index.read_bytes()
    reports = root / "reports"
    if reports.is_dir():
        for p in sorted(reports.rglob("*")):
            if p.is_file():
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def document_names(obj: object) -> list | None:
    docs = obj.get("documents") if isinstance(obj, dict) else None
    if not isinstance(docs, list) or not all(isinstance(d, dict) for d in docs):
        return None
    return [d.get("name") for d in docs]


def parse_seed_expected(text: str) -> dict[str, str]:
    expected: dict[str, str] = {}
    for line in text.splitlines():
        tok = line.split()
        if len(tok) == 4 and tok[0] == "SEED":
            expected[tok[1].replace("\\", "/")] = tok[3]
    return expected


def compare(
    a: dict[str, bytes],
    b: dict[str, bytes],
    label_a: str = "a",
    label_b: str = "b",
    seed: tuple[dict[str, str], dict[str, str], str] | None = None,
) -> tuple[list[str], bool]:
    """(lines to print, all checks hold)."""
    lines: list[str] = []
    ok = True
    for label, files in ((label_a, a), (label_b, b)):
        for k in sorted(files):
            lines.append(f"{sha(files[k])[:16]} {len(files[k])} {label}/{k}")
    ia, ib = a["index.json"].decode(), b["index.json"].decode()
    try:
        ja = json.loads(ia, object_pairs_hook=OrderedDict)
        jb = json.loads(ib, object_pairs_hook=OrderedDict)
    except ValueError as exc:
        lines.append(f"index.json is not valid JSON on one side: {exc}")
        return lines, False
    ordered_equal = json.dumps(ja) == json.dumps(jb)
    lines.append(
        f"json equal (ordered keys/arrays): {ordered_equal} | py == : {json.loads(ia) == json.loads(ib)}"
    )
    ok &= ordered_equal
    lines.append(f"top keys: {list(ja)} {list(jb)}")
    lines.append(f"names order: {document_names(ja)} {document_names(jb)}")
    stripped_equal = re.sub(r"\s", "", ia) == re.sub(r"\s", "", ib)
    lines.append(f"whitespace-stripped bytes equal: {stripped_equal}")
    lines.append(f"len {len(ia)} {len(ib)} diff bytes {len(ia) - len(ib)}")
    reports = sorted(
        {k for k in a if k.startswith("reports/")}
        | {k for k in b if k.startswith("reports/")}
    )
    for k in reports:
        same = k in a and k in b and sha(a[k]) == sha(b[k])
        ok &= same
        digest = sha(a[k])[:12] if k in a else "missing-in-a"
        lines.append(f"{k} {same} {digest}")
    if seed is not None:
        expected, got, aggregate = seed
        match = expected == got
        ok &= match
        lines.append(f"seed expected==got: {match} {len(got)}")
        lines.append(f"aggregate: {aggregate}")
    return lines, ok


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--a", required=True, help="stage folder A (for example the legacy out/s0)"
    )
    ap.add_argument(
        "--b", required=True, help="stage folder B (for example the migrated out/s2m)"
    )
    ap.add_argument("--seed-expected", help="file with SEED lines from the seed script")
    ap.add_argument(
        "--dump",
        help="read-out.sh output with the seed sections (needs --seed-expected)",
    )
    args = ap.parse_args(argv)
    try:
        if bool(args.seed_expected) != bool(args.dump):
            raise ValueError("--seed-expected and --dump must be given together")
        a, b = read_stage(Path(args.a)), read_stage(Path(args.b))
        seed = None
        if args.seed_expected:
            expected = parse_seed_expected(Path(args.seed_expected).read_text())
            if not expected:
                raise ValueError(f"{args.seed_expected} has no SEED line")
            got, aggregate = collect_out.parse_seed(
                collect_out.dump_text(Path(args.dump).read_text())
            )
            seed = (expected, got, aggregate)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"compare_worker_out: {exc}\n")
        return common.EXIT_BAD_ARGUMENT
    lines, ok = compare(a, b, Path(args.a).name, Path(args.b).name, seed)
    print("\n".join(lines))
    return common.EXIT_OK if ok else common.EXIT_REMOTE_FAILED


if __name__ == "__main__":
    sys.exit(main())
