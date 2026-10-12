#!/usr/bin/env python3
"""Run the cross-host Probe measurements through SSM Run Command, one command per side.

Reproduces the mechanism of scripts/run-probe.sh for a run where the Probe on one or both hosts is
not the default one: the Windows launcher gets -ProbeExe, and the Linux launcher gets
APPMOD_PROBE_BIN (the migrated Probe binary instead of probe_peer.py). Two modes:

  pairs.py windows-standalone      the Windows standalone Probe, saved as <out-dir>/windows.json
  pairs.py pairs [N ...]           the coordinated pairs (default all that the stage allows), one
                                   after the other; stops at the first problem

Both sides of a pair are started before either is collected, because the pair synchronizes through
the artifacts bucket (probe/<run-id>/sync/<sync-id>/...), never through the volume under test.

Safety: no automatic retry; the first problem (a status other than Success, no JSON record, an
outcome of "error", or a case-sensitivity record whose positive control did not resolve) stops the
run with exit 1 and leaves what was collected. An output file that already exists is never
overwritten. The bucket name is read from --bucket / APPMOD_ARTIFACTS_BUCKET or, if absent, from the
CloudFormation output of --stack-name, and is kept in memory; saved command text has it replaced by
ARTIFACTS_BUCKET and IPv4 addresses and EC2 hostnames scrubbed. Deletes and moves nothing.

Exit codes: 0 done; 1 a problem was found; 2 bad argument or an output file exists; 20 AWS token
expired (run `aws sso login` yourself); 21 bucket lookup failed; 22 send-command failed;
23 get-command-invocation failed.

Example (placeholders; the pairs are merged afterwards with the command this script prints):
  pairs.py --run-id s2m-20261012T090934Z --stage 2 --windows-instance i-0123456789abcdef0 \\
      --linux-instance i-00000000000000000 --bucket example-artifacts-bucket \\
      --linux-probe-bin /opt/appmod/modern/probe/DocIntake.Probe pairs 1 2
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

# (number, behavior, side 1, role 1, side 2, role 2). scripts/run-probe.sh holds the same table;
# scripts/tests/test_measure_pairs.py compares the two so they cannot drift.
PAIRS: list[tuple[int, str, str, str, str, str]] = [
    (1, "file-locking", "windows", "holder", "linux-smb", "contender"),
    (2, "file-locking", "linux-smb", "holder", "windows", "contender"),
    (3, "write-visibility", "windows", "writer", "linux-smb", "reader"),
    (4, "write-visibility", "linux-smb", "writer", "windows", "reader"),
    (5, "file-locking", "windows", "holder", "linux-nfs", "contender"),
    (6, "file-locking", "linux-nfs", "holder", "windows", "contender"),
    (7, "write-visibility", "windows", "writer", "linux-nfs", "reader"),
    (8, "write-visibility", "linux-nfs", "writer", "windows", "reader"),
]
NFS_PAIRS_FROM_STAGE = 1
DEFAULT_LINUX_LAUNCHER = "/opt/appmod/probe-launch.sh"
DEFAULT_STACK = "appmod-base"
BUCKET_OUTPUT_KEY = "ArtifactsBucketName"
DEFAULT_SVM = "APPMODSVM01"
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9._\\-]+$")
STACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
NETBIOS_RE = re.compile(r"^[A-Za-z0-9-]{1,15}$")
PENDING = ("Pending", "InProgress", "Delayed")
EXIT_PROBLEM = 1
EXIT_BUCKET = 21
EXIT_SEND = 22
EXIT_GET = 23


class DriverError(Exception):
    """A stop with a specific exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Config:
    run_id: str
    stage: int
    region: str
    svm_netbios: str
    windows_instance: str
    linux_instance: str
    out_dir: Path
    bucket: str = ""
    windows_probe_exe: str = ""
    linux_launcher: str = DEFAULT_LINUX_LAUNCHER
    linux_probe_bin: str = ""
    timeout: int = 900


