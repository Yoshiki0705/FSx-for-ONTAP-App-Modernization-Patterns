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
PAIR_ROLES = {
    "file-locking": ("holder", "contender"),
    "write-visibility": ("writer", "reader"),
}
WAIT_S = (
    180.0  # longest wait for the other host's signal (SSM and module start-up included)
)
POLL_S = 0.1  # write-visibility poll interval (design: 100 ms)
VISIBILITY_CAP_S = (
    60.0  # write-visibility upper bound after the writer's save (design: 60 s)
)


class ConfigError(ValueError):
    """Invalid probe configuration (exit 2)."""


def parse_config(argv: list[str]) -> dict:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--store")
    parser.add_argument("--root")
    parser.add_argument("--stage")
    parser.add_argument("--role")
    parser.add_argument("--run-id", dest="run_id")
    parser.add_argument("--pair-behavior", dest="pair_behavior")
    parser.add_argument("--sync-id", dest="sync_id")
    parser.add_argument("--sync-dir", dest="sync_dir")
    parser.add_argument("--ntp-offset-ms", dest="ntp_offset_ms")
    args, _ = parser.parse_known_args(argv)
    config = {
        "store": args.store,
        "root": args.root,
        "stage": args.stage,
        "role": args.role,
        "run_id": args.run_id,
        "pair_behavior": args.pair_behavior,
        "sync_id": args.sync_id,
        "sync_dir": args.sync_dir,
        "ntp_offset_ms": None,
    }
    if args.ntp_offset_ms not in (None, ""):
        try:
            config["ntp_offset_ms"] = float(args.ntp_offset_ms)
        except ValueError as exc:
            raise ConfigError("--ntp-offset-ms must be a number") from exc
    if config["pair_behavior"] is not None:
        roles = PAIR_ROLES.get(config["pair_behavior"])
        if roles is None:
            raise ConfigError(f"unknown pair behavior: {config['pair_behavior']}")
        if args.role not in roles:
            raise ConfigError(
                f"role for {config['pair_behavior']} must be one of {roles}"
            )
        for key in ("sync_id", "sync_dir"):
            if not config[key]:
                raise ConfigError(
                    f"--{key.replace('_', '-')} is required with --pair-behavior"
                )
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


# --- coordinated two-client behaviors ------------------------------------------------------------
#
# The two hosts synchronize through a channel that is NOT the volume under test, so the barrier
# cannot absorb the visibility being measured. This process talks to a local directory only:
# <sync-dir>/out/<name> is a signal this side raises, <sync-dir>/in/<name> is one the other side
# raised. The launcher (probe-launch.sh / probe-launch.ps1) bridges the two directories through the
# artifacts S3 bucket. Each signal is a small JSON object written to a temp name and renamed.


def _now_ms() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SyncChannel:
    def __init__(self, sync_dir: str) -> None:
        self.out = Path(sync_dir) / "out"
        self.inbox = Path(sync_dir) / "in"
        self.out.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def signal(self, name: str, payload: dict) -> None:
        tmp = self.out / f"{name}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.out / name)

    def peek(self, name: str) -> dict | None:
        path = self.inbox / name
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def wait(self, name: str, timeout_s: float = WAIT_S) -> dict:
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            got = self.peek(name)
            if got is not None:
                return got
            time.sleep(0.05)
        raise TimeoutError(
            f"no '{name}' signal from the other host within {timeout_s:.0f} s"
        )


def _attempt(fn) -> dict:
    try:
        fn()
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__, "errno": exc.errno}


def pair_dir(config: dict) -> Path:
    return (
        Path(config["root"]) / "probe" / config["run_id"] / "pairs" / config["sync_id"]
    )


