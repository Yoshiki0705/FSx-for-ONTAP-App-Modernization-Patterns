#!/usr/bin/env python3
"""Tests for scripts/measure/host/standalone-probe.sh (no network, no AWS, no real mounts).

The script is run with PATH shims: setpriv drops its three options, records them and runs the
rest; mountpoint answers from a list; the Probe is a stub that records its arguments. Covered: the
argument guards (exit 2), the unmounted-path guard (exit 3, before any Probe runs), the uid used
for the NFS side and its absence on the cifs side, the output markers and exit-code lines, and
that the script has no delete or move command.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent / "measure" / "host" / "standalone-probe.sh"
)
RUN_ID = "s2m-20261012T090934Z"

SETPRIV = """#!/bin/bash
# fake setpriv: log the three options, then run the rest
printf 'setpriv %s %s %s\\n' "$1" "$2" "$3" >>"$SHIM_LOG"
shift 3
exec "$@"
"""
MOUNTPOINT = """#!/bin/bash
# fake mountpoint -q PATH: unmounted when PATH is listed in SHIM_UNMOUNTED
for p in $SHIM_UNMOUNTED; do
  if [ "$p" = "$2" ]; then exit 1; fi
done
exit 0
"""
PROBE = """#!/bin/bash
printf 'probe %s\\n' "$*" >>"$SHIM_LOG"
exit "${SHIM_PROBE_EXIT:-0}"
"""


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class StandaloneProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.shims = root / "shims"
        self.shims.mkdir()
        write_exec(self.shims / "setpriv", SETPRIV)
        write_exec(self.shims / "mountpoint", MOUNTPOINT)
        self.nfs = root / "nfs"
        self.smb = root / "smb"
        self.nfs.mkdir()
        self.smb.mkdir()
        (self.nfs / "seed").mkdir()
        self.probe = root / "probe"
        write_exec(self.probe, PROBE)
        self.log = root / "log.txt"
        self.log.write_text("")

    def env(self, **over: str) -> dict[str, str]:
        base = {
            "PATH": f"{self.shims}:{os.environ['PATH']}",
            "SHIM_LOG": str(self.log),
            "SHIM_UNMOUNTED": "",
            "APPMOD_RUN_ID": RUN_ID,
            "APPMOD_PROBE_BIN": str(self.probe),
            "APPMOD_STAGE": "2",
            "APPMOD_NFS_ROOT": str(self.nfs),
            "APPMOD_SMB_ROOT": str(self.smb),
        }
        base.update(over)
        return {k: v for k, v in base.items() if v is not None}

    def run_script(self, **over: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=self.env(**over),
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )

    def test_happy_path_markers_and_uid(self) -> None:
        r = self.run_script(SHIM_PROBE_EXIT="3")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        for marker in (
            "===PRE-BEGIN===", "===PRE-END===", "===NFS-BEGIN===", "nfs-exit=3", "===NFS-END===",
            "===SMB-BEGIN===", "smb-exit=3", "===SMB-END===", "===POST-BEGIN===", "===POST-END===",
        ):  # fmt: skip
            self.assertIn(marker, r.stdout)
        self.assertLess(
            r.stdout.index("===PRE-END==="), r.stdout.index("===NFS-BEGIN===")
        )
        self.assertLess(
            r.stdout.index("===NFS-END==="), r.stdout.index("===SMB-BEGIN===")
        )
        log = self.log.read_text().splitlines()
        probes = [l for l in log if l.startswith("probe ")]
        self.assertEqual(len(probes), 2)
        self.assertIn(
            f"--store nfs --root {self.nfs} --stage 2 --role standalone --run-id {RUN_ID}",
            probes[0],
        )
        self.assertIn(
            f"--store smb --root {self.smb} --stage 2 --role standalone --run-id {RUN_ID}",
            probes[1],
        )
        # Every setpriv call uses uid and gid 10001 with no supplementary groups.
        priv = [l for l in log if l.startswith("setpriv")]
        self.assertTrue(priv)
        self.assertTrue(
            all(l == "setpriv --reuid=10001 --regid=10001 --clear-groups" for l in priv)
        )
        # The probe line right after the NFS setpriv call, and none right before the cifs run.
        nfs_index = log.index(probes[0])
        self.assertTrue(log[nfs_index - 1].startswith("setpriv"))
        self.assertFalse(log[log.index(probes[1]) - 1].startswith("setpriv"))

    def test_uid_override_and_root_refused(self) -> None:
        r = self.run_script(APPMOD_NFS_UID="20001")
        self.assertEqual(r.returncode, 0)
        self.assertIn(
            "setpriv --reuid=20001 --regid=20001 --clear-groups", self.log.read_text()
        )
        self.log.write_text("")
        r = self.run_script(APPMOD_NFS_UID="0")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("probe ", self.log.read_text())

    def test_argument_guards_exit_2_before_anything_runs(self) -> None:
        bad_cases = {
            "run id": {"APPMOD_RUN_ID": "s2-2026"},
            "run id empty": {"APPMOD_RUN_ID": ""},
            "stage letters": {"APPMOD_STAGE": "s2"},
            "stage two digits": {"APPMOD_STAGE": "12"},
            "probe relative": {"APPMOD_PROBE_BIN": "probe"},
            "probe dotdot": {"APPMOD_PROBE_BIN": f"{self.probe}/../probe"},
            "probe missing": {"APPMOD_PROBE_BIN": str(self.probe) + "-missing"},
            "nfs root with space": {"APPMOD_NFS_ROOT": "/mnt/app data"},
            "smb root dotdot": {"APPMOD_SMB_ROOT": "/mnt/../etc"},
            "uid text": {"APPMOD_NFS_UID": "abc"},
        }
        for label, over in bad_cases.items():
            self.log.write_text("")
            r = self.run_script(**over)
            self.assertEqual(r.returncode, 2, msg=f"{label}: {r.stderr}")
            self.assertEqual(r.stdout, "", msg=label)
            self.assertEqual(self.log.read_text(), "", msg=label)

    def test_non_executable_probe_is_refused(self) -> None:
        self.probe.chmod(0o644)
        r = self.run_script()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.log.read_text(), "")

    def test_unmounted_path_exit_3_before_any_probe(self) -> None:
        for which in (self.nfs, self.smb):
            self.log.write_text("")
            r = self.run_script(SHIM_UNMOUNTED=str(which))
            self.assertEqual(r.returncode, 3, msg=r.stderr)
            self.assertEqual(r.stdout, "")
            self.assertEqual(self.log.read_text(), "")

    def test_volume_files_are_left_alone(self) -> None:
        before = sorted(p.relative_to(self.nfs).as_posix() for p in self.nfs.rglob("*"))
        self.assertEqual(self.run_script().returncode, 0)
        after = sorted(p.relative_to(self.nfs).as_posix() for p in self.nfs.rglob("*"))
        self.assertEqual(before, after)

    def test_script_has_no_delete_or_move(self) -> None:
        code = [
            l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#")
        ]
        text = "\n".join(code)
        for word in (
            "rm ",
            "rm\t",
            "mv ",
            "unlink",
            "shred",
            "truncate",
            "-delete",
            "-exec",
        ):
            self.assertNotIn(word, text, msg=word)

    def test_help(self) -> None:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--help"], capture_output=True, check=False, text=True
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("APPMOD_PROBE_BIN", r.stdout)


if __name__ == "__main__":
    unittest.main()