def validate_config(cfg: Config) -> None:
    common.validate_run_id(cfg.run_id)
    if not 0 <= cfg.stage <= 3:
        raise ValueError(f"invalid stage {cfg.stage}: expected 0..3")
    common.validate_region(cfg.region)
    common.validate_instance_id(cfg.windows_instance)
    common.validate_instance_id(cfg.linux_instance)
    if not NETBIOS_RE.fullmatch(cfg.svm_netbios):
        raise ValueError(f"invalid SVM NetBIOS name: {cfg.svm_netbios!r}")
    if cfg.bucket:
        common.validate_bucket(cfg.bucket)
    if cfg.windows_probe_exe and not WINDOWS_PATH_RE.fullmatch(cfg.windows_probe_exe):
        raise ValueError(
            f"invalid --windows-probe-exe {cfg.windows_probe_exe!r}: "
            "expected a drive path such as C:\\dir\\DocIntake.Probe.exe"
        )
    for name, value in (
        ("--linux-launcher", cfg.linux_launcher),
        ("--linux-probe-bin", cfg.linux_probe_bin),
    ):
        if value and (not PATH_RE.fullmatch(value) or ".." in value.split("/")):
            raise ValueError(f"invalid {name} {value!r}: expected an absolute path")
    if not 30 <= cfg.timeout <= 3600:
        raise ValueError(f"invalid timeout {cfg.timeout}: expected 30..3600")


def select_pairs(stage: int, numbers: Sequence[int]) -> list[tuple]:
    """The pairs to run, in table order. Pairs 5..8 need the NFS mount, which exists from stage 1."""
    known = {p[0] for p in PAIRS}
    unknown = sorted(set(numbers) - known)
    if unknown:
        raise ValueError(f"unknown pair number(s): {unknown}; expected 1..{len(PAIRS)}")
    if numbers:
        chosen = [p for p in PAIRS if p[0] in set(numbers)]
    else:
        chosen = list(PAIRS)
    if stage < NFS_PAIRS_FROM_STAGE:
        nfs = [p[0] for p in chosen if "linux-nfs" in (p[2], p[4])]
        if nfs and numbers:
            raise ValueError(
                f"pair(s) {nfs} use the NFS mount, which stage {stage} does not have"
            )
        chosen = [p for p in chosen if p[0] not in nfs]
    return chosen


def sync_id_for(run_id: str, n: int) -> str:
    return f"{run_id}-p{n}"


def pair_stems(
    n: int, side1: str, role1: str, side2: str, role2: str
) -> tuple[str, str]:
    return f"pair-{n}-{side1}-{role1}", f"pair-{n}-{side2}-{role2}"


def command_line(
    cfg: Config, side: str, role: str, behavior: str = "", sync_id: str = ""
) -> str:
    """The launcher command line for one side. The bucket appears only in pair commands."""
    bucket = cfg.bucket or "<artifacts-bucket>"
    if side == "windows":
        extra = ""
        if behavior:
            extra = f" -PairBehavior {behavior} -SyncId {sync_id} -Bucket {bucket}"
        exe = f" -ProbeExe {cfg.windows_probe_exe}" if cfg.windows_probe_exe else ""
        return (
            "powershell -ExecutionPolicy Bypass -File C:\\appmod\\probe-launch.ps1 "
            f"-Stage {cfg.stage} -RunId {cfg.run_id} -Role {role} "
            f"-SvmNetbios {cfg.svm_netbios} -Region {cfg.region}{exe}{extra}"
        )
    if side not in ("linux-smb", "linux-nfs"):
        raise ValueError(f"unknown side: {side!r}")
    extra = ""
    if behavior:
        extra = f" --pair-behavior {behavior} --sync-id {sync_id} --bucket {bucket}"
    prefix = f"APPMOD_PROBE_BIN={cfg.linux_probe_bin} " if cfg.linux_probe_bin else ""
    return (
        f"{prefix}bash {cfg.linux_launcher} --store {side.split('-', 1)[1]} "
        f"--stage {cfg.stage} --role {role} --run-id {cfg.run_id} "
        f"--region {cfg.region} --svm-netbios {cfg.svm_netbios}{extra}"
    )


def side_instance(cfg: Config, side: str) -> str:
    return cfg.windows_instance if side == "windows" else cfg.linux_instance


def side_document(side: str) -> str:
    return "AWS-RunPowerShellScript" if side == "windows" else "AWS-RunShellScript"


