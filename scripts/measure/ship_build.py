#!/usr/bin/env python3
"""Ship a published build to a Linux host over SSM Run Command, sending only what differs.

A self-contained linux-x64 publish is about 81 MB (one observation, SDK 10.0.400). Sent through
Run Command in 24,000-character base64 chunks that is roughly 4,500 calls, so this driver is
usable when the host already holds an earlier build of the same runtime (--base): it lists the
host directory's sha256 prefixes, sends the files that differ, and the host script
host/install-build.sh assembles a NEW directory (base copy plus delta) and verifies every file
against the full list. Without --base everything is sent, and the run is refused when the chunk
count exceeds --max-chunks.

Never deletes anything on the host outside the new directory: install-build.sh refuses a
destination that already exists, and --prune-extra only removes files inside that new directory
which are not in the published list (for example a Probe binary copied along with a Worker base).

--component NAME=DIR is repeatable. Each component lands in <dest-dir>/NAME. --base NAME=HOSTDIR
names the host directory copied to <dest-dir>/NAME before the delta is applied.

Exit codes: 0 done; 1 a remote step ended in a failed status; 2 bad argument; 3 chunk count above
--max-chunks; 20 the AWS token has expired (run `aws sso login` yourself).

Example:
  ship_build.py --instance i-0123456789abcdef0 --run-id s2m-20261012T090934Z \\
      --component probe=./publish/probe --component worker=./publish/worker \\
      --base probe=/opt/appmod/earlier/probe --base worker=/opt/appmod/earlier/probe \\
      --dest-dir /opt/appmod/modern --executable probe/DocIntake.Probe \\
      --executable worker/DocIntake.Worker --prune-extra
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common
import ssm_run

HERE = Path(__file__).resolve().parent
CHUNK_CHARS = 24000
DEFAULT_MAX_CHUNKS = 200
EXIT_TOO_MANY_CHUNKS = 3
SKIP_NAMES = (".DS_Store",)
HASH_PREFIX = 16


def payload_b64_path(run_id: str) -> str:
    """Where the chunks are appended on the host. install-build.sh derives the same name."""
    return f"/tmp/appmod-ship-{run_id}-payload.b64"


def parse_pairs(items: Sequence[str], what: str) -> dict[str, str]:
    """NAME=VALUE items into a dict; names are validated as component names."""
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"invalid {what} {item!r}: expected NAME=VALUE")
        name, value = item.split("=", 1)
        common.validate_component(name)
        if name in out:
            raise ValueError(f"duplicate {what} for {name}")
        out[name] = value
    return out


def local_list(root: Path) -> dict[str, str]:
    """{'./rel/path': sha256-hex} for every regular file under root.

    AppleDouble files (._*) and .DS_Store are skipped; a symlink is an error because the host
    verification compares regular files only.
    """
    result: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if name.startswith("._") or name in SKIP_NAMES:
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                raise ValueError(f"symlink in published directory: {full}")
            rel = "./" + full.relative_to(root).as_posix()
            result[rel] = hashlib.sha256(full.read_bytes()).hexdigest()
    return result


def format_sha_list(listing: dict[str, str]) -> str:
    return "".join(f"{h}  {p}\n" for p, h in sorted(listing.items()))


def parse_sha_list(text: str) -> dict[str, str]:
    """Parse `sha256sum`-style `<hash>  <path>` lines."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        h, sep, p = line.partition("  ")
        if not sep:
            raise ValueError(f"malformed sha list line: {line!r}")
        out[p] = h
    return out


def parse_host_hashes(text: str) -> dict[str, dict[str, str]]:
    """Parse host-hashes.sh output: `===<dir>` then `<16-hex>  ./path` lines per directory."""
    out: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        line = line.rstrip("\n")
        if line.startswith("==="):
            current = out.setdefault(line[3:], {})
        elif current is not None and "  ./" in line:
            h, p = line.split("  ", 1)
            current[p] = h
    return out


def compute_delta(local: dict[str, str], host: dict[str, str] | None) -> list[str]:
    """Paths whose first 16 hex digits differ from the host's, or that the host lacks.

    host=None means "no base": every file is sent.
    """
    if host is None:
        return sorted(local)
    return sorted(p for p in local if host.get(p, "") != local[p][:HASH_PREFIX])


def chunk_b64(data: bytes, size: int = CHUNK_CHARS) -> list[str]:
    b64 = base64.b64encode(data).decode()
    return [b64[i : i + size] for i in range(0, len(b64), size)]


def payload_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def init_script(run_id: str) -> str:
    path = payload_b64_path(run_id)
    return f"#!/bin/bash\nrm -f {path}\n: > {path}\necho init-ok\n"


