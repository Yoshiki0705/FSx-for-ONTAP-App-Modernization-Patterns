#!/usr/bin/env python3
"""Unit tests for check-invariant.py, compare-probe.py and check-changeset.py (stdlib only).

The three scripts have hyphenated names, so they are loaded by path. No AWS, ONTAP or client call
happens; every input is an in-test JSON record.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
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


class InventoryReferenceTests(unittest.TestCase):
    """record-boundary.sh stores inventories as {path, sha256}; the check must resolve them."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        files = [{"path": "seed/a.txt", "size": 3, "sha256": "aa"}]
        self.inv_bytes = json.dumps({"files": files}).encode("utf-8")
        (self.dir / "windows-inventory.json").write_bytes(self.inv_bytes)
        (self.dir / "linux-inventory.json").write_bytes(self.inv_bytes)
        import hashlib

        self.sha = hashlib.sha256(self.inv_bytes).hexdigest()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def record(
        self,
        name="b1",
        win_sha=None,
        lnx=True,
        win_path="/opt/x/windows-inventory.json",
    ):
        inv = {"windows": {"path": win_path, "sha256": win_sha or self.sha}}
        inv["linux"] = (
            {"path": "/opt/x/linux-inventory.json", "sha256": self.sha}
            if lnx
            else {"path": "", "sha256": ""}
        )
        return {
            "boundary": name,
            "volume_uuid": "u",
            "security_style": "ntfs",
            "inventories": inv,
            "top_level_paths": ["seed", "probe", "out"],
        }

    def test_reference_resolved_next_to_record_passes(self) -> None:
        b0 = self.record("b0", lnx=False)
        self.assertEqual(invariant.check(b0, self.record(), self.dir, self.dir), [])

    def test_hash_mismatch_is_a_violation(self) -> None:
        b0 = self.record("b0", lnx=False)
        problems = invariant.check(
            b0, self.record(win_sha="00" * 32), self.dir, self.dir
        )
        self.assertTrue(any("does not match" in p for p in problems))

    def test_unresolvable_reference_is_a_violation(self) -> None:
        # The defect this guards: an unresolved reference compared as an empty inventory passed.
        b0 = self.record("b0", lnx=False)
        b1 = self.record(win_path="/nonexistent/other-name.json")
        self.assertTrue(
            any("not found" in p for p in invariant.check(b0, b1, self.dir, self.dir))
        )

    def test_empty_inventory_is_a_violation(self) -> None:
        (self.dir / "windows-inventory.json").write_bytes(b'{"files": []}')
        import hashlib

        sha = hashlib.sha256(b'{"files": []}').hexdigest()
        b0 = self.record("b0", win_sha=sha, lnx=False)
        self.assertTrue(
            any(
                "no files" in p
                for p in invariant.check(b0, self.record(), self.dir, self.dir)
            )
        )

    def test_b1_without_linux_inventory_is_a_violation(self) -> None:
        b0 = self.record("b0", lnx=False)
        problems = invariant.check(b0, self.record(lnx=False), self.dir, self.dir)
        self.assertTrue(any("linux (NFS) inventory" in p for p in problems))

    def test_missing_top_level_listing_is_a_violation(self) -> None:
        b0 = self.record("b0", lnx=False)
        b1 = self.record()
        b1["top_level_paths"] = None
        self.assertTrue(
            any(
                "top-level listing" in p
                for p in invariant.check(b0, b1, self.dir, self.dir)
            )
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

    def test_marker_name_is_not_an_observation(self) -> None:
        base = probe([behavior("write-visibility", extra={"marker": "vis-a.txt"})])
        res = probe([behavior("write-visibility", extra={"marker": "vis-b.txt"})])
        self.assertEqual(compare.compare(base, res), {"write-visibility": "ok"})

    def test_merged_new_side_is_no_baseline_not_differs(self) -> None:
        def merged(per_side):
            return probe(
                [
                    {
                        "id": "path-separator",
                        "outcome": "measured",
                        "observed": {"topology": "cross-host", "per_side": per_side},
                    }
                ]
            )

        smb = {"rejected": True, "topology": "cross-host"}
        base = merged({"linux-smb.json": smb})
        res = merged({"linux-smb.json": smb, "linux-nfs.json": {"rejected": False}})
        self.assertEqual(compare.compare(base, res), {"path-separator": "ok"})
        self.assertEqual(
            compare.side_verdicts(
                base["behaviors"][0]["observed"], res["behaviors"][0]["observed"]
            ),
            {"linux-nfs.json": "no-baseline", "linux-smb.json": "ok"},
        )
        changed = merged(
            {"linux-smb.json": {"rejected": False, "topology": "cross-host"}}
        )
        self.assertEqual(compare.compare(base, changed), {"path-separator": "differs"})

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


CS = "case-sensitivity"


def cs_valid(resolved=True, real_resolved=True):
    """A case-sensitivity observation that carries its positive control (Probe from stage 2 on)."""
    return {
        "reference": "seed/Docs/Index.json",
        "resolved": resolved,
        "real": "seed/docs/index.json",
        "real_resolved": real_resolved,
        "topology": "cross-host",
    }


# Shape recorded by the stage-0 Probe: a reference that did not resolve and nothing to show the
# lookup could have succeeded. Recorded as "measured" in the baseline runs.
CS_STAGE0 = {
    "topology": "cross-host",
    "reference": "Docs\\Index.json",
    "resolved": False,
}


def cs_probe(observed, outcome="measured"):
    return probe([{"id": CS, "outcome": outcome, "observed": observed}])


def cs_merged(per_side):
    return {"topology": "cross-host", "per_side": per_side}


def run_main(base, res):
    """Run compare.main on two Probe records; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "base.json").write_text(json.dumps(base), encoding="utf-8")
        (tmp / "res.json").write_text(json.dumps(res), encoding="utf-8")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = compare.main(
                [
                    "--baseline",
                    str(tmp / "base.json"),
                    "--result",
                    str(tmp / "res.json"),
                ]
            )
    return code, out.getvalue(), err.getvalue()


class CaseSensitivityControlTests(unittest.TestCase):
    def test_valid_vs_valid_is_ok_and_exits_0(self) -> None:
        base, res = cs_probe(cs_valid()), cs_probe(cs_valid())
        self.assertEqual(compare.compare(base, res), {CS: "ok"})
        code, out, _ = run_main(base, res)
        self.assertEqual((code, out.strip()), (0, f"{CS}: ok"))

    def test_valid_vs_valid_differs_still_exits_0(self) -> None:
        # A case-sensitive store (resolved false) against a case-insensitive one is a finding, not
        # an invalid record: both have their control.
        base, res = (
            cs_probe(cs_valid(resolved=True)),
            cs_probe(cs_valid(resolved=False)),
        )
        self.assertEqual(compare.compare(base, res), {CS: "differs"})
        self.assertEqual(run_main(base, res)[0], 0)

    def test_baseline_without_control_is_invalid_exit_3(self) -> None:
        no_control = cs_valid()
        del no_control["real_resolved"]
        base, res = cs_probe(no_control), cs_probe(cs_valid())
        self.assertEqual(compare.compare(base, res), {CS: "invalid"})
        code, out, err = run_main(base, res)
        self.assertEqual(code, compare.EXIT_NO_CONTROL)
        self.assertEqual(compare.EXIT_NO_CONTROL, 3)
        self.assertEqual(
            out.strip(), f"{CS}: invalid (no positive control in baseline)"
        )
        self.assertIn("without a positive control", err)

    def test_result_with_real_resolved_false_is_invalid(self) -> None:
        base, res = cs_probe(cs_valid()), cs_probe(cs_valid(real_resolved=False))
        self.assertEqual(compare.compare(base, res), {CS: "invalid"})
        code, out, _ = run_main(base, res)
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), f"{CS}: invalid (no positive control in result)")

    def test_both_sides_without_control(self) -> None:
        base, res = cs_probe(CS_STAGE0), cs_probe(dict(CS_STAGE0))
        code, out, _ = run_main(base, res)
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), f"{CS}: invalid (no positive control in both)")

    def test_real_resolved_must_be_boolean_true(self) -> None:
        base, res = cs_probe(cs_valid(real_resolved="true")), cs_probe(cs_valid())
        self.assertEqual(compare.compare(base, res), {CS: "invalid"})

    def test_stage0_shaped_baseline_against_valid_result(self) -> None:
        # Identical "resolved: false" would have compared ok before; with no control it says nothing.
        base, res = cs_probe(CS_STAGE0), cs_probe(cs_valid(resolved=False))
        self.assertEqual(
            compare.control_gap(base["behaviors"][0], res["behaviors"][0]), "baseline"
        )
        code, out, _ = run_main(base, res)
        self.assertEqual(code, 3)
        self.assertEqual(
            out.strip(), f"{CS}: invalid (no positive control in baseline)"
        )

    def test_peer_script_record_is_invalid(self) -> None:
        # probe_peer.py records only distinct_case_files, never a control.
        peer = {"distinct_case_files": False, "topology": "cross-host"}
        self.assertEqual(
            compare.compare(cs_probe(peer), cs_probe(cs_valid())), {CS: "invalid"}
        )

    def test_error_outcome_without_control_is_still_error_exit_0(self) -> None:
        # The Probe reports a missing control as outcome "error"; that path is unchanged.
        base, res = cs_probe(cs_valid()), cs_probe({"topology": "cross-host"}, "error")
        self.assertEqual(compare.compare(base, res), {CS: "error"})
        self.assertEqual(run_main(base, res)[0], 0)

    def test_other_behaviors_do_not_need_a_control(self) -> None:
        base, res = (
            probe([behavior("acl-evaluation")]),
            probe([behavior("acl-evaluation")]),
        )
        self.assertEqual(compare.compare(base, res), {"acl-evaluation": "ok"})
        self.assertEqual(run_main(base, res)[0], 0)

    def test_topology_mismatch_still_exits_1_when_also_invalid(self) -> None:
        base = probe([behavior("file-locking"), cs_probe(CS_STAGE0)["behaviors"][0]])
        res = probe(
            [
                behavior("file-locking", topology="same-host"),
                cs_probe(cs_valid())["behaviors"][0],
            ]
        )
        self.assertEqual(run_main(base, res)[0], 1)

    def test_merged_all_sides_valid_is_ok_with_new_side_no_baseline(self) -> None:
        base = cs_probe(
            cs_merged({"windows.json": cs_valid(), "linux-smb.json": cs_valid()})
        )
        res = cs_probe(
            cs_merged(
                {
                    "windows.json": cs_valid(),
                    "linux-smb.json": cs_valid(),
                    "linux-nfs.json": cs_valid(resolved=False),
                }
            )
        )
        self.assertEqual(compare.compare(base, res), {CS: "ok"})
        code, out, _ = run_main(base, res)
        self.assertEqual(code, 0)
        self.assertIn("linux-nfs.json: no-baseline", out)

    def test_merged_baseline_side_without_control_is_invalid_for_that_side(
        self,
    ) -> None:
        base = cs_probe(
            cs_merged({"windows.json": cs_valid(), "linux-smb.json": CS_STAGE0})
        )
        res = cs_probe(
            cs_merged({"windows.json": cs_valid(), "linux-smb.json": cs_valid()})
        )
        self.assertEqual(compare.compare(base, res), {CS: "invalid"})
        self.assertEqual(
            compare.side_verdicts(
                base["behaviors"][0]["observed"],
                res["behaviors"][0]["observed"],
                check_control=True,
            ),
            {"linux-smb.json": "invalid", "windows.json": "ok"},
        )
        code, out, _ = run_main(base, res)
        self.assertEqual(code, 3)
        self.assertIn(f"{CS}: invalid (no positive control in baseline)", out)
        self.assertIn(
            "  linux-smb.json: invalid (no positive control in baseline)", out
        )
        self.assertIn("  windows.json: ok", out)

    def test_merged_result_only_side_without_control_is_invalid(self) -> None:
        base = cs_probe(cs_merged({"windows.json": cs_valid()}))
        res = cs_probe(
            cs_merged(
                {"windows.json": cs_valid(), "linux-nfs.json": {"resolved": False}}
            )
        )
        self.assertEqual(
            compare.control_gap(base["behaviors"][0], res["behaviors"][0]), "result"
        )
        code, out, _ = run_main(base, res)
        self.assertEqual(code, 3)
        self.assertIn("  linux-nfs.json: invalid (no positive control in result)", out)

    def test_merged_record_with_no_sides_has_no_control(self) -> None:
        base, res = (
            cs_probe(cs_merged({})),
            cs_probe(cs_merged({"windows.json": cs_valid()})),
        )
        self.assertEqual(compare.compare(base, res), {CS: "invalid"})


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
