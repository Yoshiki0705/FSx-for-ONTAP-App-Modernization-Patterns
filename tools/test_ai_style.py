"""The ai-style detector fires on its examples and agrees with the renderer on `**`.

Two things a `--selftest` cannot say about itself live here. The fixture-agreement cases prove D1
matches markdown-it on whether a `**` renders — the expected counts in
`tools/fixtures/ai_style_strong.json` were produced by markdown-it through the Hub's
`scripts/regenerate_ai_style_fixtures.mjs`, which needs Node and is not run here: this test reads
the generated JSON only, so the gate stays stdlib-only. The rule cases prove every rule carries a
positive and a negative example, that non-prose spans are excluded, and that languages are kept
apart.

Adapted from the Hub's `test_ai_style_strong_fixtures.py` and the non-audit classes of
`test_ai_style_rules.py`. The audit-integration class and the criteria-document class are dropped:
this spoke has no `audit_public_output.py` and no local `writing-quality.md` — the criteria live on
the Hub and are linked from AGENTS.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ai_style_rules as rules
from ai_style_rules import unrendered_strong

FIXTURE = ROOT / "tools" / "fixtures" / "ai_style_strong.json"
SYNTHETIC = "上は**「自分の構成」**で引く。次は**自分の構成**で引く。"

BROKEN = "上は**「自分の構成」**で引く。\n"
RENDERED = "次は**自分の構成**で引く。\n"
# Enough kana for language_of to read a file with no language in its path as Japanese.
JA_LEAD = "これはひらがなを十分に含む日本語の段落です。\n\n"


def rule_ids(document: str, lang: str = "ja") -> set[str]:
    return {finding.rule for finding in rules.scan_markdown(document, lang)}


class StrongFixturesAgreeWithMarkdownIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.cases = cls.data["cases"]

    def test_every_case_matches_the_renderer(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(
                    len(unrendered_strong(case["markdown"])),
                    case["literal_double_stars"],
                    f"{case['id']}: Python and markdown-it disagree on {case['markdown']!r}",
                )

    def test_the_fixture_can_tell_the_two_outcomes_apart(self) -> None:
        """An all-zero fixture would pass against an engine that never reports anything."""
        self.assertGreaterEqual(len(self.cases), 25)
        values = {case["literal_double_stars"] for case in self.cases}
        self.assertIn(0, values)
        self.assertTrue(any(value > 0 for value in values))

    def test_the_motivating_sentence_is_present_and_broken(self) -> None:
        matching = [case for case in self.cases if SYNTHETIC in case["markdown"]]
        self.assertTrue(
            matching, "the bracketed-bold sentence is missing from the fixture"
        )
        self.assertGreaterEqual(matching[0]["literal_double_stars"], 1)

    def test_the_fixture_names_its_generator(self) -> None:
        # The generator (scripts/regenerate_ai_style_fixtures.mjs) lives on the Hub and is not
        # copied here, so we assert the fixture declares its provenance rather than that the file
        # exists locally.
        self.assertTrue(self.data["renderer"].startswith("markdown-it "))
        self.assertTrue(self.data["generated_by"].endswith(".mjs"))


class EachRuleHasAPositiveAndANegative(unittest.TestCase):
    def test_every_rule_carries_examples(self) -> None:
        """A rule added without examples has no evidence that it can fire or stay quiet."""
        for rule in rules.RULES:
            with self.subTest(rule=rule.id):
                self.assertTrue(rule.positive, f"{rule.id} has no positive example")
                self.assertTrue(rule.negative, f"{rule.id} has no negative example")
                self.assertIn(rule.level, rules.CATEGORY_FOR_LEVEL)
                self.assertTrue(
                    rule.defect, f"{rule.id} does not name the defect it suggests"
                )

    def test_positives_fire(self) -> None:
        for rule in rules.RULES:
            for lang, document in rule.positive:
                with self.subTest(rule=rule.id, document=document[:40]):
                    self.assertIn(rule.id, rule_ids(document, lang))

    def test_negatives_are_silent(self) -> None:
        for rule in rules.RULES:
            for lang, document in rule.negative:
                with self.subTest(rule=rule.id, document=document[:40]):
                    self.assertNotIn(rule.id, rule_ids(document, lang))

    def test_the_fail_tier_is_exactly_the_agreed_four(self) -> None:
        fail = {rule.id for rule in rules.RULES if rule.level == "fail"}
        self.assertEqual(fail, {"D1", "D2", "D5", "D14"})

    def test_rule_ids_are_unique(self) -> None:
        ids = [rule.id for rule in rules.RULES]
        self.assertEqual(len(ids), len(set(ids)))


class NonProseIsExcluded(unittest.TestCase):
    def test_fence_code_span_comment_frontmatter_and_switcher(self) -> None:
        cases = {
            "fence": "```text\n速さが核心です。\nご不明な点があればどうぞ。\n```\n",
            "code span": "例: `速さが核心です。` と `ご不明な点` は書かない。\n",
            "comment": "<!-- 速さが核心です。ご不明な点 -->\n本文。\n",
            "multi-line comment": "<!--\n速さが核心です。\nご不明な点\n-->\n本文。\n",
            "frontmatter": "---\ntitle: 速さが核心です。\n---\n本文。\n",
            "switcher": (
                "<!-- lang-switcher:start -->\n速さが核心です。ご不明な点\n"
                "<!-- lang-switcher:end -->\n本文。\n"
            ),
        }
        for name, document in cases.items():
            with self.subTest(name=name):
                found = rule_ids(document)
                self.assertNotIn("D2", found)
                self.assertNotIn("D5", found)

    def test_d1_ignores_code(self) -> None:
        self.assertNotIn("D1", rule_ids("`" + BROKEN.strip() + "`\n"))
        self.assertNotIn("D1", rule_ids("```text\n" + BROKEN + "```\n"))

    def test_line_numbers_are_those_of_the_file(self) -> None:
        document = "---\ntitle: x\n---\n\n" + BROKEN
        lines = {f.line for f in rules.scan_markdown(document, "ja") if f.rule == "D1"}
        self.assertEqual(lines, {5})


class LanguagesAreKeptApart(unittest.TestCase):
    def test_japanese_only_rule_is_silent_in_english(self) -> None:
        self.assertNotIn("D2", rule_ids("速さが核心です。\n", "en"))

    def test_unscanned_languages_return_nothing(self) -> None:
        self.assertEqual(rules.scan_markdown(BROKEN, "ko"), [])

    def test_language_of(self) -> None:
        self.assertEqual(
            rules.language_of("docs/en/x.md", "日本語の本文です。" * 5), "en"
        )
        self.assertEqual(rules.language_of("docs/ko/x.md", ""), "ko")
        self.assertEqual(rules.language_of("README-ja.md", "English only"), "ja")
        self.assertEqual(
            rules.language_of("guide.en.md", "日本語の本文です。" * 5), "en"
        )
        self.assertEqual(
            rules.language_of("AGENTS.md", "これはひらがなとカタカナの本文です。"), "ja"
        )
        self.assertEqual(rules.language_of("AGENTS.md", "English prose only."), "en")
        # This spoke's two top-level readmes: suffix wins over content.
        self.assertEqual(
            rules.language_of("README.en.md", "日本語の本文です。" * 5), "en"
        )
        self.assertEqual(
            rules.language_of("README.md", "これはひらがなの本文です。"), "ja"
        )


class CopyableCli(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "ai_style_rules.py"), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def test_fail_flag_gates_and_default_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            note = Path(name) / "note.md"
            note.write_text(BROKEN, encoding="utf-8")
            self.assertEqual(self.run_cli(name).returncode, 0)
            self.assertEqual(self.run_cli(name, "--fail").returncode, 1)
            note.write_text(RENDERED, encoding="utf-8")
            self.assertEqual(self.run_cli(name, "--fail").returncode, 0)

    def test_the_corpus_passes_fail_and_a_bracketed_bold_breaks_it(self) -> None:
        """The gate `make ai-style` now runs `--fail` over AI_STYLE_PATHS.

        Assert what that target asserts: the real corpus has no fail-tier finding, and injecting a
        bracketed-bold D1 line into a copy of a corpus file makes `--fail` exit 1. The copy keeps
        the negative control out of the tracked tree.
        """
        paths = ["AGENTS.md", "README.md", "README.en.md", "docs"]
        self.assertEqual(self.run_cli(*paths, "--fail").returncode, 0)
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "docs" / "ja"
            target.mkdir(parents=True)
            source = ROOT / "README.md"
            injected = target / "README.md"
            injected.write_text(
                source.read_text(encoding="utf-8") + "\n" + BROKEN, encoding="utf-8"
            )
            self.assertEqual(self.run_cli(str(injected), "--fail").returncode, 1)

    def test_markers_exclude_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "marked.md").write_text(
                BROKEN.strip() + " <!-- allow:ai-style -->\n", encoding="utf-8"
            )
            (root / "declared.md").write_text(
                "<!-- audit-file-allow: ai-style -->\n" + BROKEN, encoding="utf-8"
            )
            (root / "blog").mkdir()
            (root / "blog" / "post.md").write_text(JA_LEAD + BROKEN, encoding="utf-8")
            self.assertEqual(
                self.run_cli(name, "--fail", "--exclude", "blog/*").returncode, 0
            )
            summary = self.run_cli(name, "--summary")
            self.assertIn("| ai-style | ja | D1 | 2 |", summary.stdout)
            self.assertIn("| blog/post.md | ja | 2 | 2 |", summary.stdout)

    def test_selftest(self) -> None:
        self.assertEqual(self.run_cli("--selftest").returncode, 0)


if __name__ == "__main__":
    unittest.main()
