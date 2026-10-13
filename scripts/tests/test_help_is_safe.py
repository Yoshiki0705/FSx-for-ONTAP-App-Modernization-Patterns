#!/usr/bin/env python3
"""Unit tests for check_help_is_safe.py (stdlib only).

The committed fixtures under fixtures/help-safety/ are the required pair: a script that ignores its
arguments and calls `aws` (rejected, because the stub is called) and its fixed twin (accepted). The
inline cases pin each of the four checks, the coverage rules, and the clean child environment. Every
external tool is replaced by a stub that exits 99, so no AWS or network call can happen here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "help-safety"


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_help_is_safe", HERE / "check_help_is_safe.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_help_is_safe"] = module
    spec.loader.exec_module(module)
    return module


checker = load_checker()

WELL_BEHAVED = """#!/usr/bin/env bash
case "${1:-}" in
  -h | --help) echo "usage: s.sh [-h|--help]"; exit 0 ;;
  *) echo "s: unknown argument: ${1:-}" >&2; exit 2 ;;
esac
"""


def run_case(
    files: dict[str, str],
    allowlist: dict[str, dict[str, str]] | None = None,
    only: list[str] | None = None,
) -> list[str]:
    """Write scripts under a temp root, run the checker, return its errors."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        listing = Path(tmp) / "allowlist.json"
        listing.write_text(json.dumps({"scripts": allowlist or {}}), encoding="utf-8")
        return checker.check(root, listing, only).errors


