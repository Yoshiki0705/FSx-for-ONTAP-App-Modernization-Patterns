#!/usr/bin/env python3
"""tools/check_make_targets.py fails on a make target no Makefile defines (stdlib only).

Cases: a fixture AGENTS.md naming a missing target fails; the real AGENTS.md against the real
Makefile passes; the same real documents fail once the Makefile loses one rule, which shows the
real check can fail. No network, no AWS call.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "check_make_targets", ROOT / "tools" / "check_make_targets.py"
)
checker = importlib.util.module_from_spec(spec)
sys.modules["check_make_targets"] = checker
spec.loader.exec_module(checker)

MAKEFILE = """\
.PHONY: all lint
all: lint
lint: ## static checks
\t@true
"""


def run_check(makefile: str, **docs: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "Makefile").write_text(makefile, encoding="utf-8")
        for name, text in docs.items():
            (root / name).write_text(text, encoding="utf-8")
        code, lines = checker.check(root)
    return code, "\n".join(lines)


class FixtureTests(unittest.TestCase):
    def test_missing_target_in_agents_fails_and_is_named(self) -> None:
        code, out = run_check(
            MAKEFILE, **{"AGENTS.md": "Run `make all`, then `make nope`.\n"}
        )
        self.assertEqual(code, 1)
        self.assertIn("AGENTS.md:1: make nope is not a target", out)
        self.assertNotIn("make all is not", out)

    def test_every_mention_defined_passes(self) -> None:
        code, out = run_check(
            MAKEFILE, **{"AGENTS.md": "```bash\nmake all\nmake lint\n```\n"}
        )
        self.assertEqual(code, 0)
        self.assertIn("all defined", out)

    def test_main_exit_code_for_a_fixture_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Makefile").write_text(MAKEFILE, encoding="utf-8")
            (root / "AGENTS.md").write_text("`make nope`\n", encoding="utf-8")
            self.assertEqual(checker.main(["--root", str(root)]), 1)
            (root / "AGENTS.md").write_text("`make all`\n", encoding="utf-8")
            self.assertEqual(checker.main(["--root", str(root)]), 0)

    def test_variable_phony_and_define_are_not_targets(self) -> None:
        text = "RUFF := ruff\ntool = x\n.PHONY: ghost\n.DEFAULT_GOAL := help\ndefine need\n@true\nendef\nreal:\n"
        self.assertEqual(checker.makefile_targets(text), {"real"})
        self.assertEqual(checker.phony_without_rule(text), {"ghost"})

    def test_multi_target_rule_defines_both(self) -> None:
        self.assertEqual(checker.makefile_targets("a b: dep\n"), {"a", "b"})

    def test_prose_outside_code_is_not_extracted(self) -> None:
        text = "Make sure to make a stub. We make things.\n"
        self.assertEqual(checker.mentioned_targets(text), {})

    def test_inline_code_and_fenced_block_are_both_extracted(self) -> None:
        text = "Use `make aaa`.\n\n```bash\nmake bbb  # note\n```\n\n~~~\nmake ccc\n~~~\nmake ddd\n"
        self.assertEqual(set(checker.mentioned_targets(text)), {"aaa", "bbb", "ccc"})

    def test_option_and_variable_forms_are_ignored(self) -> None:
        text = "```bash\nmake -C sub\nmake $(TARGET)\nmake PR=1\n```\n"
        self.assertEqual(checker.mentioned_targets(text), {})

    def test_contributing_absent_is_skipped(self) -> None:
        code, out = run_check(MAKEFILE, **{"AGENTS.md": "`make all`\n"})
        self.assertEqual(code, 0)
        self.assertIn("CONTRIBUTING.md: not present, skipped", out)

    def test_contributing_with_a_missing_target_fails(self) -> None:
        code, out = run_check(
            MAKEFILE,
            **{"AGENTS.md": "`make all`\n", "CONTRIBUTING.md": "`make nope`\n"},
        )
        self.assertEqual(code, 1)
        self.assertIn("CONTRIBUTING.md:1: make nope", out)

    def test_phony_without_rule_is_reported_but_does_not_fail(self) -> None:
        code, out = run_check(
            MAKEFILE + ".PHONY: ghost\n", **{"AGENTS.md": "`make all`\n"}
        )
        self.assertEqual(code, 0)
        self.assertIn("warning: .PHONY names ghost", out)

    def test_phony_alone_does_not_make_a_mention_pass(self) -> None:
        code, out = run_check(
            MAKEFILE + ".PHONY: ghost\n", **{"AGENTS.md": "`make ghost`\n"}
        )
        self.assertEqual(code, 1)
        self.assertIn("make ghost is not a target", out)

    def test_missing_makefile_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _ = checker.check(Path(directory))
        self.assertEqual(code, 2)


class RealRepositoryTests(unittest.TestCase):
    def test_real_agents_md_against_real_makefile_passes(self) -> None:
        code, out = checker.check(ROOT)
        self.assertEqual(code, 0, out)

    def test_real_documents_fail_when_a_rule_is_removed(self) -> None:
        """Negative control: the real check can fail. Drop the headings rule, keep AGENTS.md."""
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        trimmed = "\n".join(
            line for line in makefile.splitlines() if not line.startswith("headings:")
        )
        self.assertNotEqual(
            trimmed, makefile, "the real Makefile has no headings: rule to remove"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Makefile").write_text(trimmed, encoding="utf-8")
            shutil.copy(ROOT / "AGENTS.md", root / "AGENTS.md")
            code, lines = checker.check(root)
        self.assertEqual(code, 1)
        self.assertIn("make headings is not a target", "\n".join(lines))

    def test_selftest_passes(self) -> None:
        self.assertEqual(checker.selftest(), 0)


if __name__ == "__main__":
    unittest.main()
