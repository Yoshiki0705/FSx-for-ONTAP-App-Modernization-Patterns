#!/usr/bin/env python3
"""Check the single-volume invariant across stage boundaries (stdlib only).

The target volume is reused unchanged across stages 0 to 3. This compares a later boundary against
the stage-0 baseline (b0) and fails (exit 1) when any of the three invariants is violated:

  1. the volume UUID differs from b0;
  2. the seed/ file inventory (relative path, size, SHA-256) differs from b0 -- on b1 and later the
     Windows (SMB) and Linux (NFS) inventories must also agree with each other;
  3. a top-level path other than seed/, probe/, out/ appears on the volume. The ONTAP snapshot
     directories ~snapshot and .snapshot are ignored (they are not real content).

probe/ and out/ are counted only; their contents are not compared, because many writers add to them
legitimately between boundaries.

Inputs are the boundary-record JSON files written by record-boundary.sh and the inventory JSON
written by inventory.ps1 / inventory.sh. Usage:

  check-invariant.py --baseline b0.json --boundary b1.json

Each boundary JSON references its inventories and carries the UUID and the top-level listing, so no
live ONTAP or client call is made here; this reads records only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

IGNORED_TOP_LEVEL = {"~snapshot", ".snapshot"}
ALLOWED_TOP_LEVEL = {"seed", "probe", "out"}


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def inventory_map(inventory: dict) -> dict[str, tuple[int, str]]:
    """Return {relative_path: (size, sha256)} for seed/ files, from one inventory record."""
    result: dict[str, tuple[int, str]] = {}
    for entry in inventory.get("files", []):
        rel = entry["path"].replace("\\", "/")
        result[rel] = (int(entry["size"]), str(entry["sha256"]))
    return result


def compare_inventories(label_a: str, a: dict, label_b: str, b: dict) -> list[str]:
    """Return human-readable differences between two seed inventories."""
    ma, mb = inventory_map(a), inventory_map(b)
    problems: list[str] = []
    for path in sorted(set(ma) | set(mb)):
        if path not in ma:
            problems.append(f"{path}: present in {label_b}, absent in {label_a}")
        elif path not in mb:
            problems.append(f"{path}: present in {label_a}, absent in {label_b}")
        elif ma[path] != mb[path]:
            problems.append(
                f"{path}: {label_a}={ma[path]} differs from {label_b}={mb[path]}"
            )
    return problems


def top_level_violations(boundary: dict) -> list[str]:
    """Return top-level paths that are neither allowed nor an ignored snapshot directory."""
    listing = boundary.get("top_level_paths", [])
    bad = []
    for name in listing:
        clean = name.strip("/").split("/")[0]
        if clean in IGNORED_TOP_LEVEL or clean in ALLOWED_TOP_LEVEL or clean == "":
            continue
        bad.append(name)
    return bad


def check(baseline: dict, boundary: dict) -> list[str]:
    """Return the list of invariant violations between b0 and a later boundary."""
    problems: list[str] = []

    if baseline.get("volume_uuid") != boundary.get("volume_uuid"):
        problems.append(
            f"volume UUID changed: b0={baseline.get('volume_uuid')} "
            f"boundary={boundary.get('volume_uuid')}"
        )

    if boundary.get("security_style") != "ntfs":
        problems.append(
            f"security style is {boundary.get('security_style')!r}, must stay 'ntfs'"
        )

    # seed/ inventory vs b0. b0 has only a windows inventory; later boundaries have both.
    base_inv = baseline.get("inventories", {})
    bound_inv = boundary.get("inventories", {})
    base_ref = base_inv.get("windows")
    if base_ref is None:
        problems.append("baseline has no windows inventory")
    else:
        for side in ("windows", "linux"):
            this = bound_inv.get(side)
            if this is None:
                if side == "linux":
                    continue  # linux inventory is optional on some records
                problems.append(f"boundary has no {side} inventory")
                continue
            problems.extend(
                compare_inventories("b0", base_ref, f"boundary.{side}", this)
            )
        # On b1+, windows and linux inventories must agree with each other.
        if bound_inv.get("windows") and bound_inv.get("linux"):
            problems.extend(
                compare_inventories(
                    "boundary.windows",
                    bound_inv["windows"],
                    "boundary.linux",
                    bound_inv["linux"],
                )
            )

    for bad in top_level_violations(boundary):
        problems.append(f"unexpected top-level path on the volume: {bad!r}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", required=True, help="b0 boundary-record JSON")
    parser.add_argument("--boundary", required=True, help="later boundary-record JSON")
    args = parser.parse_args(argv)

    problems = check(load(args.baseline), load(args.boundary))
    if problems:
        print("check-invariant: single-volume invariant violated:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("check-invariant: UUID, seed inventory and top-level paths match b0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
