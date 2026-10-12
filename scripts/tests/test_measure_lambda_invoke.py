#!/usr/bin/env python3
"""Unit tests for scripts/measure/lambda_invoke.py (no network, no AWS).

The AWS CLI is replaced by a recording runner that writes the response file the way
`aws lambda invoke` does. Covered: payload building and validation (the handler's rules), the
argv, the function name from the stack, every failure path (FunctionError, a worker exit code, a
probe record that is wrong or lacks a behavior, an uploaded copy that differs, an expired token),
that nothing is overwritten, that --dry-run never reaches the runner, and that the driver never
touches the EventBridge schedule. Fixtures are written by hand with a placeholder account.
"""

from __future__ import annotations

import io
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
import lambda_invoke as li

RUN_ID = "s3m-20261012T104800Z"
BUCKET = "example-artifacts-bucket"
AP = "arn:aws:s3:ap-northeast-1:123456789012:accesspoint/example-ap"

WORKER_OK = {
    "mode": "worker",
    "run_id": RUN_ID,
    "stage": "s3",
    "exit_code": 0,
    "output_prefix": "out/s3/",
}


def probe_record(run_id: str = RUN_ID, **over) -> dict:
    behaviors = [
        {
            "id": "case-sensitivity",
            "outcome": "measured",
            "observed": {"real_resolved": True},
        },
        {"id": "path-separator", "outcome": "measured", "observed": {}},
        {"id": "file-locking", "outcome": "skipped"},
        {
            "id": "acl-evaluation",
            "outcome": "measured",
            "observed": {"io_put_allowed": True},
        },
        {"id": "write-visibility", "outcome": "measured", "observed": {}},
    ]
    rec = {
        "schema": "appmod-probe/1",
        "run_id": run_id,
        "stage": 3,
        "role": "writer",
        "store": {"root": AP},
        "behaviors": behaviors,
    }
    rec.update(over)
    return rec


def cli_ok() -> str:
    return json.dumps({"StatusCode": 200, "ExecutedVersion": "$LATEST"})


class FakeAws:
    """Answers `lambda invoke` (writing the response file) and `s3 cp` (writing the download)."""

    def __init__(
        self,
        body: object,
        cli: str | None = None,
        uploaded: object = None,
        rc: int = 0,
        s3_rc: int = 0,
    ):
        self.body, self.cli, self.uploaded = body, cli or cli_ok(), uploaded
        self.rc, self.s3_rc = rc, s3_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "invoke" in argv:
            if self.rc:
                return subprocess.CompletedProcess(
                    argv, 255, "", "ResourceNotFoundException"
                )
            Path(argv[-1]).write_text(json.dumps(self.body))
            return subprocess.CompletedProcess(argv, 0, self.cli, "")
        if "cp" in argv:
            if self.s3_rc:
                return subprocess.CompletedProcess(argv, 1, "", "NoSuchKey")
            Path(argv[argv.index("cp") + 2]).write_text(json.dumps(self.uploaded))
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected call {argv}")