def build_request(cfg: Config, side: str, cmd: str, stem: str) -> dict:
    return {
        "InstanceIds": [side_instance(cfg, side)],
        "DocumentName": side_document(side),
        "Comment": f"appmod probe {cfg.run_id} stage {cfg.stage}"[:100],
        "Parameters": {"commands": [cmd], "executionTimeout": [str(cfg.timeout)]},
        "OutputS3BucketName": cfg.bucket,
        "OutputS3KeyPrefix": f"probe/{cfg.run_id}/{stem}",
    }


def check_record(stdout: str) -> str:
    """Empty when the record is usable; otherwise why the run must stop."""
    try:
        rec = json.loads(stdout.lstrip("\ufeff"))
    except ValueError:
        return "no JSON record"
    if not isinstance(rec, dict):
        return "no JSON record"
    problems = []
    for b in rec.get("behaviors", []):
        if b.get("outcome") == "error":
            problems.append(f"{b.get('id')} outcome=error")
        if b.get("id") == "case-sensitivity" and not (b.get("observed") or {}).get(
            "real_resolved", True
        ):
            problems.append("real_resolved false")
    return "; ".join(problems)


def manifest_line(
    n: int, behavior: str, sync_id: str, p: tuple, f1: str, f2: str
) -> str:
    _, _, s1, r1, s2, r2 = p
    cols = [str(n), behavior, sync_id, s1, r1, f"{f1}.json", s2, r2, f"{f2}.json"]
    return "\t".join(cols) + "\n"


def merge_command(cfg: Config) -> str:
    sides = "windows linux-smb" + (" linux-nfs" if cfg.stage >= 1 else "")
    out = cfg.out_dir
    return (
        f"python3 scripts/probe_merge.py --out-dir {out} --run-id {cfg.run_id} "
        f'--stage {cfg.stage} --sides "{sides}" --pairs {out / "pairs-manifest.tsv"}'
    )


def refuse_existing(cfg: Config, stems: Sequence[str]) -> None:
    for stem in stems:
        for suffix in (".json", ".cmd.txt", ".stderr.txt"):
            p = cfg.out_dir / (stem + suffix)
            if p.exists():
                raise ValueError(
                    f"{p} already exists; use a new --out-dir or remove the old run yourself"
                )


# ----- AWS access (everything below is exercised with injected doubles) -----------------------


def aws(cfg: Config, args: list[str], runner: Callable) -> tuple[int, str, str]:
    return common.run_aws(["--region", cfg.region, *args], runner)


def resolve_bucket(cfg: Config, stack: str, runner: Callable) -> str:
    rc, out, err = aws(
        cfg,
        [
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack,
            "--query",
            f"Stacks[0].Outputs[?OutputKey=='{BUCKET_OUTPUT_KEY}'].OutputValue",
            "--output",
            "text",
        ],
        runner,
    )
    if rc != 0 or not out.strip():
        raise DriverError(
            EXIT_BUCKET, err or f"no {BUCKET_OUTPUT_KEY} output on {stack}"
        )
    bucket = out.strip()
    common.validate_bucket(bucket)
    return bucket