def chunk_script(run_id: str, index: int, chunk: str) -> str:
    path = payload_b64_path(run_id)
    return f"#!/bin/bash\nprintf '%s' '{chunk}' >> {path}\necho chunk-{index}-ok\n"


def validate_executables(items: Sequence[str], components: Sequence[str]) -> list[str]:
    out = []
    for item in items:
        common.validate_relpath(item)
        if item.split("/", 1)[0] not in components:
            raise ValueError(
                f"--executable {item!r} does not start with a --component name"
            )
        out.append(item)
    return out


def install_env(
    *,
    sha: str,
    dest_dir: str,
    run_id: str,
    components: Sequence[str],
    bases: dict[str, str],
    executables: Sequence[str],
    prune_extra: bool,
) -> dict[str, str]:
    env = {
        "APPMOD_PAYLOAD_SHA256": sha,
        "APPMOD_DEST_DIR": dest_dir,
        "APPMOD_RUN_ID": run_id,
        "APPMOD_COMPONENTS": ",".join(components),
    }
    if bases:
        env["APPMOD_BASES"] = ",".join(f"{k}={v}" for k, v in sorted(bases.items()))
    if executables:
        env["APPMOD_EXECUTABLES"] = ",".join(executables)
    if prune_extra:
        env["APPMOD_PRUNE_EXTRA"] = "1"
    # Every value must pass the same shape check ssm_run applies to --env.
    for name, value in env.items():
        common.validate_env_item(f"{name}={value}")
    return env


def make_tarball(stage: Path, tgz: Path, runner: Callable = subprocess.run) -> bytes:
    """tar+gzip the stage directory. COPYFILE_DISABLE keeps macOS from adding ._* entries."""
    runner(
        ["tar", "-C", str(stage), "-czf", str(tgz), "."],
        check=True,
        env=dict(os.environ, COPYFILE_DISABLE="1"),
    )
    return tgz.read_bytes()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Environment: APPMOD_REGION, APPMOD_LNX_INSTANCE (default for --instance), "
        "APPMOD_DRY_RUN.",
    )
    ap.add_argument(
        "--instance",
        default=os.environ.get("APPMOD_LNX_INSTANCE"),
        help="Linux host instance id (default: $APPMOD_LNX_INSTANCE)",
    )
    ap.add_argument("--run-id", required=True, help="run id, e.g. s2m-YYYYMMDDTHHMMSSZ")
    ap.add_argument(
        "--component",
        action="append",
        default=[],
        metavar="NAME=DIR",
        required=True,
        help="published directory to ship as <dest-dir>/NAME (repeatable)",
    )
    ap.add_argument(
        "--base",
        action="append",
        default=[],
        metavar="NAME=HOSTDIR",
        help="host directory copied to <dest-dir>/NAME first; only the delta is sent",
    )
    ap.add_argument(
        "--dest-dir",
        required=True,
        help="NEW host directory under /opt/appmod/; must not exist",
    )
    ap.add_argument(
        "--executable",
        action="append",
        default=[],
        metavar="NAME/REL",
        help="file under <dest-dir> to chmod a+x (repeatable)",
    )
    ap.add_argument(
        "--prune-extra",
        action="store_true",
        help="remove files inside the new directory that are not in the published list",
    )
    ap.add_argument("--max-chunks", type=int, default=DEFAULT_MAX_CHUNKS)
    ap.add_argument(
        "--out-dir", help="where results are saved (default: .private/runs/<run-id>)"
    )
    ap.add_argument(
        "--region", default=os.environ.get("APPMOD_REGION", common.DEFAULT_REGION)
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="compute the payload and print the plan; make no AWS call (the host delta is not "
        "read, so the plan is the full-ship upper bound)",
    )
    return ap


