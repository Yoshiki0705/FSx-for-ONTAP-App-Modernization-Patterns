#!/usr/bin/env python3
"""Rebuild recorded Worker output from the dump that host/read-out.sh prints.

host/read-out.sh prints, for each stage, "== B64 <stage>/<relative path>" followed by one base64
line. This script writes each file to <out-dir>/<stage>/<relative path>, so the two stage folders
can be compared with compare_worker_out.py. The input is either the raw text or the JSON that
ssm_run.py saves (its StandardOutputContent is used).

A stage name must match s<digit>[a-z]{0,2}; a relative path may only have segments of
[A-Za-z0-9._-] and no . or .. segment, so a dump cannot write outside --out-dir. An existing file
is never overwritten (exit 2). Nothing is deleted.

Exit codes: 0 done; 2 bad argument, unreadable dump, a path that fails validation, or an existing
output file; 3 the dump holds no file.

Example:
  collect_out.py --dump read-out.json --out-dir .private/runs/s0w-20261012T102314Z
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

B64_RE = re.compile(r"^== B64 (\S+)\n(\S*)$", re.MULTILINE)


def dump_text(raw: str) -> str:
    """The dump text: the raw text, or StandardOutputContent of an ssm_run.py result."""
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            return raw
        if isinstance(obj, dict) and isinstance(obj.get("StandardOutputContent"), str):
            return obj["StandardOutputContent"]
    return raw


def parse_dump(text: str) -> dict[str, bytes]:
    """Map "<stage>/<relative path>" to content. The key is validated; bad base64 raises."""
    files: dict[str, bytes] = {}
    for m in B64_RE.finditer(text.replace("\r\n", "\n")):
        key, payload = m.group(1), m.group(2)
        stage, _, rel = key.partition("/")
        common.validate_stage_name(stage)
        common.validate_relpath(rel)
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{key}: not valid base64") from exc
        if key in files:
            raise ValueError(f"{key}: appears twice in the dump")
        files[key] = data
    return files


def parse_seed(text: str) -> tuple[dict[str, str], str]:
    """(path -> sha256, aggregate) from the "== seed sha256" and "== seed aggregate" sections."""
    text = text.replace("\r\n", "\n")
    if "== seed sha256\n" not in text or "== seed aggregate\n" not in text:
        raise ValueError("the dump has no seed sections")
    section = text.split("== seed sha256\n", 1)[1].split("== seed aggregate", 1)[0]
    observed: dict[str, str] = {}
    for line in section.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            observed[parts[1]] = parts[0]
    tail = text.split("== seed aggregate\n", 1)[1].split()
    return observed, tail[0] if tail else ""


def write_files(files: dict[str, bytes], out_dir: Path) -> list[Path]:
    targets = [out_dir / key for key in sorted(files)]
    for t in targets:
        if t.exists():
            raise FileExistsError(f"{t} already exists; not overwriting")
    written = []
    for key, t in zip(sorted(files), targets, strict=True):
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(files[key])
        written.append(t)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--dump",
        required=True,
        help="read-out.sh output (text) or ssm_run.py result (JSON)",
    )
    ap.add_argument(
        "--out-dir", required=True, help="directory that receives <stage>/..."
    )
    args = ap.parse_args(argv)
    try:
        text = dump_text(Path(args.dump).read_text())
        files = parse_dump(text)
        if not files:
            sys.stderr.write("collect_out: the dump holds no '== B64' entry\n")
            return 3
        written = write_files(files, Path(args.out_dir))
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"collect_out: {exc}\n")
        return common.EXIT_BAD_ARGUMENT
    for key in sorted(files):
        print(f"{len(files[key]):>8}  {key}")
    print(f"wrote {len(written)} file(s) under {args.out_dir}")
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