def send(
    cfg: Config, side: str, cmd: str, stem: str, runner: Callable
) -> tuple[str, str]:
    req = build_request(cfg, side, cmd, stem)
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(req, f)
        rc, out, err = aws(
            cfg,
            [
                "ssm",
                "send-command",
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
        raise DriverError(EXIT_SEND, err)
    saved = common.scrub(
        f"{req['DocumentName']} {req['InstanceIds'][0]}\n{cmd}\n", cfg.bucket
    )
    (cfg.out_dir / f"{stem}.cmd.txt").write_text(saved)
    return out.strip(), req["InstanceIds"][0]


def collect(
    cfg: Config,
    cid: str,
    inst: str,
    stem: str,
    runner: Callable,
    sleep: Callable[[float], None],
) -> tuple[str, str]:
    inv = None
    for _ in range(cfg.timeout // 3 + 20):
        sleep(3)
        rc, out, err = aws(
            cfg,
            [
                "ssm",
                "get-command-invocation",
                "--command-id",
                cid,
                "--instance-id",
                inst,
            ],
            runner,
        )
        if rc != 0:
            if "InvocationDoesNotExist" in err:
                continue
            raise DriverError(EXIT_GET, err)
        inv = json.loads(out)
        if inv.get("Status") not in PENDING:
            break
    if inv is None:
        return "NoInvocation", ""
    status = inv.get("Status") or "Unknown"
    stdout = (inv.get("StandardOutputContent") or "").replace("\r", "")
    stderr = inv.get("StandardErrorContent") or ""
    (cfg.out_dir / f"{stem}.json").write_text(common.scrub(stdout, cfg.bucket))
    (cfg.out_dir / f"{stem}.stderr.txt").write_text(common.scrub(stderr, cfg.bucket))
    print(f"{stem} status={status} rc={inv.get('ResponseCode')}")
    return status, stdout


SendFn = Callable[[str, str, str], tuple[str, str]]
CollectFn = Callable[[str, str, str], tuple[str, str]]


def problems_for(results: Sequence[tuple[str, str, str]]) -> list[str]:
    """results: (stem, status, stdout) per side."""
    bad = []
    for stem, status, stdout in results:
        if status != "Success":
            bad.append(f"{stem} status {status}")
        else:
            why = check_record(stdout)
            if why:
                bad.append(f"{stem} {why}")
    return bad


def run_windows_standalone(cfg: Config, send_fn: SendFn, collect_fn: CollectFn) -> int:
    cid, inst = send_fn(
        "windows", command_line(cfg, "windows", "standalone"), "windows"
    )
    status, stdout = collect_fn(cid, inst, "windows")
    bad = problems_for([("windows", status, stdout)])
    if bad:
        print("STOP windows: " + "; ".join(bad))
        return EXIT_PROBLEM
    return common.EXIT_OK


def run_pairs(
    cfg: Config, numbers: Sequence[int], send_fn: SendFn, collect_fn: CollectFn
) -> int:
    for p in select_pairs(cfg.stage, numbers):
        n, behavior, s1, r1, s2, r2 = p
        sync_id = sync_id_for(cfg.run_id, n)
        f1, f2 = pair_stems(n, s1, r1, s2, r2)
        # Both sides are started before either is collected: the pair meets at a barrier.
        c1, i1 = send_fn(s1, command_line(cfg, s1, r1, behavior, sync_id), f1)
        c2, i2 = send_fn(s2, command_line(cfg, s2, r2, behavior, sync_id), f2)
        st1, o1 = collect_fn(c1, i1, f1)
        st2, o2 = collect_fn(c2, i2, f2)
        bad = problems_for([(f1, st1, o1), (f2, st2, o2)])
        if bad:
            print(f"STOP pair {n}: " + "; ".join(bad))
            return EXIT_PROBLEM
        with open(cfg.out_dir / "pairs-manifest.tsv", "a") as f:
            f.write(manifest_line(n, behavior, sync_id, p, f1, f2))
        print(f"pair {n} done")
    return common.EXIT_OK


def planned_stems(mode: str, cfg: Config, numbers: Sequence[int]) -> list[str]:
    if mode == "windows-standalone":
        return ["windows"]
    stems = []
    for n, _, s1, r1, s2, r2 in select_pairs(cfg.stage, numbers):
        stems.extend(pair_stems(n, s1, r1, s2, r2))
    return stems


def dry_run(mode: str, cfg: Config, numbers: Sequence[int]) -> int:
    """Print every command that would be sent; no AWS call, no file written."""
    if mode == "windows-standalone":
        plan = [("windows", command_line(cfg, "windows", "standalone"), "windows")]
    else:
        plan = []
        for n, behavior, s1, r1, s2, r2 in select_pairs(cfg.stage, numbers):
            sid = sync_id_for(cfg.run_id, n)
            f1, f2 = pair_stems(n, s1, r1, s2, r2)
            plan.append((s1, command_line(cfg, s1, r1, behavior, sid), f1))
            plan.append((s2, command_line(cfg, s2, r2, behavior, sid), f2))
    for side, cmd, stem in plan:
        print(f"DRY-RUN {stem}: {side_document(side)} {side_instance(cfg, side)}")
        print(common.scrub(f"  {cmd}", cfg.bucket))
    return common.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Environment: APPMOD_REGION, APPMOD_WIN_INSTANCE, APPMOD_LNX_INSTANCE, "
        "APPMOD_ARTIFACTS_BUCKET, APPMOD_SVM_NETBIOS, APPMOD_DRY_RUN (any value).",
    )
    env = os.environ.get
    ap.add_argument("--run-id", required=True, help="s<stage>[suffix]-YYYYMMDDTHHMMSSZ")
    ap.add_argument("--stage", type=int, required=True, help="0..3; pairs 5-8 need 1+")
    ap.add_argument("--windows-instance", default=env("APPMOD_WIN_INSTANCE", ""))
    ap.add_argument("--linux-instance", default=env("APPMOD_LNX_INSTANCE", ""))
    ap.add_argument(
        "--bucket",
        default=env("APPMOD_ARTIFACTS_BUCKET", ""),
        help="artifacts bucket; if absent, read from the --stack-name output",
    )
    ap.add_argument("--stack-name", default=DEFAULT_STACK, help="default appmod-base")
    ap.add_argument("--region", default=env("APPMOD_REGION", common.DEFAULT_REGION))
    ap.add_argument("--svm-netbios", default=env("APPMOD_SVM_NETBIOS", DEFAULT_SVM))
    ap.add_argument(
        "--windows-probe-exe",
        default="",
        help="Windows path passed as -ProbeExe (default: the launcher's own default)",
    )
    ap.add_argument(
        "--linux-launcher",
        default=DEFAULT_LINUX_LAUNCHER,
        help=f"launcher on the Linux host (default {DEFAULT_LINUX_LAUNCHER})",
    )
    ap.add_argument(
        "--linux-probe-bin",
        default="",
        help="migrated Probe on the Linux host, passed to the launcher as APPMOD_PROBE_BIN",
    )
    ap.add_argument(
        "--out-dir", default="", help="default .private/runs/<run-id> (as run-probe.sh)"
    )
    ap.add_argument("--timeout", type=int, default=900, help="seconds per command")
    ap.add_argument(
        "--dry-run", action="store_true", help="print commands; no AWS call"
    )
    ap.add_argument("mode", choices=["windows-standalone", "pairs"])
    ap.add_argument("numbers", nargs="*", type=int, help="pair numbers (pairs mode)")
    return ap


def main(
    argv: Sequence[str] | None = None,
    runner: Callable = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "windows-standalone" and args.numbers:
            raise ValueError("windows-standalone takes no pair numbers")
        cfg = Config(
            run_id=args.run_id,
            stage=args.stage,
            region=args.region,
            svm_netbios=args.svm_netbios,
            windows_instance=args.windows_instance,
            linux_instance=args.linux_instance,
            out_dir=Path(args.out_dir or f".private/runs/{args.run_id}"),
            bucket=args.bucket,
            windows_probe_exe=args.windows_probe_exe,
            linux_launcher=args.linux_launcher,
            linux_probe_bin=args.linux_probe_bin,
            timeout=args.timeout,
        )
        validate_config(cfg)
        if not STACK_RE.fullmatch(args.stack_name):
            raise ValueError(f"invalid --stack-name {args.stack_name!r}")
        stems = planned_stems(args.mode, cfg, args.numbers)
        if args.dry_run or os.environ.get("APPMOD_DRY_RUN"):
            return dry_run(args.mode, cfg, args.numbers)
        refuse_existing(cfg, stems)
    except ValueError as exc:
        sys.stderr.write(f"pairs: {exc}\n")
        return common.EXIT_BAD_ARGUMENT
    try:
        if not cfg.bucket:
            cfg = dataclasses.replace(
                cfg, bucket=resolve_bucket(cfg, args.stack_name, runner)
            )
        cfg.out_dir.mkdir(parents=True, exist_ok=True)

        def send_fn(side: str, cmd: str, stem: str) -> tuple[str, str]:
            return send(cfg, side, cmd, stem, runner)

        def collect_fn(cid: str, inst: str, stem: str) -> tuple[str, str]:
            return collect(cfg, cid, inst, stem, runner, sleep)

        if args.mode == "windows-standalone":
            code = run_windows_standalone(cfg, send_fn, collect_fn)
        else:
            code = run_pairs(cfg, args.numbers, send_fn, collect_fn)
    except common.ExpiredTokenError as exc:
        sys.stderr.write(f"pairs: {exc}\n")
        return common.EXIT_EXPIRED_TOKEN
    except DriverError as exc:
        sys.stderr.write(f"pairs: {exc}\n")
        return exc.code
    if code == common.EXIT_OK and args.mode == "pairs":
        print("merge with (not run here):")
        print("  " + merge_command(cfg))
    return code


if __name__ == "__main__":
    sys.exit(main())
