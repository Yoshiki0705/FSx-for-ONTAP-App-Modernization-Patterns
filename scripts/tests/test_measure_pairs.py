#!/usr/bin/env python3
"""Unit tests for scripts/measure/pairs.py and the APPMOD_PROBE_BIN hook in probe-launch.sh.

No network and no AWS: the AWS CLI is replaced by a recording runner, and the pair loop is driven
with injected send/collect doubles. Covered: the pair table equals the one in scripts/run-probe.sh,
the command lines equal what run-probe.sh builds when no override is given (checked by running
run-probe.sh under APPMOD_DRY_RUN), the override forms, argument validation, the stop-at-first-problem
rule, the manifest, scrubbing of the bucket from saved text, the expired-token stop, and that the
launcher runs the same command line as before when APPMOD_PROBE_BIN is unset.
"""

from __future__ import annotations

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

REPO = Path(__file__).resolve().parent.parent.parent
MEASURE = REPO / "scripts" / "measure"
sys.path.insert(0, str(MEASURE))

import pairs

RUN_ID = "s2m-20261012T090934Z"
WIN = "i-0123456789abcdef0"
LNX = "i-00000000000000000"
BUCKET = "example-artifacts-bucket"
PROBE_BIN = "/opt/appmod/modern/probe/DocIntake.Probe"
WIN_EXE = "C:\\appmod\\DocIntake-case2\\DocIntake.Probe.exe"


def cfg(**over) -> pairs.Config:
    base = {
        "run_id": RUN_ID,
        "stage": 2,
        "region": "ap-northeast-1",
        "svm_netbios": "APPMODSVM01",
        "windows_instance": WIN,
        "linux_instance": LNX,
        "out_dir": Path("out"),
        "bucket": BUCKET,
    }
    base.update(over)
    return pairs.Config(**base)


def record(**behaviors) -> str:
    """A minimal appmod-probe/1 record; keyword = behavior id -> observed dict or 'error'."""
    out = []
    for bid, val in behaviors.items():
        bid = bid.replace("_", "-")
        if val == "error":
            out.append({"id": bid, "outcome": "error"})
        else:
            out.append({"id": bid, "outcome": "measured", "observed": val})
    return json.dumps({"schema": "appmod-probe/1", "behaviors": out})


GOOD = record(file_locking={"x": 1})


def run_probe_manifest_rows() -> list[tuple[str, str, str, str, str]]:
    text = (REPO / "scripts" / "run-probe.sh").read_text()
    block = text.split("PAIRS=(", 1)[1].split("MANIFEST=", 1)[0]
    return re.findall(r'"(\S+) (\S+) (\S+) (\S+) (\S+)"', block)


class TableTests(unittest.TestCase):
    def test_pair_table_equals_run_probe_sh(self) -> None:
        rows = run_probe_manifest_rows()
        self.assertEqual(len(rows), 8)
        ours = [(p[1], p[2], p[3], p[4], p[5]) for p in pairs.PAIRS]
        self.assertEqual(ours, rows)
        self.assertEqual([p[0] for p in pairs.PAIRS], list(range(1, 9)))

    def test_select_pairs(self) -> None:
        self.assertEqual([p[0] for p in pairs.select_pairs(2, [])], list(range(1, 9)))
        self.assertEqual([p[0] for p in pairs.select_pairs(1, [3, 1])], [1, 3])
        self.assertEqual([p[0] for p in pairs.select_pairs(0, [])], [1, 2, 3, 4])
        with self.assertRaises(ValueError):
            pairs.select_pairs(0, [5])
        with self.assertRaises(ValueError):
            pairs.select_pairs(2, [9])
        with self.assertRaises(ValueError):
            pairs.select_pairs(2, [0])