def main(
    argv: Sequence[str] | None = None,
    runner: Callable = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    tar_runner: Callable = subprocess.run,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.instance:
            raise ValueError("--instance (or APPMOD_LNX_INSTANCE) is required")
        common.validate_instance_id(args.instance)
        common.validate_run_id(args.run_id)
        common.validate_region(args.region)
        common.validate_dest_dir(args.dest_dir)
        components_map = parse_pairs(args.component, "--component")
        bases = parse_pairs(args.base, "--base")
        for name, base in bases.items():
            if name not in components_map:
                raise ValueError(f"--base {name} has no matching --component")
            common.validate_dest_dir(base)
        executables = validate_executables(args.executable, list(components_map))
        if args.max_chunks < 1:
            raise ValueError("--max-chunks must be at least 1")
        roots = {}
        for name, d in components_map.items():
            root = Path(d)
            if not root.is_dir():
                raise ValueError(f"--component {name}: not a directory: {d}")
            roots[name] = root
    except ValueError as exc:
        sys.stderr.write(f"ship_build: {exc}\n")
        return common.EXIT_BAD_ARGUMENT

    out_dir = Path(args.out_dir or f".private/runs/{args.run_id}")
    components = list(components_map)

    try:
        locals_ = {name: local_list(root) for name, root in roots.items()}
    except ValueError as exc:
        sys.stderr.write(f"ship_build: {exc}\n")
        return common.EXIT_BAD_ARGUMENT

    dry = args.dry_run or bool(os.environ.get("APPMOD_DRY_RUN"))

    def remote(
        body: str,
        name: str,
        comment: str,
        env: dict[str, str] | None = None,
        show: bool = True,
    ) -> int:
        code, res = ssm_run.run_script(
            instance=args.instance,
            platform="linux",
            body=body.encode(),
            comment=comment,
            timeout=600,
            region=args.region,
            env=env,
            runner=runner,
            sleep=sleep,
        )
        if res is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"ship-{name}.json").write_text(json.dumps(res, indent=1))
            if show or code != 0:
                sys.stdout.write(ssm_run.format_report(res))
        return code

    host_by_comp: dict[str, dict[str, str] | None] = {name: None for name in components}
    try:
        if bases and not dry:
            hashes_script = (HERE / "host" / "host-hashes.sh").read_text()
            code, res = ssm_run.run_script(
                instance=args.instance,
                platform="linux",
                body=hashes_script.encode(),
                comment=f"{args.run_id} host hashes",
                timeout=600,
                region=args.region,
                env={"APPMOD_HASH_DIRS": ",".join(sorted(set(bases.values())))},
                runner=runner,
                sleep=sleep,
            )
            if code != 0 or res is None:
                sys.stderr.write("ship_build: host-hashes step failed\n")
                return code or common.EXIT_REMOTE_FAILED
            parsed = parse_host_hashes(res["StandardOutputContent"] or "")
            for name, base in bases.items():
                host_by_comp[name] = parsed.get(base, {})

        delta = {n: compute_delta(locals_[n], host_by_comp[n]) for n in components}

        with tempfile.TemporaryDirectory(prefix="appmod-ship-") as tmp:
            stage = Path(tmp) / "stage"
            for name in components:
                for rel in delta[name]:
                    dst = stage / name / rel[2:]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(roots[name] / rel[2:], dst)
                (stage / name).mkdir(parents=True, exist_ok=True)
                (stage / f"{name}.sha256").write_text(format_sha_list(locals_[name]))
            data = make_tarball(stage, Path(tmp) / "payload.tgz", runner=tar_runner)

        sha = payload_sha(data)
        chunks = chunk_b64(data)
        plan = {
            "run_id": args.run_id,
            "dest_dir": args.dest_dir,
            "delta_files": {n: len(v) for n, v in delta.items()},
            "listed_files": {n: len(v) for n, v in locals_.items()},
            "payload_bytes": len(data),
            "payload_sha256": sha,
            "chunks": len(chunks),
            "delta_read_from_host": bool(bases) and not dry,
        }
        print(json.dumps(plan, indent=1))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "ship-delta.json").write_text(json.dumps(delta, indent=1))
        if len(chunks) > args.max_chunks:
            sys.stderr.write(
                f"ship_build: {len(chunks)} chunks exceeds --max-chunks {args.max_chunks}; "
                "pass --base so only the delta is sent, or use the artifacts-bucket route "
                "described in docs/*/reproduce.md\n"
            )
            return EXIT_TOO_MANY_CHUNKS
        if dry:
            return common.EXIT_OK

        env = install_env(
            sha=sha,
            dest_dir=args.dest_dir,
            run_id=args.run_id,
            components=components,
            bases=bases,
            executables=executables,
            prune_extra=args.prune_extra,
        )
        steps: list[tuple[str, str, str, dict[str, str] | None, bool]] = [
            (
                init_script(args.run_id),
                "up-init",
                f"{args.run_id} payload init",
                None,
                False,
            )
        ]
        for i, chunk in enumerate(chunks):
            steps.append(
                (
                    chunk_script(args.run_id, i, chunk),
                    f"up-{i:02d}",
                    f"{args.run_id} payload chunk {i + 1}/{len(chunks)}",
                    None,
                    False,
                )
            )
        install_body = (HERE / "host" / "install-build.sh").read_text()
        steps.append(
            (install_body, "install", f"{args.run_id} install build", env, True)
        )
        for body, name, comment, step_env, show in steps:
            code = remote(body, name, comment, step_env, show)
            if code != 0:
                sys.stderr.write(
                    f"ship_build: remote step {name} failed (exit {code})\n"
                )
                return code
            if not show:
                print(f"{name} ok")
    except common.ExpiredTokenError as exc:
        sys.stderr.write(f"ship_build: {exc}\n")
        return common.EXIT_EXPIRED_TOKEN
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"ship_build: {exc}\n")
        return common.EXIT_BAD_ARGUMENT
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
