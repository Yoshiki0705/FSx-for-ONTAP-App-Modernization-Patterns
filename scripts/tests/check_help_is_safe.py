#!/usr/bin/env python3
"""Fail when asking a script for --help can run it (stdlib only).

Why this exists: `scripts/create-secrets.sh --help` was run only to read its usage. The script had no
argument parsing, so it ran for real and created four Secrets Manager secrets. A script that ignores
its arguments treats "show usage" as "do the work", and nothing in the other gates notices. This gate
runs every script under scripts/ with --help, -h and a bogus option, with every external tool replaced
by a stub, and fails on the first sign that the request was not answered with usage alone.

What it does. Every `*.sh`, `*.ps1` and `*.py` under <root>/scripts/ is found by walking the tree
(scripts/tests/ is excluded: harnesses, not entry points). Each is run three times with `--help`, `-h`
and `--no-such-flag`, from a fresh empty working directory, with a fresh empty HOME and TMPDIR, stdin
closed, and a child environment built from scratch (HOME, TMPDIR, PATH, PYTHONDONTWRITEBYTECODE,
LC_ALL, STUB_LOG, and a few telemetry opt-outs; nothing is inherited, so an APPMOD_DRY_RUN or AWS_*
variable in the caller cannot make an unsafe script look safe). The first PATH entries are stubs for
aws, ssh, scp, sftp, curl, wget, gh, git, gitleaks, atx, sudo, setpriv, mount and chronyc: each appends
its name and arguments to STUB_LOG and exits 99. `python3 -c` is not stubbed.

It fails when, for a script that is not in the allow-list:
  (i)   a stub was called, on any of the three runs
  (ii)  the bogus option exits 0, or exits non-zero without saying why (the output must name an
        unknown argument or show usage, so a script that only dies because its environment is unset
        does not pass by luck)
  (iii) --help or -h exits non-zero, or prints no usage / synopsis
  (iv)  a file appeared or changed in the throwaway HOME, TMPDIR or working directory, or anywhere in
        the repository tree (path, size and mtime are compared before and after, including gitignored
        paths such as .private/, which `git status` cannot see)
A script in the allow-list is still run, and only (i) and (iv) apply to it.

Coverage, so that a new script cannot be skipped silently:
  - The script set is derived by walking scripts/. When <root> is a git work tree it is cross-checked
    against `git ls-files --cached --others --exclude-standard`: a tracked script the walk did not
    reach fails.
  - Anything under scripts/ that is executable or starts with `#!` but is not .sh / .ps1 / .py has no
    runner here and fails until it is given one or moved.
  - A new script is not in the allow-list, so it gets all four checks and fails until it handles
    --help and rejects unknown options. The allow-list is only for files that are not entry points.
  - An allow-list entry that names a file not on disk fails (a stale entry hides nothing, but it hides
    the fact that the list drifted).

Allow-list (help_safety_allowlist.json, next to this file). Each entry is {"kind", "reason"}:
  lib   sourced or imported, never executed by an operator; defines functions or variables only
  hook  reads one JSON event on stdin and has a fixed contract with its caller, and may not be edited
        here (guard_irreversible_ops.py is a byte-identical copy of the upstream guard)
Adding an entry is a review decision: a script that does work is not a library.

Known limits (stated so that a pass is not read as more than it is):
  - Stubs are found through PATH. A script that calls /usr/bin/curl by absolute path bypasses them;
    check (iv) and the dry-run tests are what remain.
  - Only the three argument shapes above are tried. A script that acts on `--help` only in
    combination with other arguments, or that reads stdin or an environment variable before parsing,
    is outside this check.
  - PowerShell scripts run under pwsh 7 here; the hosts run Windows PowerShell 5.1. A missing pwsh is
    an error, not a skip.

Run from the repository root:
  python3 scripts/tests/check_help_is_safe.py
  python3 scripts/tests/check_help_is_safe.py --root <dir-with-scripts/> [--only scripts/x.sh ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_ALLOWLIST = HERE / "help_safety_allowlist.json"

SUFFIXES = (".sh", ".ps1", ".py")
KINDS = ("lib", "hook")
ARGS_UNDER_TEST = ("--help", "-h", "--no-such-flag")
TIMEOUT_SECONDS = 30
STUBBED = (
    "aws",
    "ssh",
    "scp",
    "sftp",
    "curl",
    "wget",
    "gh",
    "git",
    "gitleaks",
    "atx",
    "sudo",
    "setpriv",
    "mount",
    "chronyc",
)
STUB_EXIT = 99
# Directories that are not part of the repository tree being protected.
SNAPSHOT_SKIP = frozenset({".git", ".venv", ".worktrees", ".agents", "node_modules"})
USAGE = re.compile(r"usage|synopsis", re.IGNORECASE)
REJECTION = re.compile(
    r"unknown argument|unexpected argument|unrecognized arguments?|unrecognized option|"
    r"unknown option|usage|positional parameter|parameter cannot be found",
    re.IGNORECASE,
)


@dataclass
class RunResult:
    args: str
    returncode: int | None  # None on timeout
    output: str
    stub_calls: list[str]
    changes: list[str]


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    runs: int = 0
    scripts: int = 0


# --------------------------------------------------------------------------------- discovery
def is_pruned(rel: Path) -> bool:
    parts = rel.parts
    return "__pycache__" in parts or (len(parts) > 1 and parts[1] == "tests")


def walk_scripts(root: Path) -> tuple[list[str], list[str]]:
    """Return (runnable scripts, unrecognized executables), both as root-relative POSIX paths."""
    runnable: list[str] = []
    unrecognized: list[str] = []
    for current, dirnames, filenames in os.walk(root / "scripts"):
        rel_dir = Path(current).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not is_pruned(rel_dir / d))
        for name in sorted(filenames):
            rel = rel_dir / name
            path = root / rel
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix in SUFFIXES:
                runnable.append(rel.as_posix())
                continue
            if os.access(path, os.X_OK) or has_shebang(path):
                unrecognized.append(rel.as_posix())
    return sorted(runnable), sorted(unrecognized)


def has_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def git_listed(root: Path) -> set[str] | None:
    """Scripts git knows about, or None when <root> is not the top of a git work tree."""
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve():
            return None
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                "scripts",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if listed.returncode != 0:
        return None
    found = set()
    for raw in listed.stdout.decode("utf-8").split("\0"):
        if raw and raw.endswith(SUFFIXES) and not is_pruned(Path(raw)):
            found.add(raw)
    return found


def load_allowlist(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("scripts")
    if not isinstance(entries, dict):
        raise TypeError(f"{path}: expected an object under 'scripts'")
    for name, entry in entries.items():
        if (
            not isinstance(entry, dict)
            or entry.get("kind") not in KINDS
            or not str(entry.get("reason", "")).strip()
        ):
            raise ValueError(
                f"{path}: {name}: needs kind in {list(KINDS)} and a non-empty reason"
            )
    return entries


# ------------------------------------------------------------------------------- environment
def snapshot(top: Path, skip: frozenset[str] = frozenset()) -> dict[str, tuple]:
    """Map relative path -> (kind, size, mtime_ns) for every entry under top."""
    seen: dict[str, tuple] = {}
    for current, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in dirnames + sorted(filenames):
            path = Path(current) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            kind = "dir" if path.is_dir() and not path.is_symlink() else "file"
            seen[str(path.relative_to(top))] = (
                kind,
                0 if kind == "dir" else info.st_size,
                0 if kind == "dir" else info.st_mtime_ns,
            )
    return seen


def diff_snapshots(label: str, before: dict, after: dict) -> list[str]:
    changes = []
    for key in sorted(set(before) | set(after)):
        if key not in before:
            changes.append(f"{label}: created {key}")
        elif key not in after:
            changes.append(f"{label}: removed {key}")
        elif before[key] != after[key]:
            changes.append(f"{label}: modified {key}")
    return changes


def write_stubs(directory: Path) -> None:
    directory.mkdir(parents=True)
    body = (
        "#!/bin/sh\n"
        'printf \'%s %s\\n\' "$(basename "$0")" "$*" >> "$STUB_LOG"\n'
        f"exit {STUB_EXIT}\n"
    )
    for name in STUBBED:
        stub = directory / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)


def tool_dirs(*tools: str | None) -> list[str]:
    dirs: list[str] = []
    for tool in tools:
        if tool:
            parent = str(Path(tool).parent)
            if parent not in dirs:
                dirs.append(parent)
    return dirs


def runner_for(path: Path, bash: str | None, pwsh: str | None) -> list[str]:
    if path.suffix == ".sh":
        return [bash or "bash", str(path)]
    if path.suffix == ".py":
        return [sys.executable, str(path)]
    return [pwsh or "pwsh", "-NoProfile", "-NonInteractive", "-File", str(path)]


def run_once(
    root: Path,
    rel: str,
    arg: str,
    base: Path,
    stubs: Path,
    bash: str | None,
    pwsh: str | None,
) -> RunResult:
    """Run one script with one argument in a fresh sandbox and report what it touched."""
    sandbox = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    home, cwd, tmp, log_dir, runtime = (
        sandbox / d for d in ("home", "cwd", "tmp", "log", "runtime")
    )
    for directory in (home, cwd, tmp, log_dir, runtime):
        directory.mkdir()
    stub_log = log_dir / "calls.txt"
    stub_log.touch()
    path_entries = [str(stubs), "/usr/bin", "/bin"]
    path_entries += [
        d for d in tool_dirs(sys.executable, bash, pwsh) if d not in path_entries
    ]
    env = {
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "PATH": os.pathsep.join(path_entries),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
        # The PowerShell host keeps its own cache and module directories under the XDG roots.
        # That is the interpreter's state, not the script's, so it goes to a directory the
        # comparison does not watch; HOME itself stays watched.
        "XDG_CACHE_HOME": str(runtime / "cache"),
        "XDG_DATA_HOME": str(runtime / "data"),
        "XDG_CONFIG_HOME": str(runtime / "config"),
        "STUB_LOG": str(stub_log),
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "POWERSHELL_UPDATECHECK": "Off",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
    }
    watched = {"home": home, "cwd": cwd, "tmp": tmp}
    before = {name: snapshot(path) for name, path in watched.items()}
    before_repo = snapshot(root, SNAPSHOT_SKIP)
    returncode: int | None
    try:
        proc = subprocess.run(
            [*runner_for(root / rel, bash, pwsh), arg],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        returncode, output = proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        returncode = None
        output = (exc.stdout or b"").decode("utf-8", "replace") if exc.stdout else ""
    changes: list[str] = []
    for name, path in watched.items():
        changes += diff_snapshots(name, before[name], snapshot(path))
    changes += diff_snapshots("repo", before_repo, snapshot(root, SNAPSHOT_SKIP))
    calls = [line for line in stub_log.read_text(encoding="utf-8").splitlines() if line]
    shutil.rmtree(sandbox, ignore_errors=True)
    return RunResult(arg, returncode, output, calls, changes)


# ------------------------------------------------------------------------------------ checks
def judge(rel: str, results: list[RunResult], limited: bool) -> list[str]:
    """Return the failures for one script. `limited` applies only (i) and (iv)."""
    errors: list[str] = []
    for result in results:
        tag = f"{rel} {result.args}"
        if result.stub_calls:
            first = result.stub_calls[0]
            errors.append(
                f"{tag}: called an external tool ({len(result.stub_calls)} call(s), first: {first!r})"
            )
        for change in result.changes:
            errors.append(f"{tag}: left a file behind ({change})")
        if result.returncode is None:
            errors.append(f"{tag}: timed out after {TIMEOUT_SECONDS}s")
            continue
        if limited:
            continue
        if result.args == "--no-such-flag":
            if result.returncode == 0:
                errors.append(f"{tag}: exited 0 on an unknown option")
            elif not REJECTION.search(result.output):
                errors.append(
                    f"{tag}: exited {result.returncode} without naming the unknown option or showing usage"
                )
        else:
            if result.returncode != 0:
                errors.append(f"{tag}: exited {result.returncode}, expected 0")
            if not USAGE.search(result.output):
                errors.append(f"{tag}: printed no usage")
    return errors


def check(
    root: Path,
    allowlist_path: Path,
    only: list[str] | None = None,
    verbose: bool = False,
) -> Report:
    report = Report()
    root = root.resolve()
    if not (root / "scripts").is_dir():
        report.errors.append(f"{root}/scripts is not a directory")
        return report
    try:
        allowlist = load_allowlist(allowlist_path)
    except (OSError, TypeError, ValueError) as exc:
        report.errors.append(f"cannot read the allow-list: {exc}")
        return report

    scripts, unrecognized = walk_scripts(root)
    for rel in unrecognized:
        report.errors.append(
            f"{rel}: executable or shebang file with no runner here (suffix not in {SUFFIXES})"
        )
    listed = git_listed(root)
    if listed is not None:
        for rel in sorted(listed - set(scripts)):
            report.errors.append(
                f"{rel}: tracked but not reached by the directory walk"
            )
    for rel in sorted(allowlist):
        if rel not in scripts:
            report.errors.append(
                f"{rel}: allow-list entry names a script that is not on disk"
            )
    if only:
        for rel in only:
            if rel not in scripts:
                report.errors.append(f"--only {rel}: not a discovered script")
    if not scripts:
        report.errors.append("no scripts discovered under scripts/")
    if report.errors:
        return report

    bash = shutil.which("bash")
    pwsh = shutil.which("pwsh")
    if bash is None:
        report.errors.append(
            "bash is not on PATH (a missing tool is an error, not a skip)"
        )
    if pwsh is None and any(s.endswith(".ps1") for s in scripts):
        report.errors.append(
            "pwsh is not on PATH (a missing tool is an error, not a skip)"
        )
    if report.errors:
        return report

    selected = [s for s in scripts if not only or s in only]
    git_before = git_status(root)
    with tempfile.TemporaryDirectory(prefix="help-safety-") as tmp:
        base = Path(tmp)
        stubs = base / "stubs"
        write_stubs(stubs)
        for rel in selected:
            results = [
                run_once(root, rel, arg, base, stubs, bash, pwsh)
                for arg in ARGS_UNDER_TEST
            ]
            report.scripts += 1
            report.runs += len(results)
            found = judge(rel, results, limited=rel in allowlist)
            report.errors += found
            if verbose:
                status = "FAIL" if found else "ok"
                kind = allowlist.get(rel, {}).get("kind", "full")
                print(f"  {status:4} {rel} [{kind}]")
    git_after = git_status(root)
    if git_before != git_after:
        report.errors.append(
            "git status --porcelain changed during the run: "
            f"before={git_before!r} after={git_after!r}"
        )
    return report


def git_status(root: Path) -> str | None:
    if git_listed(root) is None:
        return None
    done = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="directory holding scripts/"
    )
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="PATH",
        help="run only these root-relative scripts (discovery and coverage still cover all)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="one line per script"
    )
    args = parser.parse_args(argv)
    report = check(args.root, args.allowlist, args.only, args.verbose)
    for error in report.errors:
        print(f"check_help_is_safe: {error}", file=sys.stderr)
    if report.errors:
        print(
            f"check_help_is_safe: FAILED ({len(report.errors)} problem(s))",
            file=sys.stderr,
        )
        return 1
    print(
        f"check_help_is_safe: {report.scripts} script(s), {report.runs} run(s); "
        "--help is answered with usage and nothing else"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