def run_tree(tree: Path) -> list[str]:
    """Copy a fixture tree to a temp root and run the checker on it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        shutil.copytree(tree, root)
        listing = Path(tmp) / "allowlist.json"
        listing.write_text(json.dumps({"scripts": {}}), encoding="utf-8")
        return checker.check(root, listing).errors


def joined(errors: list[str]) -> str:
    return "\n".join(errors)


class FixturePairTests(unittest.TestCase):
    def test_unfixed_script_is_rejected_because_the_stub_was_called(self) -> None:
        errors = run_tree(FIXTURES / "unfixed")
        self.assertTrue(errors)
        self.assertIn("called an external tool", joined(errors))
        self.assertIn("aws secretsmanager create-secret", joined(errors))
        self.assertIn("scripts/create.sh --help", joined(errors))

    def test_fixed_script_is_accepted(self) -> None:
        self.assertEqual(run_tree(FIXTURES / "fixed"), [])


class CheckTests(unittest.TestCase):
    def test_well_behaved_script_passes(self) -> None:
        self.assertEqual(run_case({"scripts/s.sh": WELL_BEHAVED}), [])

    def test_ii_exit_zero_on_unknown_option_fails(self) -> None:
        text = '#!/usr/bin/env bash\necho "usage: s.sh"\nexit 0\n'
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("exited 0 on an unknown option", errors)

    def test_ii_exit_nonzero_without_a_reason_fails(self) -> None:
        text = (
            '#!/usr/bin/env bash\ncase "${1:-}" in\n'
            '  -h | --help) echo "usage: s.sh"; exit 0 ;;\n  *) exit 3 ;;\nesac\n'
        )
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("without naming the unknown option", errors)

    def test_iii_no_usage_on_help_fails(self) -> None:
        text = (
            '#!/usr/bin/env bash\ncase "${1:-}" in\n  -h | --help) exit 0 ;;\n'
            '  *) echo "s: unknown argument: $1" >&2; exit 2 ;;\nesac\n'
        )
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("printed no usage", errors)

    def test_iii_help_that_exits_nonzero_fails(self) -> None:
        text = '#!/usr/bin/env bash\necho "usage: s.sh"\nexit 2\n'
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("--help: exited 2, expected 0", errors)

    def test_iv_file_left_in_the_working_directory_fails(self) -> None:
        text = WELL_BEHAVED.replace("-h | --help)", "-h | --help) touch marker;")
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("left a file behind (cwd: created marker)", errors)

    def test_iv_file_left_in_the_home_directory_fails(self) -> None:
        text = WELL_BEHAVED.replace(
            "-h | --help)", '-h | --help) touch "$HOME/marker";'
        )
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("left a file behind (home: created marker)", errors)

    def test_iv_file_written_into_the_repository_tree_fails(self) -> None:
        # The ignored-directory case that `git status` cannot see: a path derived from the script.
        text = WELL_BEHAVED.replace(
            "-h | --help)",
            '-h | --help) mkdir -p "$(dirname "$0")/../.private"; touch "$(dirname "$0")/../.private/x";',
        )
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("left a file behind (repo: created .private", errors)

    def test_a_stub_is_called_when_the_tool_is_run_from_a_subshell(self) -> None:
        text = WELL_BEHAVED.replace(
            "-h | --help)", "-h | --help) (curl http://example.invalid) || true;"
        )
        errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("called an external tool", errors)

    def test_python_script_is_discovered_and_checked(self) -> None:
        text = (
            "import subprocess\nsubprocess.run(['aws', 'sts', 'get-caller-identity'])\n"
        )
        errors = joined(run_case({"scripts/p.py": text}))
        self.assertIn("scripts/p.py --help: called an external tool", errors)

    def test_python_argparse_script_passes(self) -> None:
        text = "import argparse\nargparse.ArgumentParser().parse_args()\n"
        self.assertEqual(run_case({"scripts/p.py": text}), [])


class CoverageTests(unittest.TestCase):
    def test_new_unlisted_script_is_discovered_and_fails(self) -> None:
        errors = joined(
            run_case(
                {
                    "scripts/s.sh": WELL_BEHAVED,
                    "scripts/new/added-later.sh": "#!/usr/bin/env bash\necho hi\n",
                }
            )
        )
        self.assertIn("scripts/new/added-later.sh", errors)

    def test_tests_directory_is_not_an_entry_point(self) -> None:
        files = {
            "scripts/s.sh": WELL_BEHAVED,
            "scripts/tests/t.sh": "#!/bin/bash\necho hi\n",
        }
        self.assertEqual(run_case(files), [])

    def test_executable_without_a_known_suffix_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "s.sh").write_text(WELL_BEHAVED, encoding="utf-8")
            tool = root / "scripts" / "tool"
            tool.write_text(
                "#!/usr/bin/env bash\nrm -rf /nonexistent\n", encoding="utf-8"
            )
            tool.chmod(0o755)
            listing = Path(tmp) / "allowlist.json"
            listing.write_text('{"scripts": {}}', encoding="utf-8")
            errors = joined(checker.check(root, listing).errors)
        self.assertIn("scripts/tool: executable or shebang file with no runner", errors)

    def test_stale_allowlist_entry_fails(self) -> None:
        errors = joined(
            run_case(
                {"scripts/s.sh": WELL_BEHAVED},
                {"scripts/gone.sh": {"kind": "lib", "reason": "was removed"}},
            )
        )
        self.assertIn(
            "scripts/gone.sh: allow-list entry names a script that is not on disk",
            errors,
        )

    def test_allowlist_entry_needs_a_kind_and_a_reason(self) -> None:
        for entry in (
            {"kind": "lib"},
            {"kind": "tool", "reason": "x"},
            {"reason": "x"},
        ):
            errors = joined(
                run_case({"scripts/s.sh": WELL_BEHAVED}, {"scripts/s.sh": entry})
            )
            self.assertIn("cannot read the allow-list", errors, msg=str(entry))

    def test_allowlisted_library_is_exempt_from_usage_but_not_from_side_effects(
        self,
    ) -> None:
        quiet = "#!/usr/bin/env bash\nX=1\n"
        allow = {"scripts/lib.sh": {"kind": "lib", "reason": "sourced"}}
        self.assertEqual(run_case({"scripts/lib.sh": quiet}, allow), [])
        calls_aws = "#!/usr/bin/env bash\naws sts get-caller-identity\n"
        errors = joined(run_case({"scripts/lib.sh": calls_aws}, allow))
        self.assertIn("called an external tool", errors)
        self.assertNotIn("printed no usage", errors)

    def test_only_limits_the_runs_but_not_discovery(self) -> None:
        files = {
            "scripts/ok.sh": WELL_BEHAVED,
            "scripts/bad.sh": "#!/usr/bin/env bash\naws sts get-caller-identity\n",
        }
        self.assertEqual(run_case(files, only=["scripts/ok.sh"]), [])
        self.assertIn(
            "not a discovered script", joined(run_case(files, only=["scripts/nope.sh"]))
        )


class EnvironmentTests(unittest.TestCase):
    def test_inherited_dry_run_does_not_reach_the_child(self) -> None:
        # Under APPMOD_DRY_RUN the unfixed script would print instead of calling aws and pass.
        text = (
            '#!/usr/bin/env bash\nif [ -z "${APPMOD_DRY_RUN:-}" ]; then aws sts get-caller-identity; fi\n'
            'echo "usage: s.sh"\n'
        )
        with mock.patch.dict(
            os.environ, {"APPMOD_DRY_RUN": "1", "AWS_PROFILE": "real"}
        ):
            errors = joined(run_case({"scripts/s.sh": text}))
        self.assertIn("called an external tool", errors)

    def test_child_sees_a_throwaway_home_and_no_aws_variables(self) -> None:
        text = (
            "#!/usr/bin/env bash\n"
            'if [ -n "${AWS_PROFILE:-}${APPMOD_DRY_RUN:-}" ]; then echo leaked >&2; touch "$HOME/leak"; fi\n'
            'case "${1:-}" in -h | --help) echo "usage: s.sh"; exit 0 ;; esac\n'
            'echo "s: unknown argument: ${1:-}" >&2; exit 2\n'
        )
        with mock.patch.dict(
            os.environ, {"APPMOD_DRY_RUN": "1", "AWS_PROFILE": "real"}
        ):
            self.assertEqual(run_case({"scripts/s.sh": text}), [])


if __name__ == "__main__":
    unittest.main()
