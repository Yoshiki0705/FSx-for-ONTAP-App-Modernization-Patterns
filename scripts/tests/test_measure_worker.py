#!/usr/bin/env python3
"""Unit tests for the Worker family under scripts/measure (no network, no AWS, no real mounts).

host/run-worker.sh and host/read-out.sh run with PATH shims: setpriv drops its three options,
records them and runs the rest; mountpoint answers from a list; the Worker is a stub that creates
out/<stage>/. Covered: the stage guards (exit 3, nothing run), the out/<stage> guard (exit 4, the
tree byte-identical afterwards), the mount guards (exit 5), the uid used for every volume access,
that the seed and reference stages are not modified, that a copy of the script without a guard
does run the Worker (so the guard tests would catch a removal), the read-out dump format, and the
pure parts of collect_out.py and compare_worker_out.py.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

MEASURE = Path(__file__).resolve().parent.parent / "measure"
sys.path.insert(0, str(MEASURE))

import collect_out
import compare_worker_out

RUN_WORKER = MEASURE / "host" / "run-worker.sh"
READ_OUT = MEASURE / "host" / "read-out.sh"

SETPRIV = """#!/bin/bash
# fake setpriv: log the three options, then run the rest
printf 'setpriv %s %s %s\\n' "$1" "$2" "$3" >>"$SHIM_LOG"
shift 3
exec "$@"
"""
MOUNTPOINT = """#!/bin/bash
for p in $SHIM_UNMOUNTED; do
  if [ "$p" = "$2" ]; then exit 1; fi