def lock_holder(config: dict, chan: SyncChannel) -> dict:
    # POSIX record lock (fcntl.lockf), which the Linux NFSv4 and cifs clients send to the server;
    # flock() would be local-only on some mounts and prove nothing about the other host.
    directory = pair_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "lock.dat"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX)
        os.write(fd, b"h")
        os.fsync(fd)
        acquired = _now_ms()
        chan.signal("lock-acquired", {"at": acquired})
        chan.wait("attempt-done")
        fcntl.lockf(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    released = _now_ms()
    chan.signal("released", {"at": released})
    return {
        "lock": "fcntl.lockf LOCK_EX",
        "lock_acquired_at": acquired,
        "lock_released_at": released,
    }


def lock_contender(config: dict, chan: SyncChannel) -> dict:
    chan.wait("lock-acquired")
    path = pair_dir(config) / "lock.dat"
    started = _now_ms()
    attempts: dict = {}
    holder: dict = {}

    def open_read() -> None:
        holder["r"] = os.open(path, os.O_RDONLY)

    def read_byte() -> None:
        os.read(holder["r"], 1)

    def open_write() -> None:
        holder["w"] = os.open(path, os.O_WRONLY)

    def lock_nb() -> None:
        fcntl.lockf(holder["w"], fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.lockf(holder["w"], fcntl.LOCK_UN)

    attempts["open_read"] = _attempt(open_read)
    if "r" in holder:
        attempts["read"] = _attempt(read_byte)
    attempts["open_write"] = _attempt(open_write)
    if "w" in holder:
        attempts["lock"] = _attempt(lock_nb)
    for fd in holder.values():
        os.close(fd)
    ended = _now_ms()
    chan.signal("attempt-done", {"at": ended})
    chan.wait("released")

    def relock() -> None:
        fd = os.open(path, os.O_WRONLY)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    return {
        "attempt_started_at": started,
        "attempt_ended_at": ended,
        "attempts": attempts,
        "after_release": _attempt(relock),
    }


def vis_writer(config: dict, chan: SyncChannel) -> dict:
    chan.wait("ready")
    directory = pair_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    started = _now_ms()
    fd = os.open(
        directory / "visible.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666
    )
    try:
        os.write(fd, config["sync_id"].encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    saved = _now_ms()
    chan.signal("saved", {"write_started_at": started, "save_completed_at": saved})
    return {"write_started_at": started, "save_completed_at": saved}


def vis_reader(config: dict, chan: SyncChannel) -> dict:
    import time

    directory = pair_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "visible.txt"
    absent_before = not target.exists()
    ready = _now_ms()
    chan.signal("ready", {"at": ready})
    want = config["sync_id"]
    first_listed = first_read = None
    polls = 0
    hard_stop = time.monotonic() + WAIT_S + VISIBILITY_CAP_S
    stop_at = None
    last_poll = ready
    while time.monotonic() < hard_stop:
        polls += 1
        last_poll = _now_ms()
        if first_listed is None:
            try:
                if target.name in os.listdir(directory):
                    first_listed = last_poll
            except OSError:
                pass
        try:
            if target.read_text(encoding="utf-8") == want:
                first_read = _now_ms()
                break
        except OSError:
            pass
        if stop_at is None:
            saved = chan.peek("saved")
            if saved is not None:
                saved_at = dt.datetime.fromisoformat(
                    saved["save_completed_at"].replace("Z", "+00:00")
                )
                remaining = (
                    saved_at
                    + dt.timedelta(seconds=VISIBILITY_CAP_S)
                    - dt.datetime.now(dt.timezone.utc)
                ).total_seconds()
                stop_at = time.monotonic() + max(remaining, 0.0)
        if stop_at is not None and time.monotonic() >= stop_at:
            break
        time.sleep(POLL_S)
    return {
        "absent_before": absent_before,
        "ready_at": ready,
        "seen": first_read is not None,
        "first_listed_at": first_listed,
        "first_read_ok_at": first_read,
        "last_poll_at": last_poll,
        "polls": polls,
        "poll_interval_ms": int(POLL_S * 1000),
    }


PAIR_BODIES = {
    ("file-locking", "holder"): lock_holder,
    ("file-locking", "contender"): lock_contender,
    ("write-visibility", "writer"): vis_writer,
    ("write-visibility", "reader"): vis_reader,
}


def run_pair(config: dict) -> dict:
    behavior = config["pair_behavior"]
    chan = SyncChannel(config["sync_dir"])
    body = PAIR_BODIES[(behavior, config["role"])]

    def observed() -> dict:
        sync = body(config, chan)
        sync.update({"sync_id": config["sync_id"], "role": config["role"]})
        # No topology here: only the merge can prove cross-host, from both sides' timelines.
        return {"topology": "pending-merge", "sync": sync}

    record = measure(behavior, observed)
    if record["outcome"] == "error":
        record["observed"] = {
            "topology": "pending-merge",
            "sync": {"sync_id": config["sync_id"], "role": config["role"]},
        }
    return {
        "schema": "appmod-probe/1",
        "run_id": config["run_id"],
        "started_at": _now(),
        "stage": config["stage"],
        "role": config["role"],
        "host": _host(config),
        "store": {"kind": config["store"], "root": config["root"]},
        "behaviors": [record],
    }


def _host(config: dict) -> dict:
    import socket

    return {
        "os": "linux",
        "name": socket.gethostname(),
        "runtime": "python3 (probe_peer.py)",
        "ntp_offset_ms": config.get("ntp_offset_ms"),
    }


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


def _pair_selftest() -> list[str]:
    """Run both roles of each pair behavior in threads, bridged by a thread that copies signals
    between two sync directories (the launcher's S3 bridge, without S3), and check the timeline."""
    failures: list[str] = []
    for behavior, (first, second) in PAIR_ROLES.items():
        failures.extend(_pair_selftest_one(behavior, first, second))
    return failures


def _pair_selftest_one(behavior: str, first: str, second: str) -> list[str]:
    import shutil
    import tempfile
    import threading
    import time

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        results: dict = {}
        stop = threading.Event()

        def bridge(a: Path, b: Path) -> None:
            while not stop.is_set():
                for src, dst in ((a / "out", b / "in"), (b / "out", a / "in")):
                    if src.is_dir():
                        for f in src.iterdir():
                            if f.suffix != ".tmp" and not (dst / f.name).exists():
                                dst.mkdir(parents=True, exist_ok=True)
                                shutil.copy(f, dst / f"{f.name}.tmp")
                                os.replace(dst / f"{f.name}.tmp", dst / f.name)
                time.sleep(0.02)

        def side(role: str) -> None:
            config = {
                "store": "nfs",
                "root": str(base / "vol"),
                "stage": 1,
                "role": role,
                "run_id": "s1-selftest",
                "pair_behavior": behavior,
                "sync_id": "sync-1",
                "sync_dir": str(base / role),
                "ntp_offset_ms": 0.1,
            }
            results[role] = run_pair(config)["behaviors"][0]

        (base / first).mkdir()
        (base / second).mkdir()
        t_bridge = threading.Thread(target=bridge, args=(base / first, base / second))
        t_bridge.start()
        threads = [threading.Thread(target=side, args=(r,)) for r in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        stop.set()
        t_bridge.join()
        for role in (first, second):
            rec = results.get(role) or {}
            if rec.get("outcome") != "measured":
                failures.append(
                    f"{behavior}/{role}: outcome {rec.get('outcome')} {rec.get('error_type')}"
                )
        if failures:
            return failures
        s1 = results[first]["observed"]["sync"]
        s2 = results[second]["observed"]["sync"]
        if behavior == "file-locking":
            if not (
                s1["lock_acquired_at"]
                <= s2["attempt_started_at"]
                <= s2["attempt_ended_at"]
                <= s1["lock_released_at"]
            ):
                failures.append(
                    "file-locking: the attempt is not inside the lock interval"
                )
        elif not (s2["ready_at"] <= s1["write_started_at"] and s2["seen"]):
            failures.append(
                "write-visibility: the reader did not see the write after being ready"
            )
    return failures


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

    failures.extend(_pair_selftest())

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
    if config["pair_behavior"]:
        print(json.dumps(run_pair(config)))
        return 0
    print(json.dumps(run(config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
