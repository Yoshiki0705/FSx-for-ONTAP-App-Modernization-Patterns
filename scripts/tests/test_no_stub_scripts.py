#!/usr/bin/env python3
"""Unit tests for check_no_stub_scripts.py (stdlib only).

The committed fixtures under fixtures/stub-check/ are the required pair: a stub whose real branch
only echoes `aws ssm send-command ...` (rejected) and its control (accepted). The inline cases pin
each dry-run branch form and each non-invocation the checker must not count, so a parser change
that starts reading an echoed command as a real one fails here. No AWS, ONTAP or network call.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "stub-check"


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_no_stub_scripts", HERE / "check_no_stub_scripts.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_no_stub_scripts"] = module
    spec.loader.exec_module(module)
    return module


checker = load_checker()

HEAD = '#!/usr/bin/env bash\nset -euo pipefail\nDRY_RUN="${APPMOD_DRY_RUN:-}"\n'


def run_case(files: dict[str, str], classes: dict[str, str] | None = None) -> list[str]:
    """Write scripts under a temp root, classify them (default external), return the errors."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            path = root / "scripts" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        classes = classes or {}
        entries = {
            f"scripts/{rel}": {"class": classes.get(rel, "external"), "reason": "test"}
            for rel in files
        }
        classification = root / "classification.json"
        classification.write_text(json.dumps({"scripts": entries}), encoding="utf-8")
        return checker.check(root, classification)


def one(text: str, klass: str = "external") -> list[str]:
    return run_case({"s.sh": HEAD + text}, {"s.sh": klass})


class CommittedFixtures(unittest.TestCase):
    def test_stub_fixture_is_rejected(self):
        errors = checker.check(FIXTURES / "stub", FIXTURES / "classification.json")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("scripts/send-ssm.sh", errors[0])
        self.assertIn("a stub", errors[0])

    def test_control_fixture_is_accepted(self):
        errors = checker.check(FIXTURES / "control", FIXTURES / "classification.json")
        self.assertEqual(errors, [])

    def test_main_exit_codes(self):
        classification = str(FIXTURES / "classification.json")
        stub = ["--root", str(FIXTURES / "stub"), "--classification", classification]
        control = [
            "--root",
            str(FIXTURES / "control"),
            "--classification",
            classification,
        ]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(checker.main(stub), 1)
        self.assertIn("a stub", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checker.main(control), 0)


class DryRunBranchForms(unittest.TestCase):
    def test_if_else_fi_real_in_else(self):
        text = 'if [ -n "$DRY_RUN" ]; then\n  echo "aws s3 ls"\nelse\n  aws s3 ls\nfi\n'
        self.assertEqual(one(text), [])

    def test_aws_only_in_dry_branch_is_a_stub(self):
        text = 'if [ -n "$DRY_RUN" ]; then\n  aws s3 ls\nelse\n  echo "aws s3 ls"\nfi\n'
        self.assertEqual(len(one(text)), 1)

    def test_one_line_dry_early_return(self):
        text = (
            'call() {\n  if [ -n "$DRY_RUN" ]; then echo "DRY-RUN: aws $*"; return 0; fi\n'
            '  aws "$@"\n}\ncall s3 ls\n'
        )
        self.assertEqual(one(text), [])

    def test_z_test_then_branch_is_real(self):
        self.assertEqual(one('if [ -z "$DRY_RUN" ]; then aws s3 ls; fi\n'), [])

    def test_z_test_else_branch_is_dry(self):
        text = 'if [ -z "${DRY_RUN:-}" ]; then echo skip; else aws s3 ls; fi\n'
        self.assertEqual(len(one(text)), 1)

    def test_or_clause_then_branch_runs_in_either_mode(self):
        text = 'if [ -n "$DRY_RUN" ] || [ -f x ]; then aws s3 ls; fi\n'
        self.assertEqual(one(text), [])

    def test_and_clause_then_branch_is_dry(self):
        text = 'if [ -n "$DRY_RUN" ] && [ -f x ]; then aws s3 ls; fi\n'
        self.assertEqual(len(one(text)), 1)


class NonInvocations(unittest.TestCase):
    def test_note_only_is_a_stub(self):
        text = 'note() { echo "x: $*"; }\nnote "POST /api/protocols/cifs/shares"\nnote "aws ssm send-command"\n'
        self.assertEqual(len(one(text)), 1)

    def test_printf_argument_is_a_stub(self):
        self.assertEqual(len(one("printf '%s\\n' 'aws s3 cp a b'\n")), 1)

    def test_run_echo_through_a_real_runner_is_a_stub(self):
        runner = 'run() {\n  if [ -n "$DRY_RUN" ]; then echo "DRY-RUN: $*"; return 0; fi\n  "$@"\n}\n'
        self.assertEqual(len(one(runner + 'run echo "aws ssm send-command"\n')), 1)
        self.assertEqual(one(runner + "run aws ssm send-command\n"), [])

    def test_runner_shift_offset(self):
        step = 'step() {\n  local d="$1"; shift\n  echo "- $d"\n  "$@"\n}\n'
        self.assertEqual(one(step + 'step "list" aws s3 ls\n'), [])
        self.assertEqual(len(one(step + "step aws echo hi\n")), 1)

    def test_comment_heredoc_and_quoted_string_do_not_count(self):
        text = (
            "# aws s3 ls\n"
            "cat <<EOF\naws s3 ls\nEOF\n"
            'remote="set -eu; aws s3 cp s3://b/k ."\n'
            'echo "$remote"\n'
        )
        self.assertEqual(len(one(text)), 1)

    def test_command_v_is_a_lookup(self):
        self.assertEqual(len(one("command -v aws >/dev/null\n")), 1)

    def test_array_literal_is_data(self):
        self.assertEqual(len(one('CMD=(aws s3 ls)\necho "${CMD[*]}"\n')), 1)

    def test_command_substitution_in_an_echo_argument_is_real(self):
        self.assertEqual(one('echo "$(aws sts get-caller-identity)"\n'), [])

    def test_case_pattern_is_not_a_command(self):
        self.assertEqual(len(one('case "$1" in\n  aws) echo hi ;;\nesac\n')), 1)


