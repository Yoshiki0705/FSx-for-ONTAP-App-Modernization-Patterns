#!/usr/bin/env python3
"""Unit tests for scripts/measure/common.py and scripts/measure/ssm_run.py (no network, no AWS).

The AWS CLI is replaced by a recording runner. Covered: the validators, the environment preamble,
the command list (the script decodes back to the original bytes and the exit code is propagated),
the request JSON, scrubbing of saved text, every exit-code path, the expired-token stop, and that
--dry-run never reaches the runner.
"""

from __future__ import annotations

import base64
import io
import ipaddress
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

MEASURE = Path(__file__).resolve().parent.parent / "measure"
sys.path.insert(0, str(MEASURE))

import common
import ssm_run

INSTANCE = "i-0123456789abcdef0"
RUN_ID = "s2m-20261012T090934Z"


def cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["aws"], rc, out, err)


class FakeAws:
    """Records argv and the send-command request body; answers from a queue."""

    def __init__(self, send=None, gets=None):
        self.calls: list[list[str]] = []
        self.requests: list[dict] = []
        self.send = send if send is not None else cp(0, "cmd-1\n")
        self.gets = list(gets or [])

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "send-command" in argv:
            path = argv[argv.index("--cli-input-json") + 1].removeprefix("file://")
            self.requests.append(json.loads(Path(path).read_text()))
            self.request_path = path
            return self.send
        return self.gets.pop(0) if self.gets else cp(1, "", "InvocationDoesNotExist")


def invocation(status="Success", code=0, out="hello\n", err=""):
    return cp(
        0,
        json.dumps(
            {
                "CommandId": "cmd-1",
                "Status": status,
                "ResponseCode": code,
                "StandardOutputContent": out,
                "StandardErrorContent": err,
                "Extra": "dropped",
            }
        ),
    )


def run(**kw):
    base = {
        "instance": INSTANCE,
        "platform": "linux",
        "body": b"echo hi\n",
        "comment": "c",
        "timeout": 30,
        "region": "ap-northeast-1",
        "sleep": lambda s: None,
        "now": lambda: 1700000000.0,
    }
    base.update(kw)
    return ssm_run.run_script(**base)


class ValidatorTests(unittest.TestCase):
    def test_instance_id(self) -> None:
        self.assertEqual(common.validate_instance_id(INSTANCE), INSTANCE)
        for bad in (
            "x-0123456789abcdef0",
            "i-0123",
            "i-0123456789abcdef012",
            "i-XYZ12345",
            "",
        ):
            with self.assertRaises(ValueError, msg=bad):
                common.validate_instance_id(bad)

    def test_run_id(self) -> None:
        for good in (
            "s2m-20261012T090934Z",
            "s0-20261012T090934Z",
            "s0w-20261012T102314Z",
        ):
            self.assertEqual(common.validate_run_id(good), good)
        for bad in (
            "s2-2026",
            "s2m-20261012",
            "x2m-20261012T090934Z",
            "s2mm0-20261012T090934Z",
        ):
            with self.assertRaises(ValueError, msg=bad):
                common.validate_run_id(bad)

    def test_stage_name(self) -> None:
        for good in ("s0", "s2m", "s4c"):
            common.validate_stage_name(good)
        for bad in ("out", "seed", "probe", "s", "S2", "s2mnx", "../s2"):
            with self.assertRaises(ValueError, msg=bad):
                common.validate_stage_name(bad)

    def test_dest_dir(self) -> None:
        common.validate_dest_dir("/opt/appmod/modern")
        common.validate_dest_dir("/opt/appmod/a/b.c")
        for bad in (
            "/opt/appmod",
            "/tmp/x",
            "/opt/appmod/../etc",
            "/opt/appmod/a/..",
            "/opt/appmod/a/./b",
            "opt/appmod/x",
            "/opt/appmod/a b",
        ):
            with self.assertRaises(ValueError, msg=bad):
                common.validate_dest_dir(bad)

    def test_relpath_and_component(self) -> None:
        common.validate_relpath("probe/DocIntake.Probe")
        for bad in ("/abs", "a/../b", "a//b", "a/./b", ".."):
            with self.assertRaises(ValueError, msg=bad):
                common.validate_relpath(bad)
        common.validate_component("probe")
        for bad in ("Probe", "1probe", "a/b", ""):
            with self.assertRaises(ValueError, msg=bad):
                common.validate_component(bad)

    def test_region_and_bucket(self) -> None:
        common.validate_region("ap-northeast-1")
        with self.assertRaises(ValueError):
            common.validate_region("tokyo")
        common.validate_bucket("example-bucket-123")
        with self.assertRaises(ValueError):
            common.validate_bucket("Bad_Bucket")

    def test_env_items(self) -> None:
        self.assertEqual(
            common.validate_env_item("APPMOD_X=/opt/a,b=c"), ("APPMOD_X", "/opt/a,b=c")
        )
        for bad in (
            "NOEQUALS",
            "PATH=/bin",
            "APPMOD_x=1",
            "APPMOD_P=pass word",
            "APPMOD_P=a;b",
            "APPMOD_P=$x",
        ):
            with self.assertRaises(ValueError, msg=bad):
                common.validate_env_item(bad)