class CommandLineTests(unittest.TestCase):
    def test_defaults_match_run_probe_sh_dry_run(self) -> None:
        """Without overrides, every command equals the one run-probe.sh sends."""
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run(
                [
                    "bash",
                    str(REPO / "scripts" / "run-probe.sh"),
                    "--stage",
                    "2",
                    "--run-id",
                    RUN_ID,
                    "--windows-instance",
                    WIN,
                    "--linux-instance",
                    LNX,
                    "--bucket",
                    BUCKET,
                ],
                cwd=t,
                env={**os.environ, "APPMOD_DRY_RUN": "1"},
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        sent = set(
            re.findall(r'--parameters commands=\["(.*?)"\]', r.stdout + r.stderr)
        )
        expected = set()
        c = cfg()
        for n, behavior, s1, r1, s2, r2 in pairs.PAIRS:
            sid = pairs.sync_id_for(RUN_ID, n)
            expected.add(pairs.command_line(c, s1, r1, behavior, sid))
            expected.add(pairs.command_line(c, s2, r2, behavior, sid))
        self.assertTrue(expected <= sent, msg=sorted(expected - sent))

    def test_windows_standalone_golden(self) -> None:
        self.assertEqual(
            pairs.command_line(cfg(), "windows", "standalone"),
            "powershell -ExecutionPolicy Bypass -File C:\\appmod\\probe-launch.ps1 "
            f"-Stage 2 -RunId {RUN_ID} -Role standalone -SvmNetbios APPMODSVM01 "
            "-Region ap-northeast-1",
        )

    def test_windows_pair_with_probe_exe(self) -> None:
        got = pairs.command_line(
            cfg(windows_probe_exe=WIN_EXE), "windows", "holder", "file-locking", "sid-1"
        )
        self.assertIn(
            f"-ProbeExe {WIN_EXE} -PairBehavior file-locking -SyncId sid-1 -Bucket {BUCKET}",
            got,
        )

    def test_linux_pair_default_launcher(self) -> None:
        self.assertEqual(
            pairs.command_line(
                cfg(), "linux-smb", "contender", "file-locking", "sid-1"
            ),
            f"bash /opt/appmod/probe-launch.sh --store smb --stage 2 --role contender "
            f"--run-id {RUN_ID} --region ap-northeast-1 --svm-netbios APPMODSVM01 "
            f"--pair-behavior file-locking --sync-id sid-1 --bucket {BUCKET}",
        )

    def test_linux_nfs_with_probe_bin_and_launcher(self) -> None:
        got = pairs.command_line(
            cfg(linux_probe_bin=PROBE_BIN, linux_launcher="/opt/appmod/launch.sh"),
            "linux-nfs",
            "writer",
            "write-visibility",
            "sid-3",
        )
        self.assertTrue(
            got.startswith(
                f"APPMOD_PROBE_BIN={PROBE_BIN} bash /opt/appmod/launch.sh --store nfs"
            )
        )

    def test_standalone_commands_carry_no_bucket(self) -> None:
        for side in ("windows", "linux-smb", "linux-nfs"):
            self.assertNotIn(BUCKET, pairs.command_line(cfg(), side, "standalone"))

    def test_unknown_side(self) -> None:
        with self.assertRaises(ValueError):
            pairs.command_line(cfg(), "linux-ext4", "holder")

    def test_ids_and_stems(self) -> None:
        self.assertEqual(pairs.sync_id_for(RUN_ID, 7), f"{RUN_ID}-p7")
        self.assertEqual(
            pairs.pair_stems(1, "windows", "holder", "linux-smb", "contender"),
            ("pair-1-windows-holder", "pair-1-linux-smb-contender"),
        )

    def test_manifest_line_format(self) -> None:
        p = pairs.PAIRS[0]
        line = pairs.manifest_line(
            1,
            p[1],
            f"{RUN_ID}-p1",
            p,
            "pair-1-windows-holder",
            "pair-1-linux-smb-contender",
        )
        self.assertEqual(
            line,
            f"1\tfile-locking\t{RUN_ID}-p1\twindows\tholder\tpair-1-windows-holder.json"
            "\tlinux-smb\tcontender\tpair-1-linux-smb-contender.json\n",
        )

    def test_request_json(self) -> None:
        req = pairs.build_request(cfg(), "windows", "cmd", "pair-1-windows-holder")
        self.assertEqual(req["InstanceIds"], [WIN])
        self.assertEqual(req["DocumentName"], "AWS-RunPowerShellScript")
        self.assertEqual(req["Parameters"]["executionTimeout"], ["900"])
        self.assertEqual(
            req["OutputS3KeyPrefix"], f"probe/{RUN_ID}/pair-1-windows-holder"
        )
        self.assertEqual(
            pairs.build_request(cfg(), "linux-nfs", "c", "s")["DocumentName"],
            "AWS-RunShellScript",
        )


class ValidationTests(unittest.TestCase):
    def test_valid_config_passes(self) -> None:
        pairs.validate_config(cfg(windows_probe_exe=WIN_EXE, linux_probe_bin=PROBE_BIN))

    def test_rejections(self) -> None:
        bad = {
            "instance": {"windows_instance": "x-0123456789abcdef0"},
            "run id": {"run_id": "s2-2026"},
            "stage": {"stage": 4},
            "region": {"region": "tokyo"},
            "netbios": {"svm_netbios": "A" * 16},
            "bucket": {"bucket": "Bad_Bucket"},
            "win exe": {"windows_probe_exe": "DocIntake.Probe.exe"},
            "win exe injection": {"windows_probe_exe": "C:\\a.exe; calc"},
            "launcher relative": {"linux_launcher": "probe-launch.sh"},
            "launcher dotdot": {"linux_launcher": "/opt/appmod/../etc/x.sh"},
            "probe bin space": {"linux_probe_bin": "/opt/app mod/p"},
            "probe bin semicolon": {"linux_probe_bin": "/opt/x;rm"},
            "timeout": {"timeout": 5},
        }
        for label, over in bad.items():
            with self.assertRaises(ValueError, msg=label):
                pairs.validate_config(cfg(**over))


class CheckRecordTests(unittest.TestCase):
    def test_usable_records(self) -> None:
        self.assertEqual(pairs.check_record(GOOD), "")
        self.assertEqual(pairs.check_record("\ufeff" + GOOD), "")

    def test_problems(self) -> None:
        self.assertEqual(pairs.check_record("not json"), "no JSON record")
        self.assertEqual(pairs.check_record("[1]"), "no JSON record")
        self.assertIn(
            "file-locking outcome=error",
            pairs.check_record(record(file_locking="error")),
        )
        unresolved = record(case_sensitivity={"real_resolved": False})
        self.assertIn("real_resolved false", pairs.check_record(unresolved))
        resolved = record(case_sensitivity={"real_resolved": True})
        self.assertEqual(pairs.check_record(resolved), "")


class Doubles:
    """send/collect doubles that record the order of calls."""

    def __init__(self, outputs: dict[str, tuple[str, str]] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.outputs = outputs or {}

    def send(self, side: str, cmd: str, stem: str) -> tuple[str, str]:
        self.calls.append(("send", stem))
        return f"cid-{stem}", f"inst-{side}"

    def collect(self, cid: str, inst: str, stem: str) -> tuple[str, str]:
        self.calls.append(("collect", stem))
        return self.outputs.get(stem, ("Success", GOOD))


class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def manifest(self) -> list[str]:
        p = self.out / "pairs-manifest.tsv"
        return p.read_text().splitlines() if p.exists() else []

    def test_all_pairs_both_sends_before_collects(self) -> None:
        d = Doubles()
        with redirect_stdout(io.StringIO()):
            code = pairs.run_pairs(cfg(out_dir=self.out), [], d.send, d.collect)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.manifest()), 8)
        first = d.calls[:4]
        self.assertEqual([k for k, _ in first], ["send", "send", "collect", "collect"])

    def test_stage_zero_runs_four_smb_pairs(self) -> None:
        d = Doubles()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                pairs.run_pairs(cfg(stage=0, out_dir=self.out), [], d.send, d.collect),
                0,
            )
        self.assertEqual(len(self.manifest()), 4)
        self.assertFalse(any("nfs" in stem for _, stem in d.calls))

    def test_error_outcome_stops_before_later_pairs(self) -> None:
        d = Doubles(
            {"pair-3-windows-writer": ("Success", record(write_visibility="error"))}
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pairs.run_pairs(cfg(out_dir=self.out), [], d.send, d.collect)
        self.assertEqual(code, 1)
        self.assertIn("STOP pair 3", buf.getvalue())
        self.assertEqual(len(self.manifest()), 2)
        self.assertFalse(any(stem.startswith("pair-4") for _, stem in d.calls))

    def test_non_success_status_stops(self) -> None:
        d = Doubles({"pair-1-linux-smb-contender": ("Failed", "")})
        with redirect_stdout(io.StringIO()):
            code = pairs.run_pairs(cfg(out_dir=self.out), [], d.send, d.collect)
        self.assertEqual(code, 1)
        self.assertEqual(self.manifest(), [])

    def test_selected_numbers_only(self) -> None:
        d = Doubles()
        with redirect_stdout(io.StringIO()):
            pairs.run_pairs(cfg(out_dir=self.out), [3, 5], d.send, d.collect)
        self.assertEqual([r.split("\t")[0] for r in self.manifest()], ["3", "5"])

    def test_windows_standalone_unresolved_control_stops(self) -> None:
        d = Doubles(
            {"windows": ("Success", record(case_sensitivity={"real_resolved": False}))}
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                pairs.run_windows_standalone(cfg(out_dir=self.out), d.send, d.collect),
                1,
            )
        d = Doubles()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                pairs.run_windows_standalone(cfg(out_dir=self.out), d.send, d.collect),
                0,
            )