done
exit 0
"""
WORKER = """#!/bin/bash
printf 'worker store=%s stage=%s root=%s\\n' "$DOCINTAKE_STORE" "$DOCINTAKE_STAGE" "$DOCINTAKE_ROOT" >>"$SHIM_LOG"
d="$DOCINTAKE_ROOT/out/$DOCINTAKE_STAGE"
mkdir -p "$d/reports"
printf '{"documents":[{"name":"doc-001.txt"}]}' >"$d/index.json"
printf 'report one\\n' >"$d/reports/doc-001.txt.txt"
exit "${SHIM_WORKER_EXIT:-0}"
"""


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def snapshot(root: Path) -> dict[str, str]:
    """Relative path -> content hash (or 'dir' / 'link') for everything under root."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "link"
        elif p.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class VolumeCase(unittest.TestCase):
    """A temporary NFS root with seed/ and out/, plus shims on PATH."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.shims = root / "shims"
        self.shims.mkdir()
        write_exec(self.shims / "setpriv", SETPRIV)
        write_exec(self.shims / "mountpoint", MOUNTPOINT)
        self.nfs = root / "nfs"
        (self.nfs / "seed" / "inbox").mkdir(parents=True)
        (self.nfs / "seed" / "inbox" / "doc-001.txt").write_text("seed one\n")
        (self.nfs / "seed" / "docs").mkdir()
        (self.nfs / "seed" / "docs" / "index.json").write_text("{}")
        (self.nfs / "out").mkdir()
        self.worker = root / "worker"
        write_exec(self.worker, WORKER)
        self.log = root / "log.txt"
        self.log.write_text("")

    def add_stage(self, name: str, index: str = '{"documents":[]}') -> None:
        d = self.nfs / "out" / name
        (d / "reports").mkdir(parents=True)
        (d / "index.json").write_text(index)
        (d / "reports" / "doc-001.txt.txt").write_text("report one\n")

    def env(self, **over: str | None) -> dict[str, str]:
        base: dict[str, str | None] = {
            "PATH": f"{self.shims}:{os.environ['PATH']}",
            "SHIM_LOG": str(self.log),
            "SHIM_UNMOUNTED": "",
            "APPMOD_WORKER_BIN": str(self.worker),
            "APPMOD_WORKER_STAGE": "s2m",
            "APPMOD_NFS_ROOT": str(self.nfs),
        }
        base.update(over)
        return {k: v for k, v in base.items() if v is not None}

    def run_script(
        self, script: Path = RUN_WORKER, **over: str | None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script)],
            env=self.env(**over),
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )

    def log_lines(self) -> list[str]:
        return self.log.read_text().splitlines()

    def worker_runs(self) -> list[str]:
        return [line for line in self.log_lines() if line.startswith("worker ")]


class RunWorkerTests(VolumeCase):
    def test_happy_path_writes_only_the_stage_directory(self) -> None:
        self.add_stage("s2")
        before = snapshot(self.nfs)
        r = self.run_script(APPMOD_REF_STAGES="s2")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        for marker in (
            "===PRE===",
            "===WORKER-BEGIN===",
            "worker-exit=0",
            "===WORKER-END===",
            "===POST===",
            "seed-unchanged=yes",
            "--- out/s2m/index.json (raw)",
            "--- out/s2 reports sha256 (reference)",
            "===END===",
        ):
            self.assertIn(marker, r.stdout)
        after = snapshot(self.nfs)
        added = sorted(set(after) - set(before))
        self.assertEqual(
            added,
            [
                "out/s2m",
                "out/s2m/index.json",
                "out/s2m/reports",
                "out/s2m/reports/doc-001.txt.txt",
            ],
        )
        changed = [k for k in before if before[k] != after[k]]
        self.assertEqual(changed, [], msg="existing files must not change")
        self.assertEqual(
            self.worker_runs(), [f"worker store=nfs stage=s2m root={self.nfs}"]
        )

    def test_every_volume_access_uses_the_nfs_uid(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        priv = [line for line in self.log_lines() if line.startswith("setpriv")]
        self.assertTrue(priv)
        self.assertTrue(
            all(
                line == "setpriv --reuid=10001 --regid=10001 --clear-groups"
                for line in priv
            )
        )
        idx = self.log_lines().index(self.worker_runs()[0])
        self.assertTrue(self.log_lines()[idx - 1].startswith("setpriv"))

    def test_uid_override_and_root_refused(self) -> None:
        self.assertEqual(self.run_script(APPMOD_NFS_UID="20001").returncode, 0)
        self.assertIn(
            "setpriv --reuid=20001 --regid=20001 --clear-groups", self.log.read_text()
        )
        self.log.write_text("")
        self.assertEqual(
            self.run_script(APPMOD_NFS_UID="0", APPMOD_WORKER_STAGE="s2n").returncode, 2
        )
        self.assertEqual(self.log.read_text(), "")

    def test_worker_exit_code_is_printed_not_propagated(self) -> None:
        r = self.run_script(SHIM_WORKER_EXIT="7")
        self.assertEqual(r.returncode, 0)
        self.assertIn("worker-exit=7", r.stdout)

    def test_stage_unset_or_malformed_exits_3_and_runs_nothing(self) -> None:
        cases: dict[str, str | None] = {
            "unset": None,
            "empty": "",
            "seed": "seed",
            "out": "out",
            "probe": "probe",
            "traversal": "s2m/../x",
            "upper": "S2m",
            "no digit": "sm",
            "too long": "s2mmm",
            "space": "s2 m",
        }
        before = snapshot(self.nfs)
        for label, value in cases.items():
            self.log.write_text("")
            r = self.run_script(APPMOD_WORKER_STAGE=value)
            self.assertEqual(r.returncode, 3, msg=f"{label}: {r.stderr}")
            self.assertEqual(r.stdout, "", msg=label)
            self.assertEqual(self.log.read_text(), "", msg=label)
        self.assertEqual(snapshot(self.nfs), before)

    def test_existing_stage_directory_exits_4_and_changes_nothing(self) -> None:
        self.add_stage("s2m", index="PRECIOUS")
        before = snapshot(self.nfs)
        r = self.run_script()
        self.assertEqual(r.returncode, 4, msg=r.stderr)
        self.assertIn("out/s2m already exists", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.worker_runs(), [])
        self.assertEqual(snapshot(self.nfs), before)

    def test_dangling_symlink_at_the_stage_path_also_exits_4(self) -> None:
        os.symlink(self.nfs / "nowhere", self.nfs / "out" / "s2m")
        r = self.run_script()
        self.assertEqual(r.returncode, 4, msg=r.stderr)
        self.assertEqual(self.worker_runs(), [])

    def test_mount_guards_exit_5(self) -> None:
        r = self.run_script(SHIM_UNMOUNTED=str(self.nfs))
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.log.read_text(), "")
        (self.nfs / "out").rmdir()
        r = self.run_script()
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.worker_runs(), [])

    def test_argument_guards_exit_2_before_anything_runs(self) -> None:
        bad: dict[str, dict[str, str | None]] = {
            "worker unset": {"APPMOD_WORKER_BIN": None},
            "worker relative": {"APPMOD_WORKER_BIN": "worker"},
            "worker dotdot": {"APPMOD_WORKER_BIN": f"{self.worker}/../worker"},
            "worker missing": {"APPMOD_WORKER_BIN": str(self.worker) + "-missing"},
            "store smb": {"APPMOD_WORKER_STORE": "smb"},
            "root with space": {"APPMOD_NFS_ROOT": "/mnt/app data"},
            "uid text": {"APPMOD_NFS_UID": "abc"},
            "ref malformed": {"APPMOD_REF_STAGES": "s2,seed"},
            "ref equals stage": {"APPMOD_REF_STAGES": "s2m"},
        }
        for label, over in bad.items():
            self.log.write_text("")
            r = self.run_script(**over)
            self.assertEqual(r.returncode, 2, msg=f"{label}: {r.stderr}")
            self.assertEqual(self.log.read_text(), "", msg=label)

    def test_non_executable_worker_is_refused(self) -> None:
        self.worker.chmod(0o644)
        self.assertEqual(self.run_script().returncode, 2)

    def test_reference_stages_are_not_modified(self) -> None:
        self.add_stage("s2")
        self.add_stage("s3")
        before = {
            k: v
            for k, v in snapshot(self.nfs).items()
            if k.startswith(("out/s2/", "out/s3/", "seed"))
        }
        self.assertEqual(self.run_script(APPMOD_REF_STAGES="s2,s3").returncode, 0)
        after = snapshot(self.nfs)
        for k, v in before.items():
            self.assertEqual(after[k], v, msg=k)

    def test_help(self) -> None:
        r = subprocess.run(
            ["bash", str(RUN_WORKER), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("APPMOD_WORKER_STAGE", r.stdout)
        self.assertNotIn("set -uo pipefail", r.stdout)

    def test_script_deletes_and_moves_nothing(self) -> None:
        code = [
            line
            for line in RUN_WORKER.read_text().splitlines()
            if not line.lstrip().startswith("#")
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
            "rmdir",
        ):
            self.assertNotIn(word, text, msg=word)
        for m in re.finditer(r"-exec\s+(\S+)", text):
            self.assertEqual(m.group(1), "sha256sum")


class GuardMutationTests(VolumeCase):
    """A copy of the script without a guard runs the Worker: the tests above would notice."""

    def mutated(self, *patterns: str) -> Path:
        text = RUN_WORKER.read_text()
        for pattern in patterns:
            text, count = re.subn(pattern, "", text, flags=re.DOTALL)
            self.assertEqual(count, 1, msg=f"pattern did not match once: {pattern}")
        path = Path(self.tmp.name) / "run-worker-mutated.sh"
        write_exec(path, text)
        return path

    OUT_STAGE_GUARD = r'if as_nfs_uid test -e "\$NFS_ROOT/out/\$STAGE".*?\nfi\n'
    STAGE_GUARD = (
        r'\[ -n "\$STAGE" \] \|\| die 3 [^\n]*\n'
        r"\[\[ \$STAGE =~ \$STAGE_RE \]\] \|\| die 3 [^\n]*\n"
    )

    def test_without_the_out_stage_guard_the_worker_runs(self) -> None:
        script = self.mutated(self.OUT_STAGE_GUARD)
        self.add_stage("s2m", index="PRECIOUS")
        r = self.run_script(script)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.worker_runs()), 1)

    def test_an_empty_stage_is_stopped_by_the_second_guard_alone(self) -> None:
        script = self.mutated(self.STAGE_GUARD)
        r = self.run_script(script, APPMOD_WORKER_STAGE=None)
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.worker_runs(), [])

    def test_without_both_guards_an_unset_stage_runs_the_worker(self) -> None:
        script = self.mutated(self.STAGE_GUARD, self.OUT_STAGE_GUARD)
        r = self.run_script(script, APPMOD_WORKER_STAGE=None)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.worker_runs()), 1)


class ReadOutTests(VolumeCase):
    def test_dump_round_trips_through_collect_out(self) -> None:
        self.add_stage("s0", index='{\n  "documents": []\n}\n')
        self.add_stage("s2m", index='{"documents":[]}')
        before = snapshot(self.nfs)
        r = self.run_script(READ_OUT, APPMOD_STAGES="s0,s2m", APPMOD_WORKER_STAGE=None)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(snapshot(self.nfs), before)
        files = collect_out.parse_dump(r.stdout)
        self.assertEqual(
            sorted(files),
            [
                "s0/index.json",
                "s0/reports/doc-001.txt.txt",
                "s2m/index.json",
                "s2m/reports/doc-001.txt.txt",
            ],
        )
        self.assertEqual(files["s0/index.json"], b'{\n  "documents": []\n}\n')
        self.assertEqual(files["s2m/reports/doc-001.txt.txt"], b"report one\n")
        observed, aggregate = collect_out.parse_seed(r.stdout)
        self.assertEqual(
            sorted(observed), ["seed/docs/index.json", "seed/inbox/doc-001.txt"]
        )
        self.assertEqual(
            observed["seed/inbox/doc-001.txt"],
            hashlib.sha256(b"seed one\n").hexdigest(),
        )
        self.assertRegex(aggregate, r"^[0-9a-f]{16}$")
        self.assertTrue(
            all(
                line == "setpriv --reuid=10001 --regid=10001 --clear-groups"
                for line in self.log_lines()
                if line.startswith("setpriv")
            )
        )

    def test_guards(self) -> None:
        bad: dict[str, dict[str, str | None]] = {
            "stages unset": {"APPMOD_STAGES": None},
            "stage malformed": {"APPMOD_STAGES": "s0,seed"},
            "uid 0": {"APPMOD_STAGES": "s0", "APPMOD_NFS_UID": "0"},
            "root relative": {"APPMOD_STAGES": "s0", "APPMOD_NFS_ROOT": "mnt"},
        }
        for label, over in bad.items():
            self.log.write_text("")
            r = self.run_script(READ_OUT, **over)
            self.assertEqual(r.returncode, 2, msg=label)
            self.assertEqual(self.log.read_text(), "", msg=label)
        r = self.run_script(READ_OUT, APPMOD_STAGES="s0", SHIM_UNMOUNTED=str(self.nfs))
        self.assertEqual(r.returncode, 3)

    def test_script_only_reads(self) -> None:
        code = [
            line
            for line in READ_OUT.read_text().splitlines()
            if not line.lstrip().startswith("#")
        ]
        text = "\n".join(code)
        for word in (
            "rm ",
            "mv ",
            "unlink",
            "shred",
            "truncate",
            "-delete",
            "mkdir",
            "tee ",
        ):
            self.assertNotIn(word, text, msg=word)


def b64_entry(key: str, data: bytes) -> str:
    return f"== B64 {key}\n{base64.b64encode(data).decode()}\n"


class CollectOutTests(unittest.TestCase):
    def test_parse_dump_including_empty_file_and_crlf(self) -> None:
        text = (
            "== ls out/s0\nignored\n"
            + b64_entry("s0/index.json", b"{}")
            + b64_entry("s0/reports/empty.txt", b"")
        )
        self.assertEqual(
            collect_out.parse_dump(text.replace("\n", "\r\n")),
            {"s0/index.json": b"{}", "s0/reports/empty.txt": b""},
        )

    def test_unsafe_or_malformed_keys_are_rejected(self) -> None:
        for key in ("s0/../x", "s0//x", "seed/x", "s0/", "/etc/passwd"):
            with self.assertRaises(ValueError, msg=key):
                collect_out.parse_dump(b64_entry(key, b"x"))

    def test_bad_base64_and_duplicates(self) -> None:
        with self.assertRaises(ValueError):
            collect_out.parse_dump("== B64 s0/index.json\n!!!notbase64!!!\n")
        with self.assertRaises(ValueError):
            collect_out.parse_dump(
                b64_entry("s0/index.json", b"a") + b64_entry("s0/index.json", b"b")
            )

    def test_ssm_result_json_is_unwrapped(self) -> None:
        wrapped = json.dumps(
            {
                "StandardOutputContent": b64_entry("s0/index.json", b"{}"),
                "Status": "Success",
            }
        )
        self.assertEqual(
            collect_out.parse_dump(collect_out.dump_text(wrapped)),
            {"s0/index.json": b"{}"},
        )
        self.assertEqual(collect_out.dump_text("plain text"), "plain text")

    def test_seed_sections(self) -> None:
        text = (
            "== seed sha256\n"
            + "a" * 64
            + "  seed/a.txt\n"
            + "b" * 64
            + "  seed/b.txt\n== seed aggregate\nadd86f1b0a33e6fa\n== seed dir order\n"
        )
        observed, aggregate = collect_out.parse_seed(text)
        self.assertEqual(observed, {"seed/a.txt": "a" * 64, "seed/b.txt": "b" * 64})
        self.assertEqual(aggregate, "add86f1b0a33e6fa")
        with self.assertRaises(ValueError):
            collect_out.parse_seed("nothing here")

    def test_cli_writes_files_and_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            dump = Path(t) / "dump.txt"
            dump.write_text(
                b64_entry("s0/index.json", b"{}") + b64_entry("s0/reports/r.txt", b"R")
            )
            out_dir = Path(t) / "run"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    collect_out.main(["--dump", str(dump), "--out-dir", str(out_dir)]),
                    0,
                )
            self.assertEqual((out_dir / "s0" / "reports" / "r.txt").read_bytes(), b"R")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    collect_out.main(["--dump", str(dump), "--out-dir", str(out_dir)]),
                    2,
                )
            empty = Path(t) / "empty.txt"
            empty.write_text("nothing")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    collect_out.main(
                        ["--dump", str(empty), "--out-dir", str(Path(t) / "x")]
                    ),
                    3,
                )
                self.assertEqual(
                    collect_out.main(
                        [
                            "--dump",
                            str(Path(t) / "missing"),
                            "--out-dir",
                            str(Path(t) / "y"),
                        ]
                    ),
                    2,
                )


PRETTY = '{\n  "documents": [\n    {"name": "doc-001.txt", "ok": true},\n    {"name": "doc-002.txt", "ok": true}\n  ]\n}\n'
COMPACT = (
    '{"documents":[{"name":"doc-001.txt","ok":true},{"name":"doc-002.txt","ok":true}]}'
)


def stage_files(
    index: str, reports: dict[str, bytes] | None = None
) -> dict[str, bytes]:
    files = {"index.json": index.encode()}
    for name, data in (
        reports
        if reports is not None
        else {"doc-001.txt.txt": b"one", "doc-002.txt.txt": b"two"}
    ).items():
        files[f"reports/{name}"] = data
    return files


class CompareTests(unittest.TestCase):
    def test_equal_json_with_different_bytes_holds(self) -> None:
        a, b = stage_files(PRETTY), stage_files(COMPACT)
        lines, ok = compare_worker_out.compare(a, b, "out-s0", "out-s2m")
        self.assertTrue(ok)
        text = "\n".join(lines)
        self.assertIn("json equal (ordered keys/arrays): True | py == : True", text)
        self.assertIn("whitespace-stripped bytes equal: True", text)
        self.assertIn(
            f"len {len(PRETTY)} {len(COMPACT)} diff bytes {len(PRETTY) - len(COMPACT)}",
            text,
        )
        self.assertIn(
            "names order: ['doc-001.txt', 'doc-002.txt'] ['doc-001.txt', 'doc-002.txt']",
            text,
        )
        self.assertRegex(text, r"reports/doc-001\.txt\.txt True [0-9a-f]{12}")

    def test_reordered_array_is_not_equal(self) -> None:
        swapped = '{"documents":[{"name":"doc-002.txt","ok":true},{"name":"doc-001.txt","ok":true}]}'
        lines, ok = compare_worker_out.compare(
            stage_files(COMPACT), stage_files(swapped)
        )
        self.assertFalse(ok)
        self.assertIn(
            "json equal (ordered keys/arrays): False | py == : False", "\n".join(lines)
        )

    def test_reordered_keys_are_not_ordered_equal_but_py_equal(self) -> None:
        other = '{"documents":[{"ok":true,"name":"doc-001.txt"},{"name":"doc-002.txt","ok":true}]}'
        lines, ok = compare_worker_out.compare(stage_files(COMPACT), stage_files(other))
        self.assertFalse(ok)
        self.assertIn(
            "json equal (ordered keys/arrays): False | py == : True", "\n".join(lines)
        )

    def test_missing_or_different_report_fails(self) -> None:
        a = stage_files(COMPACT)
        missing = stage_files(COMPACT, {"doc-001.txt.txt": b"one"})
        lines, ok = compare_worker_out.compare(a, missing)
        self.assertFalse(ok)
        self.assertIn("reports/doc-002.txt.txt False", "\n".join(lines))
        different = stage_files(
            COMPACT, {"doc-001.txt.txt": b"one", "doc-002.txt.txt": b"TWO"}
        )
        self.assertFalse(compare_worker_out.compare(a, different)[1])

    def test_invalid_json_fails_without_raising(self) -> None:
        lines, ok = compare_worker_out.compare(stage_files("{"), stage_files(COMPACT))
        self.assertFalse(ok)
        self.assertIn("not valid JSON", lines[-1])

    def test_seed_comparison(self) -> None:
        expected_text = (
            "SEED seed\\docs\\index.json 119 "
            + "a" * 64
            + "\nSEED seed\\inbox\\doc-001.txt 59 "
            + "b" * 64
            + "\nSEED-PLACED\n"
        )
        expected = compare_worker_out.parse_seed_expected(expected_text)
        self.assertEqual(
            expected,
            {"seed/docs/index.json": "a" * 64, "seed/inbox/doc-001.txt": "b" * 64},
        )
        a, b = stage_files(COMPACT), stage_files(COMPACT)
        good = (expected, dict(expected), "add86f1b0a33e6fa")
        lines, ok = compare_worker_out.compare(a, b, seed=good)
        self.assertTrue(ok)
        self.assertIn("seed expected==got: True 2", lines)
        self.assertIn("aggregate: add86f1b0a33e6fa", lines)
        bad = (expected, {**expected, "seed/inbox/doc-001.txt": "c" * 64}, "0" * 16)
        self.assertFalse(compare_worker_out.compare(a, b, seed=bad)[1])

    def test_cli_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for name, index in (("a", PRETTY), ("b", COMPACT)):
                (root / name / "reports").mkdir(parents=True)
                (root / name / "index.json").write_text(index)
                (root / name / "reports" / "doc-001.txt.txt").write_text("one")
                (root / name / "reports" / "doc-002.txt.txt").write_text("two")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    compare_worker_out.main(
                        ["--a", str(root / "a"), "--b", str(root / "b")]
                    ),
                    0,
                )
            self.assertIn("json equal (ordered keys/arrays): True", out.getvalue())
            (root / "b" / "reports" / "doc-002.txt.txt").write_text("TWO")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    compare_worker_out.main(
                        ["--a", str(root / "a"), "--b", str(root / "b")]
                    ),
                    1,
                )
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    compare_worker_out.main(
                        ["--a", str(root / "a"), "--b", str(root / "nope")]
                    ),
                    2,
                )
                self.assertEqual(
                    compare_worker_out.main(
                        [
                            "--a",
                            str(root / "a"),
                            "--b",
                            str(root / "b"),
                            "--seed-expected",
                            "x",
                        ]
                    ),
                    2,
                )


if __name__ == "__main__":
    unittest.main()