class PayloadTests(unittest.TestCase):
    def test_valid_payload_is_exactly_the_handler_input(self) -> None:
        for mode in ("worker", "probe"):
            self.assertEqual(
                json.loads(li.build_payload(mode, RUN_ID)),
                {"mode": mode, "run_id": RUN_ID, "stage": "s3"},
            )
        self.assertEqual(
            li.build_payload("probe", "r1"),
            '{"mode":"probe","run_id":"r1","stage":"s3"}',
        )

    def test_run_id_follows_the_handler_pattern(self) -> None:
        for ok in ("a", "s3-20261011T212200Z", "A.b_c-d", "a" * 64):
            self.assertEqual(li.validate_run_id(ok), ok)
        for bad in (
            "",
            "a/b",
            "-start",
            ".start",
            "a" * 65,
            "a b",
            "a;b",
            "run\n",
            "a\\b",
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                li.validate_run_id(bad)

    def test_mode_is_worker_or_probe(self) -> None:
        for bad in ("", "Worker", "both", "probe "):
            with self.assertRaises(ValueError, msg=repr(bad)):
                li.build_payload(bad, RUN_ID)

    def test_function_name_comes_from_the_stack(self) -> None:
        self.assertEqual(li.function_name_for("appmod-stage3"), "appmod-stage3-worker")
        self.assertEqual(li.function_name_for("x", "custom-fn"), "custom-fn")
        for bad in (
            ("bad stack", ""),
            ("1stack", ""),
            ("ok", "a b"),
            ("ok", "a" * 65),
            ("ok", "arn:aws:lambda:x"),
        ):
            with self.assertRaises(ValueError, msg=bad):
                li.function_name_for(*bad)

    def test_argv_is_a_list_with_the_documented_flags(self) -> None:
        payload = li.build_payload("probe", RUN_ID)
        argv = li.invoke_argv("fn", "ap-northeast-1", payload, "out.json", 900)
        self.assertEqual(argv[:2], ["lambda", "invoke"])
        self.assertEqual(
            argv[argv.index("--cli-binary-format") + 1], "raw-in-base64-out"
        )
        self.assertEqual(argv[argv.index("--payload") + 1], payload)
        self.assertEqual(argv[-1], "out.json")
        self.assertNotIn("scheduler", " ".join(argv))


class CheckTests(unittest.TestCase):
    def test_invocation(self) -> None:
        self.assertEqual(li.check_invocation({"StatusCode": 200}), [])
        self.assertTrue(
            li.check_invocation({"StatusCode": 200, "FunctionError": "Unhandled"})
        )
        self.assertTrue(li.check_invocation({"StatusCode": 500}))
        self.assertTrue(li.check_invocation({}))

    def test_worker_result(self) -> None:
        self.assertEqual(li.check_worker_result(WORKER_OK, RUN_ID), [])
        for key, value in (
            ("exit_code", 1),
            ("stage", "s2"),
            ("run_id", "other"),
            ("mode", "probe"),
            ("output_prefix", "out/s0/"),
        ):
            self.assertTrue(
                li.check_worker_result({**WORKER_OK, key: value}, RUN_ID), msg=key
            )
        self.assertTrue(li.check_worker_result([], RUN_ID))
        self.assertTrue(
            li.check_worker_result(
                {k: v for k, v in WORKER_OK.items() if k != "exit_code"}, RUN_ID
            )
        )

    def test_probe_result(self) -> None:
        self.assertEqual(li.check_probe_result(probe_record(), RUN_ID), [])
        self.assertTrue(
            li.check_probe_result(probe_record(schema="appmod-probe/2"), RUN_ID)
        )
        self.assertTrue(li.check_probe_result(probe_record(run_id="other"), RUN_ID))
        short = probe_record()
        short["behaviors"] = short["behaviors"][:-1]
        self.assertIn(
            "missing behaviors", " ".join(li.check_probe_result(short, RUN_ID))
        )
        errored = probe_record()
        errored["behaviors"][3] = {"id": "acl-evaluation", "outcome": "error"}
        self.assertIn("outcome=error", " ".join(li.check_probe_result(errored, RUN_ID)))
        control_less = probe_record()
        control_less["behaviors"][0]["observed"] = {"real_resolved": False}
        self.assertIn(
            "real_resolved false", " ".join(li.check_probe_result(control_less, RUN_ID))
        )

    def test_same_json_ignores_key_order_only(self) -> None:
        self.assertTrue(li.same_json({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}))
        self.assertFalse(li.same_json({"b": [1, 2]}, {"b": [2, 1]}))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "run"

    def run_li(
        self, fake: FakeAws, mode: str = "probe", bucket: str = ""
    ) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = li.run(
                mode=mode,
                run_id=RUN_ID,
                function_name="appmod-stage3-worker",
                region="ap-northeast-1",
                out_dir=self.out,
                bucket=bucket,
                runner=fake,
            )
        return code, buf.getvalue()

    def files(self) -> list[str]:
        return sorted(p.name for p in self.out.iterdir())

    def test_worker_success_saves_the_result(self) -> None:
        code, out = self.run_li(FakeAws(WORKER_OK), "worker")
        self.assertEqual(code, 0, msg=out)
        self.assertEqual(self.files(), ["lambda-worker-result.json"])
        self.assertEqual(
            json.loads((self.out / "lambda-worker-result.json").read_text()), WORKER_OK
        )

    def test_worker_nonzero_exit_code_is_a_failure(self) -> None:
        code, out = self.run_li(FakeAws({**WORKER_OK, "exit_code": 1}), "worker")
        self.assertEqual(code, 1)
        self.assertIn("exit_code", out)

    def test_function_error_keeps_the_body_under_the_error_name(self) -> None:
        body = {
            "errorType": "InvalidOperationException",
            "errorMessage": "worker exited 1; see the function log",
        }
        cli = json.dumps({"StatusCode": 200, "FunctionError": "Unhandled"})
        code, out = self.run_li(FakeAws(body, cli), "worker")
        self.assertEqual(code, 1)
        self.assertIn("FunctionError", out)
        self.assertEqual(self.files(), ["lambda-worker-error.json"])

    def test_probe_success_with_matching_upload(self) -> None:
        fake = FakeAws(probe_record(), uploaded=probe_record())
        code, out = self.run_li(fake, "probe", BUCKET)
        self.assertEqual(code, 0, msg=out)
        self.assertEqual(self.files(), ["lambda-probe-result.json", "lambda-s3.json"])
        cp = next(c for c in fake.calls if "cp" in c)
        self.assertIn(f"s3://{BUCKET}/probe/{RUN_ID}/lambda-s3.json", cp)

    def test_probe_without_bucket_does_not_touch_s3(self) -> None:
        fake = FakeAws(probe_record())
        self.assertEqual(self.run_li(fake, "probe")[0], 0)
        self.assertFalse(any("s3" in c[:1] for c in fake.calls))

    def test_uploaded_copy_that_differs_fails(self) -> None:
        other = probe_record()
        other["behaviors"][1]["observed"] = {"changed": True}
        code, out = self.run_li(
            FakeAws(probe_record(), uploaded=other), "probe", BUCKET
        )
        self.assertEqual(code, 1)
        self.assertIn("uploaded copy differs", out)

    def test_download_failure_is_exit_11(self) -> None:
        self.assertEqual(
            self.run_li(FakeAws(probe_record(), s3_rc=1), "probe", BUCKET)[0], 11
        )

    def test_invoke_failure_is_exit_10(self) -> None:
        self.assertEqual(self.run_li(FakeAws({}, rc=1), "worker")[0], 10)

    def test_cli_output_that_is_not_json_is_exit_10(self) -> None:
        self.assertEqual(
            self.run_li(FakeAws(WORKER_OK, cli="not json"), "worker")[0], 10
        )
        self.assertEqual(self.run_li(FakeAws(WORKER_OK, cli="[1]"), "worker")[0], 10)

    def test_bad_probe_record_fails_before_any_download(self) -> None:
        fake = FakeAws(probe_record(schema="x"), uploaded=probe_record())
        self.assertEqual(self.run_li(fake, "probe", BUCKET)[0], 1)
        self.assertFalse(any("cp" in c for c in fake.calls))

    def test_expired_token_raises_for_main_to_report(self) -> None:
        def expired(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 255, "", "Token has expired and refresh failed"
            )

        with self.assertRaises(common.ExpiredTokenError):
            li.run(
                mode="worker",
                run_id=RUN_ID,
                function_name="f",
                region="ap-northeast-1",
                out_dir=self.out,
                runner=expired,
            )

    def test_no_temp_file_is_left_behind(self) -> None:
        self.run_li(FakeAws(WORKER_OK), "worker")
        self.run_li(FakeAws({}, rc=1), "worker")
        self.assertFalse([n for n in self.files() if n.endswith(".tmp")])


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "run"

    def main(self, *args: str, runner=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = li.main(
                ["--out-dir", str(self.out), *args], runner=runner or FakeAws(WORKER_OK)
            )
        return code, out.getvalue(), err.getvalue()

    def test_default_function_name_from_stack(self) -> None:
        fake = FakeAws(WORKER_OK)
        code, _, err = self.main("--mode", "worker", "--run-id", RUN_ID, runner=fake)
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(
            fake.calls[0][fake.calls[0].index("--function-name") + 1],
            "appmod-stage3-worker",
        )

    def test_bad_arguments_exit_2_without_a_call(self) -> None:
        def no_calls(argv, **kw):
            raise AssertionError("AWS must not be called")

        cases = [
            ("--mode", "worker", "--run-id", "a/b"),
            ("--mode", "worker", "--run-id", RUN_ID, "--region", "tokyo"),
            ("--mode", "worker", "--run-id", RUN_ID, "--bucket", "Bad_Bucket"),
            ("--mode", "worker", "--run-id", RUN_ID, "--timeout", "5"),
            ("--mode", "worker", "--run-id", RUN_ID, "--stack-name", "bad stack"),
        ]
        for args in cases:
            self.assertEqual(self.main(*args, runner=no_calls)[0], 2, msg=args)

    def test_existing_result_is_refused(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / "lambda-worker-result.json").write_text("{}")

        def no_calls(argv, **kw):
            raise AssertionError("AWS must not be called")

        code, _, err = self.main(
            "--mode", "worker", "--run-id", RUN_ID, runner=no_calls
        )
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_dry_run_prints_and_never_calls_aws(self) -> None:
        def no_calls(argv, **kw):
            raise AssertionError("AWS must not be called")

        code, out, _ = self.main(
            "--mode",
            "probe",
            "--run-id",
            RUN_ID,
            "--bucket",
            BUCKET,
            "--dry-run",
            runner=no_calls,
        )
        self.assertEqual(code, 0)
        self.assertIn('"stage":"s3"', out)
        self.assertIn("aws lambda invoke", out)
        self.assertFalse(self.out.exists())

    def test_expired_token_is_exit_20(self) -> None:
        def expired(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 255, "", "ExpiredToken: the security token expired"
            )

        code, _, err = self.main("--mode", "worker", "--run-id", RUN_ID, runner=expired)
        self.assertEqual(code, 20)
        self.assertIn("aws sso login", err)

    def test_driver_never_calls_the_scheduler(self) -> None:
        text = (MEASURE / "lambda_invoke.py").read_text()
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn('"scheduler"', code)
        self.assertNotIn("update-schedule", code)

    def test_help(self) -> None:
        r = subprocess.run(
            [sys.executable, str(MEASURE / "lambda_invoke.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("--mode", r.stdout)


if __name__ == "__main__":
    unittest.main()
