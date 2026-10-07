#!/usr/bin/env python3
"""Compare a stage Probe result against the stage-0 baseline (stdlib only).

Probe emits observations only. This script assigns each behavior a verdict against the stage-0
result:

  ok              both measured and the observation matches b0
  differs         both measured and the observation differs
  not-comparable  the two results have a different observed.topology, so they are not comparable
                  (in this design every stage is cross-host, so not-comparable flags a setup error)

A behavior whose outcome is error or skipped in either side is reported with that outcome and not
given an ok/differs verdict. Usage:

  compare-probe.py --baseline s0.json --result s1.json

Reads Probe JSON only; no live measurement happens here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def behaviors_by_id(result: dict) -> dict[str, dict]:
    return {b["id"]: b for b in result.get("behaviors", [])}


# Per-run identifiers, not observations: a marker file name is a fresh UUID on every run, so
# comparing it made write-visibility read "differs" on every stage (live 2026-10-07).
VOLATILE_KEYS = {"marker"}


def stable(observed: dict) -> dict:
    return {k: v for k, v in observed.items() if k not in VOLATILE_KEYS}


def side_verdicts(base_obs: dict, res_obs: dict) -> dict[str, str]:
    """Per-side verdicts for a merged record (observed.per_side), keyed by side file name.

    A side present only in the result (from stage 1, linux-nfs) has nothing in stage 0 to compare
    with and is reported "no-baseline"; it does not make the behavior "differs".
    """
    base_sides = base_obs.get("per_side") or {}
    res_sides = res_obs.get("per_side") or {}
    verdicts: dict[str, str] = {}
    for side in sorted(set(base_sides) | set(res_sides)):
        if side not in base_sides:
            verdicts[side] = "no-baseline"
        elif side not in res_sides:
            verdicts[side] = "missing-in-result"
        else:
            verdicts[side] = (
                "ok"
                if stable(base_sides[side]) == stable(res_sides[side])
                else "differs"
            )
    return verdicts


def verdict(base_b: dict, res_b: dict) -> str:
    """Return the verdict for one behavior comparison."""
    base_outcome = base_b.get("outcome")
    res_outcome = res_b.get("outcome")
    if base_outcome != "measured" or res_outcome != "measured":
        # Carry the non-measured outcome through; it is not an ok/differs comparison.
        return res_outcome if res_outcome != "measured" else base_outcome

    base_obs = base_b.get("observed", {})
    res_obs = res_b.get("observed", {})
    if base_obs.get("topology") != res_obs.get("topology"):
        return "not-comparable"

    if "per_side" in base_obs or "per_side" in res_obs:
        per_side = side_verdicts(base_obs, res_obs)
        compared = [v for v in per_side.values() if v in ("ok", "differs")]
        if not compared:
            return "not-comparable"
        return "differs" if "differs" in compared else "ok"

    if stable(base_obs) == stable(res_obs):
        return "ok"
    return "differs"


def compare(baseline: dict, result: dict) -> dict[str, str]:
    base = behaviors_by_id(baseline)
    res = behaviors_by_id(result)
    verdicts: dict[str, str] = {}
    for behavior_id in sorted(set(base) | set(res)):
        if behavior_id not in base:
            verdicts[behavior_id] = "no-baseline"
        elif behavior_id not in res:
            verdicts[behavior_id] = "missing-in-result"
        else:
            verdicts[behavior_id] = verdict(base[behavior_id], res[behavior_id])
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", required=True, help="stage-0 Probe JSON")
    parser.add_argument("--result", required=True, help="later-stage Probe JSON")
    args = parser.parse_args(argv)

    baseline, result_record = load(args.baseline), load(args.result)
    verdicts = compare(baseline, result_record)
    base_by_id, res_by_id = behaviors_by_id(baseline), behaviors_by_id(result_record)
    for behavior_id, result in verdicts.items():
        print(f"{behavior_id}: {result}")
        base_b, res_b = base_by_id.get(behavior_id), res_by_id.get(behavior_id)
        if base_b and res_b:
            per_side = side_verdicts(
                base_b.get("observed", {}), res_b.get("observed", {})
            )
            for side, side_result in per_side.items():
                print(f"  {side}: {side_result}")
    # A not-comparable verdict means the topologies did not match, which in this design is a setup
    # error worth surfacing with a non-zero exit.
    if any(v == "not-comparable" for v in verdicts.values()):
        print(
            "compare-probe: a behavior was not-comparable (topology mismatch)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
