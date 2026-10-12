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
| `drift` | `tools/check_make_targets.py` | Every `make` target named in code spans or code blocks of `AGENTS.md` and `CONTRIBUTING.md` has a rule in the Makefile; a missing file is skipped |
| `test` | unittest, `--selftest` | Detector tests, the irreversible-operation guard, and the audit's own rules |

`make pr-verify PR=<number>` is the merge gate and is not part of `make all`, because it needs
`gh` and a pushed pull request. It reads the PR's head SHA, lists the workflow runs for that SHA
(`tools/pr_verify.py`), and exits 0 only when every workflow that triggers on `pull_request`
completed with success. A workflow with no run for the SHA counts as a failure, since the
latest-results view of `gh pr checks` can return the previous commit's verdict. Exit 1 is a
failure or a missing run, exit 2 is "still running", and `WAIT=<seconds>` polls every 30 seconds.
A workflow with a `paths:` filter is required only when a changed file matches it.

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
