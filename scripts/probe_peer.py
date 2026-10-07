#!/usr/bin/env python3
"""Linux-side peer of DocIntake.Probe (stdlib only).

.NET Framework 4.8 does not run on Linux, so the Linux side of the five behaviors is measured here
and emitted in the same JSON shape (schema appmod-probe/1). It operates on a given mount: stage 0
uses the SMB mount, stage 1 and later add the NFS mount. From stage 2 the migrated .NET Probe takes
over the Linux side and this script stays only for comparison with stages 0 and 1.

Config is validated strictly: a missing required option or an unknown store kind exits 2 without
measuring (mirrors DocIntake.Probe). Each behavior runs independently; a failure is recorded with
outcome "error" and the exception type, and the others continue.

  probe_peer.py --store nfs --root /mnt/appdata --stage 1 --role writer --run-id s1-...
  probe_peer.py --selftest     prove config validation and that a behavior error is isolated
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import uuid
from pathlib import Path

KNOWN_STORES = {"smb", "nfs", "s3"}
REQUIRED = ("store", "root", "stage", "role", "run_id")


class ConfigError(ValueError):
    """Invalid probe configuration (exit 2)."""


def parse_config(argv: list[str]) -> dict:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--store")
    parser.add_argument("--root")
    parser.add_argument("--stage")
    parser.add_argument("--role")
    parser.add_argument("--run-id", dest="run_id")
    args, _ = parser.parse_known_args(argv)
    config = {
        "store": args.store,
        "root": args.root,
        "stage": args.stage,
        "role": args.role,
        "run_id": args.run_id,
    }
    for key in REQUIRED:
        if config[key] in (None, ""):
            raise ConfigError(f"missing required option: --{key.replace('_', '-')}")
    if config["store"] not in KNOWN_STORES:
        raise ConfigError(f"unknown store kind: {config['store']}")
    try:
        stage = int(config["stage"])
    except (TypeError, ValueError) as exc:
        raise ConfigError("stage must be an integer 0..3") from exc
    if not 0 <= stage <= 3:
        raise ConfigError("stage must be 0..3")
    config["stage"] = stage
    return config


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def measure(behavior_id: str, body) -> dict:
    started = _now()
    try:
        observed = body()
        observed.setdefault("topology", "cross-host")
        return {
            "id": behavior_id,
            "started_at": started,
            "outcome": "measured",
            "observed": observed,
            "error_type": None,
        }
    except Exception as exc:  # noqa: BLE001 - a failed behavior must not stop the others
        return {
            "id": behavior_id,
            "started_at": started,
            "outcome": "error",
            "observed": {"topology": "cross-host"},
            "error_type": type(exc).__name__,
        }


def case_sensitivity(root: Path) -> dict:
    # Create two files that differ only in case and report whether both exist as distinct files.
    base = root
    base.mkdir(parents=True, exist_ok=True)
    lower = base / "casecheck.txt"
    upper = base / "CaseCheck.txt"
    lower.write_text("l", encoding="utf-8")
    upper.write_text("u", encoding="utf-8")
    distinct = (
        lower.exists() and upper.exists() and lower.read_text() != upper.read_text()
    )
    return {"distinct_case_files": bool(distinct)}


def path_separator(root: Path) -> dict:
    # Write a name containing a backslash and report the resulting file name on this filesystem.
    # When the OS/protocol rejects the name, that rejection IS the observation: record it as a
    # measured result (rejected=true, the attempted name, the errno), not as outcome=error. error is
    # reserved for the probe failing to observe anything at all.
    base = root
    base.mkdir(parents=True, exist_ok=True)
    name = "sep\\check.txt"
    target = base / name
    try:
        target.write_text("x", encoding="utf-8")
    except OSError as exc:
        return {
            "attempted_name": name,
            "rejected": True,
            "exception_type": type(exc).__name__,
            "errno": exc.errno,
        }
    return {
        "attempted_name": name,
        "rejected": False,
        "written_name": name,
        "exists_literal": (base / name).exists(),
    }


def file_locking(root: Path) -> dict:
    # Acquire an advisory exclusive lock with fcntl and report acquisition. The contender is a
    # separate process coordinated by run-probe.sh.
    base = root
    base.mkdir(parents=True, exist_ok=True)
    path = base / "lockcheck.txt"
    with path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {"exclusive_lock_acquired": acquired}


def acl_evaluation(root: Path) -> dict:
    # On Linux there is no GetAccessControl-style pre-check; record skipped-equivalent by reporting
    # the POSIX mode and that a pre-check is not available, so the pre-check vs I/O mismatch the
    # NTFS side records has no counterpart here.
    base = root
    base.mkdir(parents=True, exist_ok=True)
    path = base / "aclcheck.txt"
    path.write_text("a", encoding="utf-8")
    mode = oct(os.stat(path).st_mode & 0o777)
    return {"posix_mode": mode, "precheck_available": False}


def write_visibility(root: Path) -> dict:
    base = root
    base.mkdir(parents=True, exist_ok=True)
    marker = base / f"vis-{uuid.uuid4().hex}.txt"
    marker.write_text("v", encoding="utf-8")
    return {"marker": marker.name}


def work_dir(config: dict) -> Path:
    """probe/<run_id>/<store>/ under the mount: the write area the design fixes for the Probe.

    Per run, because a fixed probe/ directory carries the previous run's files into the next
    (case-sensitivity creates a name the next run then finds). Per store, because from stage 1 the
    SMB and NFS sides run concurrently on one host and would otherwise write the same names.
    """
    return Path(config["root"]) / "probe" / config["run_id"] / config["store"]


def run(config: dict) -> dict:
    root = work_dir(config)
    behaviors = [
        measure("case-sensitivity", lambda: case_sensitivity(root)),
        measure("path-separator", lambda: path_separator(root)),
        measure("file-locking", lambda: file_locking(root)),
        measure("acl-evaluation", lambda: acl_evaluation(root)),
        measure("write-visibility", lambda: write_visibility(root)),
    ]
    return {
        "schema": "appmod-probe/1",
        "run_id": config["run_id"],
        "started_at": _now(),
        "stage": config["stage"],
        "role": config["role"],
        "host": {"os": "linux", "runtime": "python3 (probe_peer.py)"},
        "store": {
            "kind": config["store"],
            "root": config["root"],
            "work_dir": str(root),
        },
        "behaviors": behaviors,
    }


def selftest() -> int:
    import tempfile

    failures = []
    # Missing key and unknown store both raise ConfigError.
    for argv, label in [
        (
            ["--store", "nfs", "--root", "/x", "--stage", "1", "--role", "writer"],
            "missing run-id",
        ),
        (
            [
                "--store",
                "bad",
                "--root",
                "/x",
                "--stage",
                "1",
                "--role",
                "w",
                "--run-id",
                "r",
            ],
            "unknown store",
        ),
        (
            [
                "--store",
                "nfs",
                "--root",
                "/x",
                "--stage",
                "9",
                "--role",
                "w",
                "--run-id",
                "r",
            ],
            "stage out of range",
        ),
    ]:
        try:
            parse_config(argv)
            failures.append(f"{label}: expected ConfigError")
        except ConfigError:
            pass

    # A behavior whose body throws is isolated as outcome error; others still measured.
    with tempfile.TemporaryDirectory() as directory:
        config = {
            "store": "nfs",
            "root": directory,
            "stage": 1,
            "role": "writer",
            "run_id": "s1-selftest",
        }
        result = run(config)
        outcomes = {b["id"]: b["outcome"] for b in result["behaviors"]}
        if len(outcomes) != 5:
            failures.append("expected 5 behaviors")
        if any(
            b["observed"].get("topology") != "cross-host" for b in result["behaviors"]
        ):
            failures.append("every behavior must carry topology cross-host")

        def boom():
            raise RuntimeError("planted")

        bad = measure("case-sensitivity", boom)
        if bad["outcome"] != "error" or bad["error_type"] != "RuntimeError":
            failures.append(
                "a throwing behavior must be recorded as error with its type"
            )

    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("selftest: config validation and behavior isolation hold")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    try:
        config = parse_config(argv)
    except ConfigError as exc:
        print(f"probe_peer: invalid configuration: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(run(config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
