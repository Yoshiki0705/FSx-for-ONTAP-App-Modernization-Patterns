#!/usr/bin/env python3
"""Generate the synthetic seed/ data for the DocIntake sample app (stdlib only).

seed/ holds synthetic documents the Worker reads and the Probe references. It is written once at
stage 0 and never changed after, so the single-volume invariant can compare it across boundaries.
The data is fully synthetic: no personal names, account IDs or secrets, so it passes gitleaks and
make audit.

  make-seed.py --out <dir>     write seed/ under <dir>
  make-seed.py --selftest      write to a temp dir and verify the layout, then clean up

Layout written:
  seed/inbox/doc-001.txt .. doc-005.txt   documents the Worker summarizes
  seed/docs/index.json                    the lower-case real index the case-sensitivity reference
                                          ("Docs/Index.json") is contrasted against
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DOCUMENTS = {
    "doc-001.txt": "Quarterly plan for the sample workload.\nNo real data here.\n",
    "doc-002.txt": "Design note: the store is chosen by one config key.\n",
    "doc-003.txt": "Checklist: inbox, index, reports.\n",
    "doc-004.txt": "A longer synthetic document.\n" + ("filler line\n" * 20),
    "doc-005.txt": "Edge case: trailing spaces and a tab\tcharacter.\n",
}


def write_seed(out: Path) -> list[Path]:
    written: list[Path] = []
    inbox = out / "seed" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for name, body in DOCUMENTS.items():
        path = inbox / name
        path.write_text(body, encoding="utf-8")
        written.append(path)

    docs = out / "seed" / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    index = docs / "index.json"
    index.write_text(
        json.dumps({"documents": sorted(DOCUMENTS)}, indent=2) + "\n", encoding="utf-8"
    )
    written.append(index)
    return written


def selftest() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        written = write_seed(out)
        if len(written) != len(DOCUMENTS) + 1:
            print("selftest: unexpected number of files written", file=sys.stderr)
            return 1
        if not (out / "seed" / "docs" / "index.json").exists():
            print("selftest: index.json missing", file=sys.stderr)
            return 1
    print("selftest: seed layout written and verified")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="directory to write seed/ under")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.out:
        print("make-seed: --out is required", file=sys.stderr)
        return 2
    written = write_seed(Path(args.out))
    print(f"make-seed: wrote {len(written)} file(s) under {args.out}/seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
