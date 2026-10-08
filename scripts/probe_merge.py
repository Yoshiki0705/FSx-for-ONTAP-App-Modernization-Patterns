#!/usr/bin/env python3
"""Merge one Probe run's per-host records into merged.json (stdlib only). Called by run-probe.sh.

Two kinds of input sit in the run directory:

  standalone sides   <side>.json, one appmod-probe/1 record per host and store (windows,
                     linux-smb, linux-nfs), each measuring all five behaviors on its own
  coordinated pairs  pair-<n>-<side>-<role>.json, two records per pair, each carrying one
                     two-client behavior (file-locking or write-visibility) run against the other
                     host through a barrier on the artifacts bucket, listed in pairs-manifest.tsv

A two-client behavior is recorded cross-host ONLY when the pair proves it. Both sides must carry
the sync_id the manifest assigned and the role it assigned, run on different hosts, and have a
timeline that overlaps the way the behavior requires:

  file-locking      the contender's attempt lies inside the holder's lock interval
                    (lock_acquired_at <= attempt_started_at, attempt_ended_at <= lock_released_at)
  write-visibility  the reader confirmed the marker absent before the writer started
                    (ready_at <= write_started_at) and either saw it after the save
                    (first_read_ok_at >= save_completed_at - clock tolerance) or kept polling for
                    60 s after the save without seeing it

Anything else is not-comparable. This replaces a merge that wrote cross-host on these two behaviors
unconditionally, which let two hosts that never interacted read as a cross-host measurement.

  probe_merge.py --out-dir DIR --run-id ID --stage N --sides "windows linux-smb" \
      [--pairs DIR/pairs-manifest.tsv]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

TWO_CLIENT = ("file-locking", "write-visibility")
OUTCOMES = {"measured", "error", "skipped"}
VISIBILITY_CAP_MS = 60_000
ROLES = {
    "file-locking": ("holder", "contender"),
    "write-visibility": ("writer", "reader"),
}


def load(path: Path) -> dict | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return record if record.get("schema") == "appmod-probe/1" else None


def ts(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ms_between(later, earlier) -> float | None:
    a, b = ts(later), ts(earlier)
    if a is None or b is None:
        return None
    return (a - b).total_seconds() * 1000.0


def offset_ms(record: dict) -> float | None:
    value = (record.get("host") or {}).get("ntp_offset_ms")
    return abs(float(value)) if isinstance(value, (int, float)) else None


def read_manifest(path: Path | None) -> list[dict]:
    pairs: list[dict] = []
    if path is None or not path.is_file():
        return pairs
    for line in path.read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if len(cols) != 9 or not line.strip():
            continue
        n, behavior, sync_id, s1, r1, f1, s2, r2, f2 = cols
        pairs.append(
            {
                "n": n,
                "behavior": behavior,
                "sync_id": sync_id,
                "sides": [
                    {"side": s1, "role": r1, "file": f1},
                    {"side": s2, "role": r2, "file": f2},
                ],
            }
        )
    return pairs


def behavior_of(record: dict | None, behavior_id: str) -> dict | None:
    for b in (record or {}).get("behaviors", []):
        if b.get("id") == behavior_id:
            return b
    return None


def evaluate_pair(out_dir: Path, pair: dict) -> dict:
    """Return the merged view of one coordinated pair, with a proven or refused topology."""
    behavior_id = pair["behavior"]
    first_role, second_role = ROLES.get(behavior_id, ("", ""))
    name = "/".join(s["side"] for s in pair["sides"])
    result: dict = {
        "pair": name,
        "sync_id": pair["sync_id"],
        "roles": {s["role"]: s["side"] for s in pair["sides"]},
        "topology": "not-comparable",
        "outcome": "measured",
        "reasons": [],
    }
    by_role: dict[str, tuple[dict, dict]] = {}
    for side in pair["sides"]:
        record = load(out_dir / side["file"])
        b = behavior_of(record, behavior_id)
        if record is None or b is None:
            result["reasons"].append(f"{side['file']}: no {behavior_id} record")
            result["outcome"] = "error"
            continue
        outcome = b.get("outcome") if b.get("outcome") in OUTCOMES else "error"
        if outcome != "measured":
            result["outcome"] = outcome
            result["reasons"].append(
                f"{side['side']}: outcome {outcome} ({b.get('error_type')})"
            )
        sync = (b.get("observed") or {}).get("sync") or {}
        if sync.get("sync_id") != pair["sync_id"]:
            result["reasons"].append(
                f"{side['side']}: sync_id {sync.get('sync_id')!r} != {pair['sync_id']!r}"
            )
        if sync.get("role") != side["role"]:
            result["reasons"].append(
                f"{side['side']}: role {sync.get('role')!r} != {side['role']!r}"
            )
        by_role[side["role"]] = (record, b)
    if set(by_role) != {first_role, second_role}:
        result["reasons"].append("both roles are required")
        return result

    (rec1, b1), (rec2, b2) = by_role[first_role], by_role[second_role]
    host1, host2 = rec1.get("host") or {}, rec2.get("host") or {}
    if not host1.get("name") or host1.get("name") == host2.get("name"):
        result["reasons"].append(
            "the two sides did not run on two distinct named hosts"
        )
    o1, o2 = offset_ms(rec1), offset_ms(rec2)
    tolerance = (o1 or 0.0) + (o2 or 0.0)
    result["ntp_offset_ms"] = {first_role: o1, second_role: o2}
    result["clock_tolerance_ms"] = round(tolerance, 3)
    s1 = (b1.get("observed") or {}).get("sync") or {}
    s2 = (b2.get("observed") or {}).get("sync") or {}
    result[first_role] = s1
    result[second_role] = s2

    if behavior_id == "file-locking":
        after_acquire = ms_between(
            s2.get("attempt_started_at"), s1.get("lock_acquired_at")
        )
        before_release = ms_between(
            s1.get("lock_released_at"), s2.get("attempt_ended_at")
        )
        if (
            after_acquire is None
            or before_release is None
            or after_acquire < 0
            or before_release < 0
        ):
            result["reasons"].append(
                "the contender's attempt is not inside the holder's lock interval"
            )
        attempts = s2.get("attempts") or {}
        result["contender_denied"] = any(
            isinstance(v, dict) and v.get("ok") is False for v in attempts.values()
        )
        after = s2.get("after_release") or {}
        result["after_release_ok"] = (
            after.get("ok") if isinstance(after, dict) else None
        )
    elif behavior_id == "write-visibility":
        gap = ms_between(s1.get("write_started_at"), s2.get("ready_at"))
        if gap is None or gap < 0:
            result["reasons"].append(
                "the reader was not ready before the writer started"
            )
        delay = ms_between(s2.get("first_read_ok_at"), s1.get("save_completed_at"))
        if s2.get("seen") is True:
            if delay is None or delay < -tolerance:
                result["reasons"].append(
                    "the reader's observation precedes the writer's save"
                )
            result["visible_after_ms"] = None if delay is None else round(delay, 1)
            listed = ms_between(s2.get("first_listed_at"), s1.get("save_completed_at"))
            result["listed_after_ms"] = None if listed is None else round(listed, 1)
            result["difference"] = (
                "below clock offset (no difference)"
                if delay is not None and abs(delay) <= tolerance
                else "measured"
            )
        else:
            covered = ms_between(s2.get("last_poll_at"), s1.get("save_completed_at"))
            if covered is None or covered < VISIBILITY_CAP_MS:
                result["reasons"].append(
                    "the reader stopped polling before 60 s after the save"
                )
            result["visible_after_ms"] = None
            result["difference"] = "not visible within 60 s"
    else:
        result["reasons"].append(f"unknown two-client behavior {behavior_id!r}")

    if not result["reasons"]:
        result["topology"] = "cross-host"
    return result


def merge(
    out_dir: Path, run_id: str, stage: int, sides: list[str], pairs: list[dict]
) -> dict:
    loaded: dict[str, dict] = {}
    missing = []
    for side in sides:
        record = load(out_dir / f"{side}.json")
        if record is None:
            missing.append(side)
        else:
            loaded[f"{side}.json"] = record
    if missing:
        raise SystemExit(
            "probe_merge: no readable appmod-probe/1 record from: " + ", ".join(missing)
        )

    behavior_ids: list[str] = []
    for record in loaded.values():
        for b in record.get("behaviors", []):
            if b.get("id") not in behavior_ids:
                behavior_ids.append(b["id"])
    for pair in pairs:
        if pair["behavior"] not in behavior_ids:
            behavior_ids.append(pair["behavior"])

    merged = []
    for bid in behavior_ids:
        per_side = {}
        outcome = "measured"
        for name, record in loaded.items():
            b = behavior_of(record, bid)
            if b is None:
                continue
            side_outcome = b.get("outcome") if b.get("outcome") in OUTCOMES else "error"
            if side_outcome == "error":
                outcome = "error"
            elif side_outcome == "skipped" and outcome != "error":
                outcome = "skipped"
            per_side[name] = b.get("observed", {})
        observed: dict = {"per_side": per_side}
        if bid in TWO_CLIENT:
            evaluated = [
                evaluate_pair(out_dir, p) for p in pairs if p["behavior"] == bid
            ]
            observed["pairs"] = evaluated
            for p in evaluated:
                if p["outcome"] == "error":
                    outcome = "error"
            proven = bool(evaluated) and all(
                p["topology"] == "cross-host" for p in evaluated
            )
            observed["topology"] = "cross-host" if proven else "not-comparable"
            if not evaluated:
                observed["topology_reason"] = "no coordinated pair was run"
        else:
            topologies = {
                v.get("topology") for v in per_side.values() if isinstance(v, dict)
            }
            observed["topology"] = (
                topologies.pop() if len(topologies) == 1 else "not-comparable"
            )
        merged.append({"id": bid, "outcome": outcome, "observed": observed})

    return {
        "schema": "appmod-probe/1",
        "run_id": run_id,
        "stage": stage,
        "merged_from": sorted(loaded)
        + sorted(f for p in pairs for f in (s["file"] for s in p["sides"])),
        "behaviors": merged,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", required=True, type=int)
    parser.add_argument(
        "--sides", required=True, help="space-separated standalone side names"
    )
    parser.add_argument("--pairs", help="pairs-manifest.tsv written by run-probe.sh")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    pairs = read_manifest(Path(args.pairs) if args.pairs else None)
    record = merge(out_dir, args.run_id, args.stage, args.sides.split(), pairs)
    path = out_dir / "merged.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(
        f"run-probe: merged {len(record['behaviors'])} behavior(s) from "
        f"{len(args.sides.split())} side(s) and {len(pairs)} pair(s) into {path}"
    )
    for b in record["behaviors"]:
        if b["id"] in TWO_CLIENT:
            print(f"run-probe: {b['id']} topology={b['observed']['topology']}")
            for p in b["observed"]["pairs"]:
                why = "; ".join(p["reasons"]) if p["reasons"] else "proven"
                print(f"run-probe:   {p['pair']} {p['topology']} ({why})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
