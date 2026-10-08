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

record-boundary.sh stores each inventory as a reference, {"path": ..., "sha256": ...}, not as its
content. The reference is resolved here: the recorded path if it exists, else a file with the same
name next to the boundary JSON (the record is written on the Linux host and copied, with its
inventories, into .private/runs/<run-id>/). The file's SHA-256 must equal the recorded one, and the
inventory must list at least one file. A reference that cannot be resolved, a hash mismatch and an
empty inventory are violations: comparing an unresolved reference as if it were an empty inventory
would pass every boundary, which is how this check once read green while comparing nothing.
An inventory given inline ({"files": [...]}) is used as is.

The Windows inventory is required on every boundary; the Linux (NFS) inventory is required from b1
on. The top-level listing must be present on the later boundary.

No live ONTAP or client call is made here; this reads records only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

IGNORED_TOP_LEVEL = {"~snapshot", ".snapshot"}
ALLOWED_TOP_LEVEL = {"seed", "probe", "out"}


class InventoryError(ValueError):
    """An inventory reference that cannot be resolved to verified content."""


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def resolve_inventory(ref: dict | None, record_dir: Path | None) -> dict | None:
    """Return the inventory content for one reference, or None when the reference is absent.

    Raises InventoryError when a reference is given but cannot be resolved and verified.
    """
    if not ref:
        return None
    if "files" in ref:
        return ref
    path = str(ref.get("path") or "")
    expected = str(ref.get("sha256") or "")
    if not path and not expected:
        return None
    candidates = [Path(path)] if path else []
    if record_dir is not None and path:
        candidates.append(record_dir / Path(path).name)
    for candidate in candidates:
        if candidate.is_file():
            data = candidate.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise InventoryError(
                    f"{candidate}: sha256 {actual} does not match the recorded {expected or '(none)'}"
                )
            try:
                content = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InventoryError(
                    f"{candidate}: not an inventory JSON ({exc})"
                ) from exc
            if not isinstance(content, dict) or not content.get("files"):
                raise InventoryError(f"{candidate}: inventory lists no files")
            return content
    raise InventoryError(
        f"inventory {path!r} not found (looked in: {', '.join(map(str, candidates))})"
    )


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
    listing = boundary.get("top_level_paths") or []
    bad = []
    for name in listing:
        clean = name.strip("/").split("/")[0]
        if clean in IGNORED_TOP_LEVEL or clean in ALLOWED_TOP_LEVEL or clean == "":
            continue
        bad.append(name)
    return bad


def _side(
    record: dict, side: str, record_dir: Path | None, label: str, problems: list[str]
) -> dict | None:
    """Resolve one side's inventory of a record, appending a problem when it cannot be verified."""
    try:
        return resolve_inventory(
            (record.get("inventories") or {}).get(side), record_dir
        )
    except InventoryError as exc:
        problems.append(f"{label}.{side} inventory: {exc}")
        return None


def check(
    baseline: dict,
    boundary: dict,
    baseline_dir: Path | None = None,
    boundary_dir: Path | None = None,
) -> list[str]:
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

    # seed/ inventory vs b0. b0 has only a windows inventory; b1 and later must have both.
    base_win = _side(baseline, "windows", baseline_dir, "b0", problems)
    bound_win = _side(boundary, "windows", boundary_dir, "boundary", problems)
    bound_lnx = _side(boundary, "linux", boundary_dir, "boundary", problems)
    linux_required = boundary.get("boundary") not in (None, "b0")
    if base_win is None:
        problems.append("baseline has no verifiable windows inventory")
    if bound_win is None:
        problems.append("boundary has no verifiable windows inventory")
    if bound_lnx is None and linux_required:
        problems.append(
            f"boundary {boundary.get('boundary')} has no verifiable linux (NFS) inventory"
        )
    if base_win is not None:
        for side, this in (("windows", bound_win), ("linux", bound_lnx)):
            if this is not None:
                problems.extend(
                    compare_inventories("b0", base_win, f"boundary.{side}", this)
                )
    # On b1+, windows and linux inventories must agree with each other.
    if bound_win is not None and bound_lnx is not None:
        problems.extend(
            compare_inventories(
                "boundary.windows", bound_win, "boundary.linux", bound_lnx
            )
        )

    if not isinstance(boundary.get("top_level_paths"), list) or not boundary.get(
        "top_level_paths"
    ):
        problems.append("boundary has no top-level listing (top_level_paths)")
    for bad in top_level_violations(boundary):
        problems.append(f"unexpected top-level path on the volume: {bad!r}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", required=True, help="b0 boundary-record JSON")
    parser.add_argument("--boundary", required=True, help="later boundary-record JSON")
    args = parser.parse_args(argv)

    problems = check(
        load(args.baseline),
        load(args.boundary),
        Path(args.baseline).resolve().parent,
        Path(args.boundary).resolve().parent,
    )
    if problems:
        print("check-invariant: single-volume invariant violated:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("check-invariant: UUID, seed inventory and top-level paths match b0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