class PreambleTests(unittest.TestCase):
    def test_bash_quotes_value(self) -> None:
        self.assertEqual(
            common.env_preamble("linux", {"APPMOD_A": "x'y"}),
            ["export APPMOD_A='x'\"'\"'y'"],
        )

    def test_powershell_doubles_quote(self) -> None:
        self.assertEqual(
            common.env_preamble("windows", {"APPMOD_A": "x'y"}),
            ["$env:APPMOD_A='x''y'"],
        )

    def test_name_outside_prefix_refused(self) -> None:
        with self.assertRaises(ValueError):
            common.env_preamble("linux", {"PATH": "/x"})

    def test_sorted_and_unknown_platform(self) -> None:
        lines = common.env_preamble("linux", {"APPMOD_B": "2", "APPMOD_A": "1"})
        self.assertEqual(lines, ["export APPMOD_A=1", "export APPMOD_B=2"])
        with self.assertRaises(ValueError):
            common.env_preamble("macos", {"APPMOD_A": "1"})


class ScrubTests(unittest.TestCase):
    def test_bucket_hostname_and_ip(self) -> None:
        # Built at run time so that no address-shaped literal sits in the tracked file.
        private_ip = str(ipaddress.IPv4Address(0x0A000102))
        other_ip = str(ipaddress.IPv4Address(0xC0000207))
        host = f"ip-{private_ip.replace('.', '-')}.ap-northeast-1.compute." + "internal"
        text = f"s3://example-bucket-1/probe from {host} at {private_ip} and {other_ip}"
        out = common.scrub(text, "example-bucket-1")
        self.assertNotIn("example-bucket-1", out)
        self.assertNotIn("ip-10-0-1-2", out)
        self.assertNotIn(private_ip, out)
        self.assertNotIn(other_ip, out)
        self.assertIn("ARTIFACTS_BUCKET", out)
        self.assertIn("linux-host", out)
        self.assertEqual(out.count("x.x.x.x"), 2)

    def test_no_bucket_is_fine(self) -> None:
        self.assertEqual(common.scrub("plain"), "plain")


