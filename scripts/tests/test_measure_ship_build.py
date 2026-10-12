#!/usr/bin/env python3
"""Unit tests for scripts/measure/ship_build.py and the host scripts it sends (no network).

Pure parts: sha list parsing, host-hash parsing, the delta rule, chunking, the generated chunk
script, the install environment, tar creation (COPYFILE_DISABLE), argument validation. One
end-to-end test drives main() against a fake AWS runner, decodes what would have been sent,
reassembles the payload, and checks that only the differing files are inside it. The host scripts
are checked statically (the destination is created with mkdir, never mkdir -p; exits 20/21/22/23
exist; nothing deletes outside temp paths) plus bash -n; host-hashes.sh runs for its argument
guards. install-build.sh writes under /opt/appmod and is not executed here.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MEASURE = Path(__file__).resolve().parent.parent / "measure"
sys.path.insert(0, str(MEASURE))

import common
import ship_build

INSTANCE = "i-0123456789abcdef0"
RUN_ID = "s2m-20261012T090934Z"
SHA = "a" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ListTests(unittest.TestCase):
    def test_local_list_skips_appledouble_and_ds_store(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "a").write_bytes(b"A")
            (root / "sub").mkdir()
            (root / "sub" / "b").write_bytes(b"B")
            (root / "._a").write_bytes(b"junk")
            (root / ".DS_Store").write_bytes(b"junk")
            got = ship_build.local_list(root)
        self.assertEqual(got, {"./a": sha(b"A"), "./sub/b": sha(b"B")})

    def test_local_list_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "a").write_bytes(b"A")
            (root / "l").symlink_to(root / "a")
            with self.assertRaises(ValueError):
                ship_build.local_list(root)

    def test_sha_list_round_trip(self) -> None:
        listing = {"./b": "2" * 64, "./a b": "1" * 64}
        text = ship_build.format_sha_list(listing)
        self.assertEqual(text.splitlines()[0], "1" * 64 + "  ./a b")
        self.assertEqual(ship_build.parse_sha_list(text), listing)
        with self.assertRaises(ValueError):
            ship_build.parse_sha_list("nohashhere\n")

    def test_parse_host_hashes(self) -> None:
        text = (
            "===/opt/appmod/earlier/probe\n"
            "0123456789abcdef  ./DocIntake.Probe\n"
            "fedcba9876543210  ./lib/x.dll\n"
            "===/opt/appmod/earlier/worker\n"
            "1111111111111111  ./DocIntake.Worker\n"
        )
        got = ship_build.parse_host_hashes(text)
        self.assertEqual(
            set(got), {"/opt/appmod/earlier/probe", "/opt/appmod/earlier/worker"}
        )
        self.assertEqual(
            got["/opt/appmod/earlier/probe"]["./lib/x.dll"], "fedcba9876543210"
        )


class DeltaTests(unittest.TestCase):
    def test_delta_compares_16_hex_prefix_and_missing(self) -> None:
        local = {"./same": "a" * 64, "./changed": "b" * 64, "./new": "c" * 64}
        host = {"./same": "a" * 16, "./changed": "0" * 16}
        self.assertEqual(ship_build.compute_delta(local, host), ["./changed", "./new"])

    def test_no_base_sends_everything(self) -> None:
        local = {"./b": "b" * 64, "./a": "a" * 64}
        self.assertEqual(ship_build.compute_delta(local, None), ["./a", "./b"])

    def test_empty_base_sends_everything(self) -> None:
        self.assertEqual(ship_build.compute_delta({"./a": "a" * 64}, {}), ["./a"])

    def test_nothing_changed(self) -> None:
        self.assertEqual(
            ship_build.compute_delta({"./a": "a" * 64}, {"./a": "a" * 16}), []
        )


class ChunkTests(unittest.TestCase):
    def test_chunks_reassemble_byte_identical(self) -> None:
        data = os.urandom(5000)
        chunks = ship_build.chunk_b64(data, size=777)
        self.assertTrue(all(len(c) <= 777 for c in chunks))
        self.assertEqual(base64.b64decode("".join(chunks)), data)
        self.assertEqual(ship_build.payload_sha(data), sha(data))

    def test_chunk_script_appends_and_keeps_payload_in_one_argument(self) -> None:
        script = ship_build.chunk_script(RUN_ID, 3, "QUJD")
        self.assertIn(
            f"printf '%s' 'QUJD' >> /tmp/appmod-ship-{RUN_ID}-payload.b64", script
        )
        self.assertEqual(script.count("QUJD"), 1)
        self.assertNotIn("> /tmp", script.replace(">> /tmp", ""))
        self.assertIn("chunk-3-ok", script)

    def test_init_script_truncates(self) -> None:
        script = ship_build.init_script(RUN_ID)
        self.assertIn(f": > /tmp/appmod-ship-{RUN_ID}-payload.b64", script)


class EnvTests(unittest.TestCase):
    def kw(self, **over):
        base = {
            "sha": SHA,
            "dest_dir": "/opt/appmod/modern",
            "run_id": RUN_ID,
            "components": ["probe", "worker"],
            "bases": {"probe": "/opt/appmod/earlier/probe"},
            "executables": ["probe/DocIntake.Probe"],
            "prune_extra": True,
        }
        base.update(over)
        return base

    def test_full_environment(self) -> None:
        env = ship_build.install_env(**self.kw())
        self.assertEqual(env["APPMOD_COMPONENTS"], "probe,worker")
        self.assertEqual(env["APPMOD_BASES"], "probe=/opt/appmod/earlier/probe")
        self.assertEqual(env["APPMOD_PRUNE_EXTRA"], "1")
        self.assertEqual(env["APPMOD_PAYLOAD_SHA256"], SHA)

    def test_optional_values_omitted(self) -> None:
        env = ship_build.install_env(
            **self.kw(bases={}, executables=[], prune_extra=False)
        )
        self.assertEqual(
            set(env),
            {
                "APPMOD_PAYLOAD_SHA256",
                "APPMOD_DEST_DIR",
                "APPMOD_RUN_ID",
                "APPMOD_COMPONENTS",
            },
        )

    def test_values_pass_the_ssm_env_shape(self) -> None:
        for name, value in ship_build.install_env(**self.kw()).items():
            common.validate_env_item(f"{name}={value}")

    def test_executable_must_belong_to_a_component(self) -> None:
        self.assertEqual(
            ship_build.validate_executables(["probe/x"], ["probe", "worker"]),
            ["probe/x"],
        )
        for bad in ("other/x", "probe/../x", "/abs"):
            with self.assertRaises(ValueError, msg=bad):
                ship_build.validate_executables([bad], ["probe"])

    def test_parse_pairs(self) -> None:
        self.assertEqual(
            ship_build.parse_pairs(["a=/x", "b=/y"], "--c"), {"a": "/x", "b": "/y"}
        )
        for bad in (["noequals"], ["A=/x"], ["a=/x", "a=/y"]):
            with self.assertRaises(ValueError, msg=bad):
                ship_build.parse_pairs(bad, "--c")


class TarTests(unittest.TestCase):
    def test_copyfile_disable_is_passed(self) -> None:
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            seen["env"] = kw["env"]
            Path(argv[argv.index("-czf") + 1]).write_bytes(b"tgz")
            return subprocess.CompletedProcess(argv, 0)

        with tempfile.TemporaryDirectory() as t:
            data = ship_build.make_tarball(Path(t), Path(t) / "p.tgz", runner=fake)
        self.assertEqual(data, b"tgz")
        self.assertEqual(seen["env"]["COPYFILE_DISABLE"], "1")
        self.assertEqual(seen["argv"][0], "tar")


class FakeHost:
    """Fake AWS CLI: records each script that would run on the host and answers from a state."""

    def __init__(self, host_hashes_text: str = "", install_status: str = "Success"):
        self.scripts: list[tuple[str, str]] = []  # (script text, env preamble)
        self.host_hashes_text = host_hashes_text
        self.install_status = install_status
        self.last = ""

    def __call__(self, argv, **kw):
        if "send-command" in argv:
            path = argv[argv.index("--cli-input-json") + 1].removeprefix("file://")
            req = json.loads(Path(path).read_text())
            cmds = req["Parameters"]["commands"]
            pre = "\n".join(c for c in cmds if c.startswith("export "))
            printf = next(c for c in cmds if c.startswith("printf"))
            body = base64.b64decode(printf.split("'")[3]).decode()
            self.scripts.append((body, pre))
            self.last = body
            return subprocess.CompletedProcess(argv, 0, "cmd-1\n", "")
        out = "ok\n"
        status = "Success"
        if "APPMOD_HASH_DIRS" in self.last:
            out = self.host_hashes_text
        elif "APPMOD_PAYLOAD_SHA256" in self.last:
            status = self.install_status
        inv = {
            "CommandId": "cmd-1",
            "Status": status,
            "ResponseCode": 0 if status == "Success" else 23,
            "StandardOutputContent": out,
            "StandardErrorContent": "",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(inv), "")


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("APPMOD_LNX_INSTANCE", None)
        os.environ.pop("APPMOD_DRY_RUN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.probe = self.root / "pub-probe"
        self.probe.mkdir()
        (self.probe / "same.dll").write_bytes(b"same")
        (self.probe / "changed.dll").write_bytes(b"new-bytes")
        (self.probe / "DocIntake.Probe").write_bytes(b"exe")
        self.out = self.root / "out"

    def argv(self, *extra: str) -> list[str]:
        return [
            "--instance", INSTANCE, "--run-id", RUN_ID, "--component", f"probe={self.probe}",
            "--dest-dir", "/opt/appmod/modern", "--out-dir", str(self.out), *extra,
        ]  # fmt: skip

    def run_main(self, argv, runner=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ship_build.main(
                argv, runner=runner or FakeHost(), sleep=lambda s: None, **kw
            )
        return code, out.getvalue(), err.getvalue()

    def with_value(self, flag: str, value: str) -> list[str]:
        argv = self.argv()
        argv[argv.index(flag) + 1] = value
        return argv

    def test_bad_arguments_exit_2(self) -> None:
        no_instance = [a for a in self.argv() if a not in ("--instance", INSTANCE)]
        cases = [
            no_instance,
            self.with_value("--dest-dir", "/tmp/modern"),
            self.with_value("--dest-dir", "/opt/appmod/../etc"),
            self.with_value("--component", f"Probe={self.probe}"),
            self.with_value("--component", f"probe={self.root / 'missing'}"),
            self.argv("--base", "worker=/opt/appmod/earlier/worker"),
            self.argv("--base", "probe=/etc"),
            self.argv("--executable", "worker/x"),
            self.argv("--max-chunks", "0"),
        ]
        for argv in cases:
            code, _, _ = self.run_main(argv, runner=_boom)
            self.assertEqual(code, 2, msg=argv)

    def test_bad_instance_and_run_id_exit_2(self) -> None:
        for flag, bad in (("--instance", "i-nope"), ("--run-id", "s2-2026")):
            code, _, _ = self.run_main(self.with_value(flag, bad), runner=_boom)
            self.assertEqual(code, 2, msg=flag)

    def test_dry_run_makes_no_aws_call_and_writes_the_plan(self) -> None:
        code, out, _ = self.run_main(self.argv("--dry-run"), runner=_boom)
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["delta_files"], {"probe": 3})
        self.assertFalse(plan["delta_read_from_host"])
        self.assertTrue((self.out / "ship-delta.json").exists())

    def test_chunk_guard_exit_3(self) -> None:
        (self.probe / "big.bin").write_bytes(os.urandom(40000))
        code, _, err = self.run_main(
            self.argv("--max-chunks", "1", "--dry-run"), runner=_boom
        )
        self.assertEqual(code, 3)
        self.assertIn("--max-chunks", err)

    def test_end_to_end_sends_only_the_delta(self) -> None:
        base = "/opt/appmod/earlier/probe"
        host_text = (
            f"==={base}\n"
            f"{sha(b'same')[:16]}  ./same.dll\n"
            f"{sha(b'old-bytes')[:16]}  ./changed.dll\n"
            f"{sha(b'stale')[:16]}  ./stale.dll\n"
        )
        fake = FakeHost(host_text)
        code, _out, err = self.run_main(
            self.argv(
                "--base",
                f"probe={base}",
                "--executable",
                "probe/DocIntake.Probe",
                "--prune-extra",
            ),
            runner=fake,
        )
        self.assertEqual(code, 0, msg=err)
        bodies = [b for b, _ in fake.scripts]
        # host-hashes step, init, N chunks, install
        self.assertIn("APPMOD_HASH_DIRS", bodies[0])
        self.assertTrue(fake.scripts[0][1].endswith(f"export APPMOD_HASH_DIRS={base}"))
        self.assertIn(": > ", bodies[1])
        chunks = []
        for b in bodies[2:-1]:
            m = re.search(r"printf '%s' '([A-Za-z0-9+/=]*)' >> ", b)
            self.assertIsNotNone(m)
            chunks.append(m.group(1))
        install_body, install_pre = fake.scripts[-1]
        self.assertIn("die 21", install_body)
        data = base64.b64decode("".join(chunks))
        want = re.search(
            r"export APPMOD_PAYLOAD_SHA256=([0-9a-f]{64})", install_pre
        ).group(1)
        self.assertEqual(sha(data), want)
        for line in (
            "export APPMOD_DEST_DIR=/opt/appmod/modern",
            f"export APPMOD_BASES=probe={base}",
            "export APPMOD_EXECUTABLES=probe/DocIntake.Probe",
            "export APPMOD_PRUNE_EXTRA=1",
            "export APPMOD_COMPONENTS=probe",
        ):
            self.assertIn(line, install_pre)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = sorted(
                m.name.removeprefix("./") for m in tf.getmembers() if m.isfile()
            )
            full_list = tf.extractfile("./probe.sha256").read().decode()
        # changed.dll differs, DocIntake.Probe is missing on the host, same.dll is not sent.
        self.assertEqual(
            names, ["probe.sha256", "probe/DocIntake.Probe", "probe/changed.dll"]
        )
        self.assertEqual(
            ship_build.parse_sha_list(full_list),
            {
                "./DocIntake.Probe": sha(b"exe"),
                "./changed.dll": sha(b"new-bytes"),
                "./same.dll": sha(b"same"),
            },
        )
        self.assertEqual(
            json.loads((self.out / "ship-delta.json").read_text())["probe"],
            ["./DocIntake.Probe", "./changed.dll"],
        )
        self.assertTrue((self.out / "ship-install.json").exists())

    def test_install_failure_is_reported(self) -> None:
        fake = FakeHost(install_status="Failed")
        code, _, err = self.run_main(self.argv(), runner=fake)
        self.assertEqual(code, 1)
        self.assertIn("remote step install failed", err)

    def test_expired_token_exit_20(self) -> None:
        def expired(argv, **kw):
            return subprocess.CompletedProcess(argv, 255, "", "Token has expired")

        code, _, err = self.run_main(self.argv(), runner=expired)
        self.assertEqual(code, 20)
        self.assertIn("aws sso login", err)


def _boom(*a, **k):
    raise AssertionError("no AWS call is allowed here")


class HostScriptTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (MEASURE / "host" / name).read_text()

    def test_bash_syntax(self) -> None:
        for name in ("host-hashes.sh", "install-build.sh", "standalone-probe.sh"):
            r = subprocess.run(
                ["bash", "-n", str(MEASURE / "host" / name)],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(r.returncode, 0, msg=f"{name}: {r.stderr}")

    def test_install_build_guards_exist(self) -> None:
        s = self.read("install-build.sh")
        self.assertIn('die 20 "payload sha256 mismatch"', s)
        self.assertIn('die 21 "$DEST already exists', s)
        self.assertIn('die 22 "APPMOD_DEST_DIR must match', s)
        self.assertIn('die 23 "verification failed', s)
        self.assertIn("DEST_RE='^/opt/appmod/", s)

    def test_install_build_creates_destination_without_parents_option(self) -> None:
        s = self.read("install-build.sh")
        code = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
        self.assertIn('mkdir "$DEST"\n', s)
        self.assertNotRegex(code, r"mkdir\s+-p")
        # The destination existence check comes before mkdir.
        self.assertLess(s.index('if [ -e "$DEST" ]'), s.index('mkdir "$DEST"\n'))
        # The payload hash is checked before the destination is touched.
        self.assertLess(s.index("die 20"), s.index('mkdir "$DEST"\n'))

    def test_install_build_deletes_only_temp_paths_and_pruned_files(self) -> None:
        s = self.read("install-build.sh")
        for line in s.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "rm " not in stripped:
                continue
            ok = (
                'rm -rf "$XDIR" "$WORK"' in stripped
                or 'rm -f "$B64" "$TGZ"' in stripped
                or 'rm -f -- "$DEST/$c/$path"' in stripped
            )
            self.assertTrue(ok, msg=f"unexpected delete: {stripped}")
        # Pruning is opt-in and only for regular, non-symlink files.
        self.assertIn('if [ "$PRUNE" = "1" ]', s)
        self.assertIn('[ ! -L "$DEST/$c/$path" ]', s)

    def test_host_hashes_is_read_only(self) -> None:
        s = self.read("host-hashes.sh")
        for forbidden in ("rm ", "mv ", "cp ", "chmod", "chown", "mkdir", "> /"):
            self.assertNotIn(
                forbidden,
                "\n".join(l for l in s.splitlines() if not l.strip().startswith("#")),
            )

    def run_hashes(self, env: dict[str, str]):
        e = {"PATH": os.environ["PATH"], **env}
        return subprocess.run(
            ["bash", str(MEASURE / "host" / "host-hashes.sh")],
            env=e,
            capture_output=True,
            check=False,
            text=True,
        )

    def test_host_hashes_argument_guards(self) -> None:
        self.assertEqual(self.run_hashes({}).returncode, 2)
        for bad in (
            "/etc",
            "/opt/appmod/../etc",
            "relative/dir",
            "/opt/appmod/does-not-exist-xyz",
        ):
            r = self.run_hashes({"APPMOD_HASH_DIRS": bad})
            self.assertEqual(r.returncode, 2, msg=bad)
            self.assertEqual(r.stdout, "", msg=bad)

    def test_help_exits_zero(self) -> None:
        for name in ("host-hashes.sh", "install-build.sh", "standalone-probe.sh"):
            r = subprocess.run(
                ["bash", str(MEASURE / "host" / name), "--help"],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(r.returncode, 0, msg=name)
            self.assertIn("APPMOD_", r.stdout)


if __name__ == "__main__":
    unittest.main()