class OtherInvocations(unittest.TestCase):
    def test_git_clone_install_sh_gitleaks(self):
        self.assertEqual(one("git -C /tmp clone https://example.invalid/r.git\n"), [])
        self.assertEqual(len(one("git -C /tmp status\n")), 1)
        self.assertEqual(one('bash "$HOME/up/install.sh" --project x\n'), [])
        self.assertEqual(one("gitleaks dir . --no-banner\n"), [])

    def test_wrapper_in_a_sourced_library(self):
        lib = 'call() {\n  if [ -n "$DRY_RUN" ]; then echo "DRY-RUN"; return 0; fi\n  curl -sS "$1"\n}\n'
        main = HEAD + 'HERE="$(dirname "$0")"\n. "$HERE/lib/l.sh"\ncall https://x/api\n'
        errors = run_case({"lib/l.sh": HEAD + lib, "m.sh": main})
        self.assertEqual(errors, [])

    def test_echo_only_library_function_is_not_a_wrapper(self):
        lib = 'call() { echo "curl -sS $1"; }\n'
        main = HEAD + 'HERE="$(dirname "$0")"\n. "$HERE/lib/l.sh"\ncall https://x/api\n'
        errors = run_case({"lib/l.sh": HEAD + lib, "m.sh": main}, {"lib/l.sh": "pure"})
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("scripts/m.sh", errors[0])


class FailClosed(unittest.TestCase):
    GATE = (
        "if verified; then\n  echo ok\n"
        'elif [ -n "$DRY_RUN" ]; then\n  echo "would exit 2"\n'
        "else\n  echo unverified >&2\n  exit 2\nfi\n"
    )

    def test_real_only_exit_after_elif(self):
        self.assertEqual(one(self.GATE, "fail-closed"), [])

    def test_missing_real_only_exit(self):
        text = 'if [ -n "$DRY_RUN" ]; then echo dry; fi\natx custom def exec\n'
        self.assertEqual(len(one(text, "fail-closed")), 1)

    def test_usage_exit_in_either_mode_does_not_count(self):
        text = "if [ $# -lt 1 ]; then echo usage >&2; exit 2; fi\n"
        self.assertEqual(len(one(text, "fail-closed")), 1)

    def test_dry_branch_exit_does_not_count(self):
        self.assertEqual(
            len(one('if [ -n "$DRY_RUN" ]; then exit 2; fi\n', "fail-closed")), 1
        )


class Classification(unittest.TestCase):
    def test_unclassified_script_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "new.sh").write_text(
                HEAD + "aws s3 ls\n", encoding="utf-8"
            )
            classification = root / "c.json"
            classification.write_text('{"scripts": {}}', encoding="utf-8")
            errors = checker.check(root, classification)
        self.assertEqual(len(errors), 1)
        self.assertIn("scripts/new.sh: not classified", errors[0])

    def test_stale_entry_bad_class_and_empty_reason_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "a.sh").write_text(HEAD, encoding="utf-8")
            (root / "scripts" / "b.sh").write_text(HEAD, encoding="utf-8")
            classification = root / "c.json"
            entries = {
                "scripts/a.sh": {"class": "other", "reason": "x"},
                "scripts/b.sh": {"class": "pure", "reason": " "},
                "scripts/gone.sh": {"class": "pure", "reason": "x"},
            }
            classification.write_text(
                json.dumps({"scripts": entries}), encoding="utf-8"
            )
            errors = "\n".join(checker.check(root, classification))
        self.assertIn("scripts/a.sh: class must be one of", errors)
        self.assertIn("scripts/b.sh: a one-line reason is required", errors)
        self.assertIn("scripts/gone.sh: classified", errors)

    def test_tests_directory_is_excluded_from_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts" / "tests").mkdir(parents=True)
            (root / "scripts" / "ontap").mkdir()
            (root / "scripts" / "tests" / "t.sh").write_text(HEAD, encoding="utf-8")
            (root / "scripts" / "ontap" / "o.sh").write_text(HEAD, encoding="utf-8")
            self.assertEqual(checker.discover(root), ["scripts/ontap/o.sh"])

    def test_every_repository_script_is_discovered(self):
        # The discovery must match a plain recursive glob, so a script added one directory deeper
        # cannot fall outside the gate.
        root = HERE.parent.parent
        expected = sorted(
            p.relative_to(root).as_posix()
            for p in (root / "scripts").rglob("*.sh")
            if "tests" not in p.relative_to(root / "scripts").parts[:1]
        )
        self.assertEqual(checker.discover(root), expected)
        self.assertGreater(len(expected), 0)


if __name__ == "__main__":
    unittest.main()