def ok_invocation(stdout: str = GOOD) -> subprocess.CompletedProcess:
    body = {
        "Status": "Success",
        "ResponseCode": 0,
        "StandardOutputContent": stdout,
        "StandardErrorContent": "",
    }
    return subprocess.CompletedProcess(["aws"], 0, json.dumps(body), "")


class FakeAws:
    def __init__(
        self, bucket_rc: int = 0, send_rc: int = 0, expired_on: str = ""
    ) -> None:
        self.calls: list[list[str]] = []
        self.requests: list[dict] = []
        self.bucket_rc, self.send_rc, self.expired_on = bucket_rc, send_rc, expired_on

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        service = argv[3] if len(argv) > 3 else ""
        if self.expired_on and self.expired_on in argv:
            return subprocess.CompletedProcess(
                argv, 255, "", "Token has expired and refresh failed"
            )
        if "describe-stacks" in argv:
            if self.bucket_rc:
                return subprocess.CompletedProcess(
                    argv, 255, "", "stack does not exist"
                )
            return subprocess.CompletedProcess(argv, 0, BUCKET + "\n", "")
        if "send-command" in argv:
            path = argv[argv.index("--cli-input-json") + 1].removeprefix("file://")
            self.requests.append(json.loads(Path(path).read_text()))
            if self.send_rc:
                return subprocess.CompletedProcess(argv, 255, "", "AccessDenied")
            return subprocess.CompletedProcess(
                argv, 0, f"cmd-{len(self.requests)}\n", ""
            )
        if "get-command-invocation" in argv:
            return ok_invocation()
        raise AssertionError(f"unexpected call {argv} ({service})")


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "run"

    def argv(self, *extra: str) -> list[str]:
        return [
            "--run-id",
            RUN_ID,
            "--stage",
            "1",
            "--windows-instance",
            WIN,
            "--linux-instance",
            LNX,
            "--out-dir",
            str(self.out),
            *extra,
        ]

    def run_main(self, argv, runner):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pairs.main(argv, runner=runner, sleep=lambda s: None)
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end_pairs_with_bucket_from_stack(self) -> None:
        fake = FakeAws()
        code, out, err = self.run_main(
            self.argv("--linux-probe-bin", PROBE_BIN, "pairs", "1"), fake
        )
        self.assertEqual(code, 0, msg=err)
        self.assertIn("describe-stacks", fake.calls[0])
        self.assertIn("appmod-base", fake.calls[0])
        self.assertTrue((self.out / "pair-1-windows-holder.json").exists())
        self.assertTrue((self.out / "pair-1-linux-smb-contender.stderr.txt").exists())
        self.assertEqual(
            len((self.out / "pairs-manifest.tsv").read_text().splitlines()), 1
        )
        # The request carries the real bucket; nothing saved does.
        self.assertEqual(fake.requests[0]["OutputS3BucketName"], BUCKET)
        for p in self.out.iterdir():
            self.assertNotIn(BUCKET, p.read_text(), msg=p.name)
        self.assertIn(
            "ARTIFACTS_BUCKET", (self.out / "pair-1-windows-holder.cmd.txt").read_text()
        )
        self.assertIn(
            f"APPMOD_PROBE_BIN={PROBE_BIN}",
            (self.out / "pair-1-linux-smb-contender.cmd.txt").read_text(),
        )
        self.assertIn("scripts/probe_merge.py", out)
        self.assertIn('--sides "windows linux-smb linux-nfs"', out)

    def test_bucket_argument_skips_the_stack_lookup(self) -> None:
        fake = FakeAws()
        code, _, _ = self.run_main(
            self.argv("--bucket", BUCKET, "windows-standalone"), fake
        )
        self.assertEqual(code, 0)
        self.assertFalse(any("describe-stacks" in c for c in fake.calls))
        self.assertTrue((self.out / "windows.json").exists())

    def test_exit_codes(self) -> None:
        self.assertEqual(self.run_main(self.argv("pairs"), FakeAws(bucket_rc=1))[0], 21)
        self.assertEqual(
            self.run_main(self.argv("--bucket", BUCKET, "pairs"), FakeAws(send_rc=1))[
                0
            ],
            22,
        )
        code, _, err = self.run_main(
            self.argv("--bucket", BUCKET, "pairs"), FakeAws(expired_on="send-command")
        )
        self.assertEqual(code, 20)
        self.assertIn("aws sso login", err)

    def test_existing_output_is_refused_before_any_call(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / "pair-1-windows-holder.json").write_text("{}")

        def no_calls(argv, **kw):
            raise AssertionError("AWS must not be called")

        code, _, err = self.run_main(
            self.argv("--bucket", BUCKET, "pairs", "1"), no_calls
        )
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_bad_arguments_exit_2(self) -> None:
        def no_calls(argv, **kw):
            raise AssertionError("AWS must not be called")

        cases = [
            [
                "--run-id",
                "bad",
                "--stage",
                "1",
                "--windows-instance",
                WIN,
                "--linux-instance",
                LNX,
                "pairs",
            ],
            [
                "--run-id",
                RUN_ID,
                "--stage",
                "0",
                "--windows-instance",
                WIN,
                "--linux-instance",
                LNX,
                "pairs",
                "5",
            ],
            [
                "--run-id",
                RUN_ID,
                "--stage",
                "1",
                "--windows-instance",
                "",
                "--linux-instance",
                LNX,
                "pairs",
            ],
            [
                "--run-id",
                RUN_ID,
                "--stage",
                "1",
                "--windows-instance",
                WIN,
                "--linux-instance",
                LNX,
                "windows-standalone",
                "1",
            ],
        ]
        for argv in cases:
            self.assertEqual(self.run_main(argv, no_calls)[0], 2, msg=argv)

    def test_dry_run_never_calls_aws_or_writes(self) -> None:
        def no_calls(argv, **kw):
            raise AssertionError("AWS must not be called")

        code, out, _ = self.run_main(
            self.argv("--bucket", BUCKET, "--dry-run", "pairs", "2"), no_calls
        )
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN pair-2-linux-smb-holder", out)
        self.assertNotIn(BUCKET, out)
        self.assertFalse(self.out.exists())

    def test_help_exits_zero(self) -> None:
        r = subprocess.run(
            [sys.executable, str(MEASURE / "pairs.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("--linux-probe-bin", r.stdout)


# ----- the APPMOD_PROBE_BIN hook in scripts/probe-launch.sh ------------------------------------

LAUNCHER = REPO / "scripts" / "probe-launch.sh"
SETPRIV = """#!/bin/bash
printf 'setpriv %s %s %s\\n' "$1" "$2" "$3" >>"$SHIM_LOG"
shift 3
exec "$@"
"""
MOUNTPOINT = "#!/bin/bash\nexit 0\n"
PYTHON3 = """#!/bin/bash
printf 'python3 %s\\n' "$*" >>"$SHIM_LOG"
"""
STUB_PROBE = """#!/bin/bash
printf 'probe-bin %s\\n' "$*" >>"$SHIM_LOG"
"""


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ProbeLaunchHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        shims = root / "shims"
        shims.mkdir()
        write_exec(shims / "setpriv", SETPRIV)
        write_exec(shims / "mountpoint", MOUNTPOINT)
        write_exec(shims / "python3", PYTHON3)
        self.shims = shims
        self.stub = root / "stub-probe"
        write_exec(self.stub, STUB_PROBE)
        self.log = root / "log.txt"
        self.log.write_text("")

    def run_launcher(self, **env: str) -> subprocess.CompletedProcess:
        base = {
            "PATH": f"{self.shims}:{os.environ['PATH']}",
            "SHIM_LOG": str(self.log),
            "HOME": self.tmp.name,
        }
        base.update(env)
        return subprocess.run(
            [
                "bash",
                str(LAUNCHER),
                "--store",
                "nfs",
                "--stage",
                "1",
                "--role",
                "standalone",
                "--run-id",
                RUN_ID,
            ],
            env=base,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_unset_runs_the_python_probe_as_before(self) -> None:
        r = self.run_launcher()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            self.log.read_text().splitlines(),
            [
                "setpriv --reuid=10001 --regid=10001 --clear-groups",
                f"python3 /opt/appmod/probe_peer.py --store nfs --root /mnt/appdata --stage 1 --role standalone --run-id {RUN_ID}",
            ],
        )

    def test_set_runs_the_binary_with_the_same_arguments(self) -> None:
        r = self.run_launcher(APPMOD_PROBE_BIN=str(self.stub))
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            self.log.read_text().splitlines(),
            [
                "setpriv --reuid=10001 --regid=10001 --clear-groups",
                f"probe-bin --store nfs --root /mnt/appdata --stage 1 --role standalone --run-id {RUN_ID}",
            ],
        )

    def test_bad_value_exits_2_before_anything_runs(self) -> None:
        for value in (
            "relative/probe",
            str(self.stub) + "-missing",
            "/tmp/x;rm",
            str(self.stub.parent),
        ):
            self.log.write_text("")
            r = self.run_launcher(APPMOD_PROBE_BIN=value)
            self.assertEqual(r.returncode, 2, msg=value)
            self.assertEqual(self.log.read_text(), "", msg=value)

    def test_every_invocation_goes_through_the_chosen_command(self) -> None:
        code = [
            line
            for line in LAUNCHER.read_text().splitlines()
            if not line.lstrip().startswith("#")
        ]
        text = "\n".join(code)
        self.assertNotIn('python3 "$PROBE" --store', text)
        self.assertEqual(text.count('"${PROBE_CMD[@]}" --store'), 3)

    def test_script_still_parses(self) -> None:
        r = subprocess.run(
            ["bash", "-n", str(LAUNCHER)], capture_output=True, text=True, check=False
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
