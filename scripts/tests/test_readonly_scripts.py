#!/usr/bin/env python3
"""Unit tests for check-invariant.py, compare-probe.py and check-changeset.py (stdlib only).

The three scripts have hyphenated names, so they are loaded by path. No AWS, ONTAP or client call
happens; every input is an in-test JSON record.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


invariant = load_module("check_invariant", "check-invariant.py")
compare = load_module("compare_probe", "compare-probe.py")
changeset = load_module("check_changeset", "check-changeset.py")


def win_inventory(files):
    return {"files": files}


def boundary(uuid="uuid-1", style="ntfs", windows=None, linux=None, top=None):
    return {
        "volume_uuid": uuid,
        "security_style": style,
        "inventories": {k: v for k, v in (("windows", windows), ("linux", linux)) if v},
        "top_level_paths": top if top is not None else ["seed", "probe", "out"],
    }


class InvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = [{"path": "seed/a.txt", "size": 3, "sha256": "aa"}]
        self.b0 = boundary(windows=win_inventory(self.files))

    def test_identical_passes(self) -> None:
        b1 = boundary(
            windows=win_inventory(self.files), linux=win_inventory(self.files)
        )
        self.assertEqual(invariant.check(self.b0, b1), [])

    def test_uuid_change_flagged(self) -> None:
        b1 = boundary(uuid="uuid-2", windows=win_inventory(self.files))
        self.assertTrue(any("UUID" in p for p in invariant.check(self.b0, b1)))

    def test_hash_mismatch_flagged(self) -> None:
        changed = [{"path": "seed/a.txt", "size": 3, "sha256": "bb"}]
        b1 = boundary(windows=win_inventory(changed))
        self.assertTrue(any("differs" in p for p in invariant.check(self.b0, b1)))

    def test_windows_linux_mismatch_flagged(self) -> None:
        other = [{"path": "seed/a.txt", "size": 4, "sha256": "cc"}]
        b1 = boundary(windows=win_inventory(self.files), linux=win_inventory(other))
        self.assertTrue(invariant.check(self.b0, b1))

    def test_unknown_top_level_flagged(self) -> None:
        b1 = boundary(
            windows=win_inventory(self.files), top=["seed", "probe", "out", "stray"]
        )
        self.assertTrue(any("stray" in p for p in invariant.check(self.b0, b1)))

    def test_snapshot_dirs_ignored(self) -> None:
        b1 = boundary(
            windows=win_inventory(self.files),
            top=["seed", "probe", "out", "~snapshot", ".snapshot"],
        )
        self.assertEqual(invariant.check(self.b0, b1), [])

    def test_security_style_change_flagged(self) -> None:
        b1 = boundary(style="unix", windows=win_inventory(self.files))
        self.assertTrue(
            any("security style" in p for p in invariant.check(self.b0, b1))
        )

    def test_main_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "b0.json").write_text(json.dumps(self.b0), encoding="utf-8")
            good = boundary(windows=win_inventory(self.files))
            (tmp / "b1.json").write_text(json.dumps(good), encoding="utf-8")
            self.assertEqual(
                invariant.main(
                    [
                        "--baseline",
                        str(tmp / "b0.json"),
                        "--boundary",
                        str(tmp / "b1.json"),
                    ]
                ),
                0,
            )
            bad = boundary(uuid="x", windows=win_inventory(self.files))
            (tmp / "b2.json").write_text(json.dumps(bad), encoding="utf-8")
            self.assertEqual(
                invariant.main(
                    [
                        "--baseline",
                        str(tmp / "b0.json"),
                        "--boundary",
                        str(tmp / "b2.json"),
                    ]
                ),
                1,
            )


def probe(behaviors):
    return {"schema": "appmod-probe/1", "behaviors": behaviors}


def behavior(bid, outcome="measured", topology="cross-host", extra=None):
    observed = {"topology": topology}
    if extra:
        observed.update(extra)
    return {"id": bid, "outcome": outcome, "observed": observed}


class CompareProbeTests(unittest.TestCase):
    def test_ok_when_identical(self) -> None:
        base = probe([behavior("file-locking")])
        res = probe([behavior("file-locking")])
        self.assertEqual(compare.compare(base, res), {"file-locking": "ok"})

    def test_differs_when_observation_changes(self) -> None:
        base = probe([behavior("write-visibility", extra={"delay_ms": 10})])
        res = probe([behavior("write-visibility", extra={"delay_ms": 900})])
        self.assertEqual(compare.compare(base, res), {"write-visibility": "differs"})

    def test_not_comparable_on_topology_mismatch(self) -> None:
        base = probe([behavior("file-locking", topology="cross-host")])
        res = probe([behavior("file-locking", topology="same-host")])
        self.assertEqual(compare.compare(base, res), {"file-locking": "not-comparable"})

    def test_skipped_carried_through(self) -> None:
        base = probe([behavior("acl-evaluation")])
        res = probe([behavior("acl-evaluation", outcome="skipped")])
        self.assertEqual(compare.compare(base, res), {"acl-evaluation": "skipped"})

    def test_main_returns_1_on_not_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "s0.json").write_text(
                json.dumps(probe([behavior("file-locking", topology="cross-host")])),
                encoding="utf-8",
            )
            (tmp / "s1.json").write_text(
                json.dumps(probe([behavior("file-locking", topology="same-host")])),
                encoding="utf-8",
            )
            self.assertEqual(
                compare.main(
                    [
                        "--baseline",
                        str(tmp / "s0.json"),
                        "--result",
                        str(tmp / "s1.json"),
                    ]
                ),
                1,
            )


def rc(logical_id, replacement):
    return {
        "ResourceChange": {"LogicalResourceId": logical_id, "Replacement": replacement}
    }


class ChangeSetTests(unittest.TestCase):
    def test_replacement_rejected_by_default(self) -> None:
        cs = {"Changes": [rc("AppDataVolume", "True")]}
        self.assertEqual(changeset.disallowed(cs, set()), [("AppDataVolume", "True")])

    def test_allow_list_permits_named_ids(self) -> None:
        cs = {"Changes": [rc("appmodsvm", "True"), rc("appdata", "True")]}
        self.assertEqual(changeset.disallowed(cs, {"appmodsvm", "appdata"}), [])

    def test_other_id_still_rejected_when_allow_list_present(self) -> None:
        cs = {"Changes": [rc("appdata", "True"), rc("FileSystem", "True")]}
        self.assertEqual(
            changeset.disallowed(cs, {"appmodsvm", "appdata"}), [("FileSystem", "True")]
        )

    def test_no_replacement_passes(self) -> None:
        cs = {"Changes": [rc("ArtifactsBucket", "False")]}
        self.assertEqual(changeset.disallowed(cs, set()), [])

    def test_conditional_treated_as_replacing(self) -> None:
        cs = {"Changes": [rc("AppDataVolume", "Conditional")]}
        self.assertEqual(
            changeset.disallowed(cs, set()), [("AppDataVolume", "Conditional")]
        )

    def test_main_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "bad.json").write_text(
                json.dumps({"Changes": [rc("AppDataVolume", "True")]}), encoding="utf-8"
            )
            self.assertEqual(changeset.main(["--change-set", str(tmp / "bad.json")]), 1)
            self.assertEqual(
                changeset.main(
                    [
                        "--change-set",
                        str(tmp / "bad.json"),
                        "--allow-replacement",
                        "appmodsvm,AppDataVolume",
                    ]
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
