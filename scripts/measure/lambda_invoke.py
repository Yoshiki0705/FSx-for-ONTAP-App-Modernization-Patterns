#!/usr/bin/env python3
"""Invoke the stage-3 Lambda once (worker or probe) and save and check the result.

The handler is app/modern/src/DocIntake.Lambda (see its BUILD.md); the function is created by
templates/stage3-serverless.yaml as <stack-name>-worker. The payload is exactly the handler's
input: {"mode": "worker" | "probe", "run_id": "...", "stage": "s3"}. The run id must match
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$, which the handler also enforces before any S3 call.

Files written under --out-dir (an existing file is never overwritten):
  lambda-worker-result.json   the worker status {"mode","run_id","stage","exit_code","output_prefix"}
  lambda-probe-result.json    the appmod-probe/1 record returned by the probe
  lambda-s3.json              with --bucket (probe only): the copy the handler uploaded to
                              s3://<bucket>/probe/<run-id>/lambda-s3.json
  lambda-<mode>-error.json    the response body when the invocation reported a FunctionError

A failure is: a CLI error, a FunctionError (the handler throws when the worker exits non-zero), a
worker status other than exit_code 0 under out/s3/, a probe record that is not appmod-probe/1 for
this run id, that lacks one of the five behaviors, that carries an outcome of "error", or whose
case-sensitivity control did not resolve, or an uploaded copy that differs from the returned one.

This driver invokes the function and reads one S3 object; it never changes the EventBridge schedule.
It was written from the documented invoke command and the handler source and has not been run
against a live function in this form.

Exit codes: 0 done; 1 the result failed a check; 2 bad argument or an output file exists; 10 the
invoke call failed; 11 the S3 download failed; 20 AWS token expired (run `aws sso login` yourself).

Example (placeholders):
  lambda_invoke.py --mode probe --run-id s3m-20261012T104800Z --stack-name appmod-stage3 \\
      --bucket example-artifacts-bucket --out-dir .private/runs/s3m-20261012T104800Z
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common
import pairs

STAGE = "s3"
MODES = ("worker", "probe")
HANDLER_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FUNCTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
DEFAULT_STACK = "appmod-stage3"
PROBE_SCHEMA = "appmod-probe/1"
PROBE_BEHAVIORS = (
    "case-sensitivity",
    "path-separator",
    "file-locking",
    "acl-evaluation",
    "write-visibility",
)
WORKER_OUTPUT_PREFIX = f"out/{STAGE}/"
EXIT_FAILED_CHECK = 1
EXIT_INVOKE = 10
EXIT_DOWNLOAD = 11
INVOKE_TIMEOUT_MAX = 900  # the Lambda maximum, in seconds


def validate_mode(value: str) -> str:
    if value not in MODES:
        raise ValueError(f"invalid mode {value!r}: expected worker or probe")
    return value


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or not HANDLER_RUN_ID_RE.fullmatch(value):
        raise ValueError(
            f"invalid run id {value!r}: expected {HANDLER_RUN_ID_RE.pattern}"
        )
    return value


def validate_function_name(value: str) -> str:
    if not FUNCTION_NAME_RE.fullmatch(value):
        raise ValueError(f"invalid function name {value!r}")
    return value


def function_name_for(stack: str, explicit: str = "") -> str:
    """The template names the function <stack>-worker; an explicit name wins."""
    if explicit:
        return validate_function_name(explicit)
    if not STACK_RE.fullmatch(stack):
        raise ValueError(f"invalid stack name {stack!r}")
    return validate_function_name(f"{stack}-worker")


def build_payload(mode: str, run_id: str) -> str:
    """The invocation payload. The stage is fixed: the handler rejects any other value."""
    validate_mode(mode)
    validate_run_id(run_id)
    return json.dumps(
        {"mode": mode, "run_id": run_id, "stage": STAGE}, separators=(",", ":")
    )


def invoke_argv(
    function_name: str, region: str, payload: str, response_file: str, timeout: int
) -> list[str]:
    """The argv after `aws` (never a shell string)."""
    return [
        "lambda",
        "invoke",
        "--region",
        region,
        "--function-name",
        function_name,
        "--cli-binary-format",
        "raw-in-base64-out",
        "--cli-read-timeout",
        str(timeout),
        "--payload",
        payload,
        response_file,
    ]


def parse_cli_output(stdout: str) -> dict:
    try:
        obj = json.loads(stdout)
    except ValueError as exc:
        raise ValueError(f"the invoke call printed no JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise TypeError("the invoke call printed a JSON value that is not an object")
    return obj


def check_invocation(cli: dict) -> list[str]:
    problems = []
    if cli.get("FunctionError"):
        problems.append(f"FunctionError {cli['FunctionError']}")
    if cli.get("StatusCode") != 200:
        problems.append(f"StatusCode {cli.get('StatusCode')}")
    return problems


def check_worker_result(obj: object, run_id: str) -> list[str]:
    if not isinstance(obj, dict):
        return ["worker result is not a JSON object"]
    expected = {
        "mode": "worker",
        "run_id": run_id,
        "stage": STAGE,
        "exit_code": 0,
        "output_prefix": WORKER_OUTPUT_PREFIX,
    }
    return [
        f"{k}: expected {v!r}, got {obj.get(k)!r}"
        for k, v in expected.items()
        if obj.get(k) != v
    ]


def check_probe_result(obj: object, run_id: str) -> list[str]:
    if not isinstance(obj, dict):
        return ["probe result is not a JSON object"]
    problems = []
    if obj.get("schema") != PROBE_SCHEMA:
        problems.append(f"schema: expected {PROBE_SCHEMA!r}, got {obj.get('schema')!r}")
    if obj.get("run_id") != run_id:
        problems.append(f"run_id: expected {run_id!r}, got {obj.get('run_id')!r}")
    ids = [b.get("id") for b in obj.get("behaviors", []) if isinstance(b, dict)]
    missing = [b for b in PROBE_BEHAVIORS if b not in ids]
    if missing:
        problems.append(f"missing behaviors: {missing}")
    why = pairs.check_record(json.dumps(obj))
    if why:
        problems.append(why)
    return problems


def same_json(a: object, b: object) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def result_name(mode: str) -> str:
    return f"lambda-{mode}-result.json"


def error_name(mode: str) -> str:
    return f"lambda-{mode}-error.json"


def refuse_existing(out_dir: Path, mode: str, with_s3: bool) -> None:
    names = [result_name(mode), error_name(mode)]
    if with_s3:
        names.append("lambda-s3.json")
    for name in names:
        if (out_dir / name).exists():
            raise ValueError(
                f"{out_dir / name} already exists; use a new --out-dir or remove it yourself"
            )


def run(
    *,
    mode: str,
    run_id: str,
    function_name: str,
    region: str,
    out_dir: Path,
    bucket: str = "",
    timeout: int = INVOKE_TIMEOUT_MAX,
    runner: Callable = subprocess.run,
) -> int:
    """Invoke, save, check. Raises common.ExpiredTokenError."""
    payload = build_payload(mode, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".invoke-", suffix=".tmp")
    os.close(fd)
    try:
        rc, out, err = common.run_aws(
            invoke_argv(function_name, region, payload, tmp, timeout), runner
        )
        if rc != 0:
            sys.stderr.write(err)
            return EXIT_INVOKE
        try:
            cli = parse_cli_output(out)
        except (ValueError, TypeError) as exc:
            sys.stderr.write(f"lambda_invoke: {exc}\n")
            return EXIT_INVOKE
        body = Path(tmp).read_text()
        problems = check_invocation(cli)
        if problems:
            (out_dir / error_name(mode)).write_text(body)
            print("FAILED: " + "; ".join(problems))
            print(f"response body kept in {out_dir / error_name(mode)}")
            return EXIT_FAILED_CHECK
        try:
            result = json.loads(body)
        except ValueError:
            (out_dir / error_name(mode)).write_text(body)
            print("FAILED: the response body is not JSON")
            return EXIT_FAILED_CHECK
        (out_dir / result_name(mode)).write_text(body)
    finally:
        os.unlink(tmp)
    if mode == "worker":
        problems = check_worker_result(result, run_id)
    else:
        problems = check_probe_result(result, run_id)
        if bucket and not problems:
            uri = f"s3://{bucket}/probe/{run_id}/lambda-s3.json"
            dest = out_dir / "lambda-s3.json"
            rc, _, err = common.run_aws(
                ["s3", "cp", uri, str(dest), "--only-show-errors"], runner
            )
            if rc != 0:
                sys.stderr.write(err)
                return EXIT_DOWNLOAD
            try:
                uploaded = json.loads(dest.read_text())
            except ValueError:
                uploaded = None
            if not same_json(uploaded, result):
                problems.append("the uploaded copy differs from the returned record")
    if problems:
        print("FAILED: " + "; ".join(problems))
        return EXIT_FAILED_CHECK
    print(f"{mode} ok: run_id={run_id} -> {out_dir / result_name(mode)}")
    return common.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Environment: APPMOD_REGION (default ap-northeast-1), "
        "APPMOD_ARTIFACTS_BUCKET, APPMOD_DRY_RUN (any value).",
    )
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument(
        "--run-id", required=True, help="handler pattern, e.g. s3m-YYYYMMDDTHHMMSSZ"
    )
    ap.add_argument("--function-name", default="", help="default <stack-name>-worker")
    ap.add_argument(
        "--stack-name", default=DEFAULT_STACK, help=f"default {DEFAULT_STACK}"
    )
    ap.add_argument(
        "--bucket",
        default=os.environ.get("APPMOD_ARTIFACTS_BUCKET", ""),
        help="probe only: also fetch probe/<run-id>/lambda-s3.json and compare it",
    )
    ap.add_argument("--out-dir", default="", help="default .private/runs/<run-id>")
    ap.add_argument(
        "--region", default=os.environ.get("APPMOD_REGION", common.DEFAULT_REGION)
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=INVOKE_TIMEOUT_MAX,
        help=f"CLI read timeout in seconds, 30..{INVOKE_TIMEOUT_MAX} (default {INVOKE_TIMEOUT_MAX})",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the payload and argv; no AWS call"
    )
    return ap


def main(argv: Sequence[str] | None = None, runner: Callable = subprocess.run) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_mode(args.mode)
        validate_run_id(args.run_id)
        common.validate_region(args.region)
        name = function_name_for(args.stack_name, args.function_name)
        if args.bucket:
            common.validate_bucket(args.bucket)
        if not 30 <= args.timeout <= INVOKE_TIMEOUT_MAX:
            raise ValueError(
                f"invalid timeout {args.timeout}: expected 30..{INVOKE_TIMEOUT_MAX}"
            )
        out_dir = Path(args.out_dir or f".private/runs/{args.run_id}")
        use_bucket = args.bucket if args.mode == "probe" else ""
        payload = build_payload(args.mode, args.run_id)
        if args.dry_run or os.environ.get("APPMOD_DRY_RUN"):
            argv_out = invoke_argv(
                name,
                args.region,
                payload,
                str(out_dir / result_name(args.mode)),
                args.timeout,
            )
            print("payload: " + payload)
            print("aws " + shlex.join(argv_out))
            if use_bucket:
                print(
                    f"then: aws s3 cp s3://{use_bucket}/probe/{args.run_id}/lambda-s3.json {out_dir / 'lambda-s3.json'}"
                )
            return common.EXIT_OK
        refuse_existing(out_dir, args.mode, bool(use_bucket))
    except ValueError as exc:
        sys.stderr.write(f"lambda_invoke: {exc}\n")
        return common.EXIT_BAD_ARGUMENT
    try:
        return run(
            mode=args.mode,
            run_id=args.run_id,
            function_name=name,
            region=args.region,
            out_dir=out_dir,
            bucket=use_bucket,
            timeout=args.timeout,
            runner=runner,
        )
    except common.ExpiredTokenError as exc:
        sys.stderr.write(f"lambda_invoke: {exc}\n")
        return common.EXIT_EXPIRED_TOKEN


if __name__ == "__main__":
    sys.exit(main())