class CommandTests(unittest.TestCase):
    def test_linux_commands_decode_and_propagate(self) -> None:
        body = b"#!/bin/bash\necho 'quoted'\nexit 7\n"
        cmds = ssm_run.build_commands("linux", body, 42, {"APPMOD_A": "1"})
        self.assertEqual(cmds[0], "export APPMOD_A=1")
        self.assertEqual(cmds[-2:], ["rm -f /tmp/appmod-ssm-42.sh", "exit $rc"])
        self.assertIn("rc=$?", cmds)
        printf = next(c for c in cmds if c.startswith("printf"))
        b64 = printf.split("'")[3]
        self.assertEqual(base64.b64decode(b64), body)
        self.assertIn("base64 -d > /tmp/appmod-ssm-42.sh", printf)

    def test_windows_commands(self) -> None:
        cmds = ssm_run.build_commands("windows", b"Write-Output 1\n", 42)
        self.assertTrue(
            cmds[0].startswith("[IO.File]::WriteAllBytes('C:\\appmod\\ssm-42.ps1'")
        )
        self.assertIn("[Convert]::FromBase64String(", cmds[0])
        self.assertEqual(cmds[-1], "exit $rc")
        self.assertIn("Remove-Item -Force 'C:\\appmod\\ssm-42.ps1'", cmds)

    def test_request_shape(self) -> None:
        req = ssm_run.build_request(INSTANCE, "linux", ["x"], "c" * 150, 600)
        self.assertEqual(req["DocumentName"], "AWS-RunShellScript")
        self.assertEqual(len(req["Comment"]), 100)
        self.assertEqual(req["Parameters"]["executionTimeout"], ["600"])
        self.assertNotIn("OutputS3BucketName", req)
        win = ssm_run.build_request(INSTANCE, "windows", ["x"], "c", 60)
        self.assertEqual(win["DocumentName"], "AWS-RunPowerShellScript")

    def test_output_s3_only_with_both(self) -> None:
        only_bucket = ssm_run.build_request(
            INSTANCE, "linux", ["x"], "c", 60, "example-bucket-1", None
        )
        self.assertNotIn("OutputS3BucketName", only_bucket)
        both = ssm_run.build_request(
            INSTANCE, "linux", ["x"], "c", 60, "example-bucket-1", "ssm/out"
        )
        self.assertEqual(both["OutputS3BucketName"], "example-bucket-1")
        self.assertEqual(both["OutputS3KeyPrefix"], "ssm/out")

    def test_redact_replaces_body(self) -> None:
        body = b"secret-looking-but-not\n"
        req = ssm_run.build_request(
            INSTANCE, "linux", ssm_run.build_commands("linux", body, 1), "c", 60
        )
        red = ssm_run.redact_request(req, body)
        text = json.dumps(red)
        self.assertNotIn(base64.b64encode(body).decode(), text)
        self.assertIn("sha256=", text)

    def test_status_mapping_and_result(self) -> None:
        self.assertEqual(ssm_run.exit_code_for_status("Success"), 0)
        for s in ("Failed", "TimedOut", "Cancelled", "InProgress", None):
            self.assertEqual(ssm_run.exit_code_for_status(s), 1)
        inv = json.loads(invocation().stdout)
        res = ssm_run.assemble_result(inv)
        self.assertEqual(list(res), list(ssm_run.RESULT_KEYS))
        self.assertNotIn("Extra", res)

    def test_timeout_bounds(self) -> None:
        ssm_run.validate_timeout(30)
        ssm_run.validate_timeout(3600)
        for bad in (29, 3601, 0, -1):
            with self.assertRaises(ValueError):
                ssm_run.validate_timeout(bad)


