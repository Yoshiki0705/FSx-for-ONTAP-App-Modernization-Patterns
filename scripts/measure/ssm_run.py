#!/usr/bin/env python3
"""Send one script to one instance through SSM Run Command and save the invocation result.

The script file is embedded as base64 in the command list, written to a temporary file on the
instance, executed, removed, and its exit code is returned. Nothing is retried, and no credential
path other than the caller's own AWS CLI session is tried. Variables for the script are given as
--env APPMOD_NAME=VALUE (the value shape is restricted; never pass a secret this way).

Exit codes: 0 Success; 1 the invocation ended in another status; 2 bad argument; 10 send-command
failed; 11 get-command-invocation failed; 12 no invocation returned; 20 the AWS token has expired
(run `aws sso login` yourself).

Examples:
  ssm_run.py --instance i-0123456789abcdef0 --platform linux --script host/host-hashes.sh \\
      --env APPMOD_HASH_DIRS=/opt/appmod/probe --comment "list hashes" --out hashes.json
  ssm_run.py --instance i-0123456789abcdef0 --platform linux --script host/standalone-probe.sh \\
      --env APPMOD_RUN_ID=s2m-20261012T090934Z --env APPMOD_STAGE=2 \\
      --env APPMOD_PROBE_BIN=/opt/appmod/modern/probe/DocIntake.Probe --comment probe --out out.json
  ssm_run.py ... --dry-run     # print the request with the script replaced by its sha256
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

POLL_SECONDS = 5
PENDING_STATUSES = ("Pending", "InProgress", "Delayed")
RESULT_KEYS = (
    "CommandId",
    "Status",
    "ResponseCode",
    "StandardOutputContent",
    "StandardErrorContent",
)
TIMEOUT_MIN, TIMEOUT_MAX = 30, 3600


def remote_path(platform: str, stamp: int) -> str:
    if platform == "linux":
        return f"/tmp/appmod-ssm-{stamp}.sh"
    if platform == "windows":
        return f"C:\\appmod\\ssm-{stamp}.ps1"
    raise ValueError(f"invalid platform: {platform!r}")


def document_name(platform: str) -> str:
    return {"linux": "AWS-RunShellScript", "windows": "AWS-RunPowerShellScript"}[
        platform
    ]


def build_commands(
    platform: str, body: bytes, stamp: int, env: Mapping[str, str] | None = None
) -> list[str]:
    """The command list: set variables, write the script, run it, remove it, return its code."""
    b64 = base64.b64encode(body).decode()
    remote = remote_path(platform, stamp)
    preamble = common.env_preamble(platform, env or {})
    if platform == "linux":
        return [
            *preamble,
            f"printf '%s' '{b64}' | base64 -d > {remote}",
            f"bash {remote}",
            "rc=$?",
            f"rm -f {remote}",
            "exit $rc",
        ]
    return [
        *preamble,
        f"[IO.File]::WriteAllBytes('{remote}', [Convert]::FromBase64String('{b64}'))",
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {remote}",
        "$rc = $LASTEXITCODE",
        f"Remove-Item -Force '{remote}'",
        "exit $rc",
    ]


def build_request(
    instance: str,
    platform: str,
    commands: list[str],
    comment: str,
    timeout: int,
    output_bucket: str | None = None,
    output_prefix: str | None = None,
) -> dict:
    req: dict = {
        "InstanceIds": [instance],
        "DocumentName": document_name(platform),
        "Comment": comment[:100],
        "Parameters": {"commands": commands, "executionTimeout": [str(timeout)]},
        "TimeoutSeconds": 60,
    }
    if output_bucket and output_prefix:
        req["OutputS3BucketName"] = output_bucket
        req["OutputS3KeyPrefix"] = output_prefix
    return req


def redact_request(req: dict, body: bytes) -> dict:
    """A copy of the request with the base64 script replaced by the script's sha256."""
    b64 = base64.b64encode(body).decode()
    marker = f"<script sha256={hashlib.sha256(body).hexdigest()} bytes={len(body)}>"
    out = json.loads(json.dumps(req))
    out["Parameters"]["commands"] = [
        c.replace(b64, marker) for c in out["Parameters"]["commands"]
    ]
    return out


def exit_code_for_status(status: str | None) -> int:
    return common.EXIT_OK if status == "Success" else common.EXIT_REMOTE_FAILED


def assemble_result(inv: Mapping) -> dict:
    return {k: inv.get(k) for k in RESULT_KEYS}


def format_report(res: Mapping) -> str:
    text = res.get("StandardOutputContent") or ""
    if res.get("StandardErrorContent"):
        text += "\n---stderr---\n" + res["StandardErrorContent"]
    return text + f"\n---status={res.get('Status')} rc={res.get('ResponseCode')}---\n"


def validate_timeout(value: int) -> int:
    if not TIMEOUT_MIN <= value <= TIMEOUT_MAX:
        raise ValueError(
            f"invalid timeout {value}: expected {TIMEOUT_MIN}..{TIMEOUT_MAX} seconds"
        )
    return value


