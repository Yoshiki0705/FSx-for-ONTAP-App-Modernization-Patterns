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


# Fields of a coordinated pair (probe_merge.py) that describe what happened, as opposed to when.
PAIR_KEYS = ("contender_denied", "after_release_ok", "difference")
# Why a verdict is not-comparable. Only a topology mismatch between two proven records is a setup
# error worth a nonzero exit; a baseline that never coordinated its two-client pairs (stage 0)
# has no cross-host result to compare with, which is a property of the record, not a fault.
NO_PROVEN_BASELINE = "no proven cross-host pair in the baseline"


def pair_summary(pair: dict) -> dict:
    summary = {k: pair.get(k) for k in PAIR_KEYS if k in pair}
    contender = pair.get("contender") or {}
    if contender.get("attempts"):
        summary["attempts"] = {
            k: (v.get("ok") if isinstance(v, dict) else v)
            for k, v in contender["attempts"].items()
        }
    reader = pair.get("reader") or {}
    if "seen" in reader:
        summary["seen"] = reader["seen"]
    return summary


def pair_verdict(base_obs: dict, res_obs: dict) -> str:
    base_pairs = {
        p["pair"]: p
        for p in base_obs.get("pairs") or []
        if p.get("topology") == "cross-host"
    }
    res_pairs = {
        p["pair"]: p
        for p in res_obs.get("pairs") or []
        if p.get("topology") == "cross-host"
    }
    common = sorted(set(base_pairs) & set(res_pairs))
    if not common:
        return "not-comparable"
    same = all(
        pair_summary(base_pairs[k]) == pair_summary(res_pairs[k]) for k in common
    )
    return "ok" if same else "differs"


def not_comparable_reason(base_b: dict, res_b: dict) -> str:
    base_obs, res_obs = base_b.get("observed", {}), res_b.get("observed", {})
    if "pairs" in base_obs or "pairs" in res_obs:
        if not any(
            p.get("topology") == "cross-host" for p in base_obs.get("pairs") or []
        ):
            return NO_PROVEN_BASELINE
        return "no pair proven cross-host on both sides"
    return "topology mismatch"


def verdict(base_b: dict, res_b: dict) -> str:
    """Return the verdict for one behavior comparison."""
    base_outcome = base_b.get("outcome")
    res_outcome = res_b.get("outcome")
    if base_outcome != "measured" or res_outcome != "measured":
        # Carry the non-measured outcome through; it is not an ok/differs comparison.
        return res_outcome if res_outcome != "measured" else base_outcome

    base_obs = base_b.get("observed", {})
    res_obs = res_b.get("observed", {})
    if "pairs" in base_obs or "pairs" in res_obs:
        return pair_verdict(base_obs, res_obs)
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
    setup_errors = []
    for behavior_id, result in verdicts.items():
        base_b, res_b = base_by_id.get(behavior_id), res_by_id.get(behavior_id)
        reason = ""
        if result == "not-comparable" and base_b and res_b:
            reason = not_comparable_reason(base_b, res_b)
            if reason == "topology mismatch":
                setup_errors.append(behavior_id)
        print(f"{behavior_id}: {result}" + (f" ({reason})" if reason else ""))
        if base_b and res_b:
            if "pairs" in res_b.get("observed", {}):
                # Within the result's own stage: every pair under one configuration, so the SMB/SMB
                # pair can be read against the cross-protocol pairs.
                for pair in res_b["observed"]["pairs"]:
                    print(
                        f"  pair {pair['pair']} ({pair['topology']}): "
                        + json.dumps(pair_summary(pair), sort_keys=True)
                    )
                continue
            per_side = side_verdicts(
                base_b.get("observed", {}), res_b.get("observed", {})
            )
            for side, side_result in per_side.items():
                print(f"  {side}: {side_result}")
    # A topology mismatch between two records that both claim a topology is a setup error in this
    # design, surfaced with a non-zero exit. A baseline with no proven pair is reported, not failed.
    if setup_errors:
        print(
            "compare-probe: not-comparable on topology mismatch: "
            + ", ".join(setup_errors),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