class RunScriptTests(unittest.TestCase):
    def test_success_after_pending_and_missing_invocation(self) -> None:
        aws = FakeAws(
            gets=[
                cp(1, "", "InvocationDoesNotExist"),
                invocation("InProgress"),
                invocation(),
            ]
        )
        code, res = run(runner=aws)
        self.assertEqual(code, 0)
        self.assertEqual(res["Status"], "Success")
        self.assertEqual(res["StandardOutputContent"], "hello\n")
        self.assertEqual(len(aws.calls), 4)
        self.assertEqual(aws.calls[0][:3], ["aws", "ssm", "send-command"])
        self.assertIn("--region", aws.calls[0])
        self.assertFalse(
            Path(aws.request_path).exists(), "request file must be unlinked"
        )

    def test_request_file_is_private_and_carries_script(self) -> None:
        seen = {}

        def runner(argv, **kw):
            if "send-command" in argv:
                p = Path(
                    argv[argv.index("--cli-input-json") + 1].removeprefix("file://")
                )
                seen["mode"] = p.stat().st_mode & 0o777
                return cp(0, "cmd-1\n")
            return invocation()

        run(runner=runner)
        self.assertEqual(seen["mode"], 0o600)

    def test_failed_status_is_exit_1(self) -> None:
        code, res = run(runner=FakeAws(gets=[invocation("Failed", 3, "", "boom")]))
        self.assertEqual(code, 1)
        self.assertEqual(res["ResponseCode"], 3)

    def test_send_failure_is_10(self) -> None:
        with redirect_stderr(io.StringIO()):
            code, res = run(runner=FakeAws(send=cp(255, "", "AccessDenied")))
        self.assertEqual((code, res), (10, None))

    def test_get_failure_is_11(self) -> None:
        with redirect_stderr(io.StringIO()):
            code, res = run(runner=FakeAws(gets=[cp(255, "", "AccessDenied")]))
        self.assertEqual((code, res), (11, None))

    def test_no_invocation_is_12(self) -> None:
        code, res = run(runner=FakeAws())
        self.assertEqual((code, res), (12, None))

    def test_expired_token_on_send_makes_one_call(self) -> None:
        aws = FakeAws(
            send=cp(255, "", "An error occurred: Token has expired and refresh failed")
        )
        with self.assertRaises(common.ExpiredTokenError):
            run(runner=aws)
        self.assertEqual(len(aws.calls), 1)

    def test_expired_token_on_poll_stops(self) -> None:
        aws = FakeAws(gets=[cp(255, "", "ExpiredToken: the security token")])
        with self.assertRaises(common.ExpiredTokenError):
            run(runner=aws)
        self.assertEqual(len(aws.calls), 2)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = Path(self.tmp.name) / "s.sh"
        self.script.write_bytes(b"echo hi\n")
        self.out = Path(self.tmp.name) / "out.json"

    def argv(self, *extra: str) -> list[str]:
        return [
            "--instance", INSTANCE, "--platform", "linux", "--script", str(self.script),
            "--comment", "c", "--out", str(self.out), *extra,
        ]  # fmt: skip

    def test_dry_run_never_calls_the_runner(self) -> None:
        def boom(*a, **k):
            raise AssertionError("runner must not be called in --dry-run")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ssm_run.main(
                self.argv("--dry-run", "--env", "APPMOD_A=1"), runner=boom
            )
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("export APPMOD_A=1", text)
        self.assertNotIn(base64.b64encode(b"echo hi\n").decode(), text)
        self.assertFalse(self.out.exists())

    def test_env_var_dry_run_is_honoured(self) -> None:
        def boom(*a, **k):
            raise AssertionError("runner must not be called")

        import os

        os.environ["APPMOD_DRY_RUN"] = "1"
        self.addCleanup(os.environ.pop, "APPMOD_DRY_RUN", None)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ssm_run.main(self.argv(), runner=boom), 0)

    def test_bad_arguments_exit_2(self) -> None:
        cases = [
            self.argv("--env", "PATH=/bin"),
            self.argv("--env", "APPMOD_P=has space"),
            self.argv("--timeout", "5"),
            self.argv("--region", "tokyo"),
            self.argv("--output-s3-bucket", "example-bucket-1"),
        ]
        for argv in cases:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(ssm_run.main(argv, runner=FakeAws()), 2, msg=argv)
        bad_instance = self.argv()
        bad_instance[1] = "not-an-instance"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ssm_run.main(bad_instance, runner=FakeAws()), 2)

    def test_missing_script_exit_2(self) -> None:
        self.script.unlink()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ssm_run.main(self.argv(), runner=FakeAws()), 2)

    def test_success_writes_result_and_report(self) -> None:
        aws = FakeAws(gets=[invocation(out="hello\n", err="warn")])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ssm_run.main(self.argv(), runner=aws, sleep=lambda s: None)
        self.assertEqual(code, 0)
        saved = json.loads(self.out.read_text())
        self.assertEqual(saved["Status"], "Success")
        self.assertIn("---stderr---\nwarn", buf.getvalue())
        self.assertIn("---status=Success rc=0---", buf.getvalue())

    def test_expired_token_exit_20(self) -> None:
        aws = FakeAws(send=cp(255, "", "Token has expired"))
        err = io.StringIO()
        with redirect_stderr(err):
            code = ssm_run.main(self.argv(), runner=aws, sleep=lambda s: None)
        self.assertEqual(code, 20)
        self.assertIn("aws sso login", err.getvalue())
        self.assertEqual(len(aws.calls), 1)


if __name__ == "__main__":
    unittest.main()
