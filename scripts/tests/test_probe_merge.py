#!/usr/bin/env python3
"""The merge labels a two-client behavior cross-host only when the pair proves it (stdlib only).

The fixtures under fixtures/probe-merge/ are one coordinated file-locking pair each:

  proven         same sync id, the contender's attempt inside the holder's lock interval
  no-overlap     the contender attempted after the holder released
  sync-mismatch  the contender carries another pair's sync id

A merge that writes cross-host on these behaviors unconditionally (as the merge embedded in
run-probe.sh did before probe_merge.py) labels all three cross-host, so the two negative cases
fail against it. No AWS or ONTAP call happens.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "probe-merge"

spec = importlib.util.spec_from_file_location("probe_merge", SCRIPTS / "probe_merge.py")
probe_merge = importlib.util.module_from_spec(spec)
sys.modules["probe_merge"] = probe_merge
spec.loader.exec_module(probe_merge)


def merged_locking(case: str) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        for f in (FIXTURES / case).iterdir():
            shutil.copy(f, out / f.name)
        pairs = probe_merge.read_manifest(out / "pairs-manifest.tsv")
        record = probe_merge.merge(
            out, "s1-fixture", 1, ["windows", "linux-smb"], pairs
        )
    return {b["id"]: b for b in record["behaviors"]}["file-locking"]


class ProbeMergeTopologyTests(unittest.TestCase):
    def test_proven_pair_is_cross_host(self) -> None:
        b = merged_locking("proven")
        self.assertEqual(b["observed"]["topology"], "cross-host")
        self.assertEqual(b["observed"]["pairs"][0]["topology"], "cross-host")
        self.assertTrue(b["observed"]["pairs"][0]["contender_denied"])

    def test_no_overlap_is_not_cross_host(self) -> None:
        b = merged_locking("no-overlap")
        self.assertEqual(b["observed"]["topology"], "not-comparable")
        self.assertIn("lock interval", "; ".join(b["observed"]["pairs"][0]["reasons"]))

    def test_sync_id_mismatch_is_not_cross_host(self) -> None:
        b = merged_locking("sync-mismatch")
        self.assertEqual(b["observed"]["topology"], "not-comparable")
        self.assertIn("sync_id", "; ".join(b["observed"]["pairs"][0]["reasons"]))

    def test_no_pair_is_not_cross_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            for name in ("windows.json", "linux-smb.json"):
                shutil.copy(FIXTURES / "proven" / name, out / name)
            record = probe_merge.merge(
                out, "s1-fixture", 1, ["windows", "linux-smb"], []
            )
        b = {x["id"]: x for x in record["behaviors"]}["file-locking"]
        self.assertEqual(b["observed"]["topology"], "not-comparable")


if __name__ == "__main__":
    unittest.main()
