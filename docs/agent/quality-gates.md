# Quality gates

Every gate is a `make` target, and CI calls the same targets, so a local run and a CI run inspect
the same tree with the same tool versions. `make all` runs everything and is the commit gate.

| Target | Tool | What it checks |
|---|---|---|
| `markdown` | markdownlint-cli2 (`.markdownlint-cli2.jsonc`) | Markdown style over `**/*.md` |
| `headings` | `tools/check_heading_style.py` | Japanese section headings are noun phrases |
| `role-labels` | `tools/check_role_labels.py` | No callout label claims a job title or persona |
| `ai-style` | `tools/ai_style_rules.py` | Writing-style signals; fail tier only |
| `python` | ruff (pinned in `requirements-dev.txt`) | Lint and format of `tools/` and `scripts/`; fails when the ruff on `PATH` is not the pinned version |
| `shell` | shellcheck | Every tracked `*.sh` |
| `cfn` | cfn-lint and cfn-guard | Every template under `templates/`, found recursively, against rules in `guard/` |
| `audit` | `tools/audit_public_output.py`, then `secrets` | Naming, neutrality, and PII over files git would commit |
| `secrets` | gitleaks (`.gitleaks.toml`) | Secrets in the working tree; CI scans full history |
| `links` | `tools/check_links.py` | Internal Markdown links resolve |
| `test` | unittest, `--selftest` | Detector tests, the irreversible-operation guard, and the audit's own rules |

Each detector runs its selftest before scanning, so a rule that stopped matching fails the gate
instead of reporting an empty result as a pass.

`tools/check_links.py`, `tools/frontmatter.py`, and `tools/ai_style_rules.py` are copied from the
Hub; `tools/check_heading_style.py`, `tools/check_role_labels.py`, and `tools/test_ai_style.py`
from the Container-Datastore Spoke. `scripts/guard_irreversible_ops.py` is a byte-identical copy
of the Hub's guard.

## Known pitfalls

| Pitfall | Resolution |
|---|---|
| A target named after an existing directory silently does nothing | Every target is declared `.PHONY` |
| A failing gate hidden by a pipe | Read the exit status directly: `make all > /tmp/all.log 2>&1; echo $?` |
| A one-level glob skips templates one directory deeper | `cfn` finds templates recursively |
| cfn-lint warnings read as a pass | Any non-zero exit fails the gate |