def run_script(
    *,
    instance: str,
    platform: str,
    body: bytes,
    comment: str,
    timeout: int,
    region: str,
    env: Mapping[str, str] | None = None,
    output_bucket: str | None = None,
    output_prefix: str | None = None,
    runner: Callable = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> tuple[int, dict | None]:
    """Send, poll, and return (exit_code, result). Raises common.ExpiredTokenError."""
    commands = build_commands(platform, body, int(now()), env)
    req = build_request(
        instance, platform, commands, comment, timeout, output_bucket, output_prefix
    )
    # The request goes through a 0600 temp file passed as file://, so the base64 body never
    # appears in the process list. The file is removed whether or not the call succeeds.
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(req, f)
        rc, out, err = common.run_aws(
            [
                "ssm",
                "send-command",
                "--region",
                region,
                "--cli-input-json",
                "file://" + path,
                "--query",
                "Command.CommandId",
                "--output",
                "text",
            ],
            runner,
        )
    finally:
        os.unlink(path)
    if rc != 0:
        sys.stderr.write(err)
        return common.EXIT_SEND_FAILED, None
    command_id = out.strip()
    inv = None
    for _ in range(timeout // POLL_SECONDS + 5):
        sleep(POLL_SECONDS)
        rc, out, err = common.run_aws(
            [
                "ssm",
                "get-command-invocation",
                "--region",
                region,
                "--command-id",
                command_id,
                "--instance-id",
                instance,
            ],
            runner,
        )
        if rc != 0:
            if "InvocationDoesNotExist" in err:
                continue
            sys.stderr.write(err)
            return common.EXIT_GET_FAILED, None
        inv = json.loads(out)
        if inv.get("Status") not in PENDING_STATUSES:
            break
    if inv is None:
        return common.EXIT_NO_INVOCATION, None
    res = assemble_result(inv)
    return exit_code_for_status(res["Status"]), res


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Environment: APPMOD_REGION (default ap-northeast-1), APPMOD_DRY_RUN (any value).",
    )
    ap.add_argument("--instance", required=True, help="EC2 instance id (i-...)")
    ap.add_argument("--platform", required=True, choices=["linux", "windows"])
    ap.add_argument(
        "--script", required=True, help="script file to run (bash or PowerShell)"
    )
    ap.add_argument(
        "--comment", required=True, help="SSM comment (truncated to 100 characters)"
    )
    ap.add_argument("--out", required=True, help="file for the result JSON")
    ap.add_argument(
        "--timeout", type=int, default=600, help="seconds, 30..3600 (default 600)"
    )
    ap.add_argument(
        "--region",
        default=os.environ.get("APPMOD_REGION", common.DEFAULT_REGION),
        help="AWS region",
    )
    ap.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="APPMOD_NAME=VALUE",
        help="variable for the script; name must be APPMOD_*; never pass a secret",
    )
    ap.add_argument(
        "--output-s3-bucket", help="also write the SSM output to this bucket"
    )
    ap.add_argument("--output-s3-prefix", help="key prefix for --output-s3-bucket")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request (script replaced by its sha256) and make no AWS call",
    )
    return ap


def main(
    argv: Sequence[str] | None = None,
    runner: Callable = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        common.validate_instance_id(args.instance)
        common.validate_region(args.region)
        validate_timeout(args.timeout)
        env = common.parse_env_items(args.env)
        if bool(args.output_s3_bucket) != bool(args.output_s3_prefix):
            raise ValueError(
                "--output-s3-bucket and --output-s3-prefix must be given together"
            )
        if args.output_s3_bucket:
            common.validate_bucket(args.output_s3_bucket)
        body = Path(args.script).read_bytes()
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"ssm_run: {exc}\n")
        return common.EXIT_BAD_ARGUMENT
    dry = args.dry_run or bool(os.environ.get("APPMOD_DRY_RUN"))
    if dry:
        commands = build_commands(args.platform, body, 0, env)
        req = build_request(
            args.instance,
            args.platform,
            commands,
            args.comment,
            args.timeout,
            args.output_s3_bucket,
            args.output_s3_prefix,
        )
        print(json.dumps(redact_request(req, body), indent=1))
        return common.EXIT_OK
    try:
        code, res = run_script(
            instance=args.instance,
            platform=args.platform,
            body=body,
            comment=args.comment,
            timeout=args.timeout,
            region=args.region,
            env=env,
            output_bucket=args.output_s3_bucket,
            output_prefix=args.output_s3_prefix,
            runner=runner,
            sleep=sleep,
        )
    except common.ExpiredTokenError as exc:
        sys.stderr.write(f"ssm_run: {exc}\n")
        return common.EXIT_EXPIRED_TOKEN
    if res is not None:
        Path(args.out).write_text(json.dumps(res, indent=1))
        sys.stdout.write(format_report(res))
    return code


if __name__ == "__main__":
    sys.exit(main())
