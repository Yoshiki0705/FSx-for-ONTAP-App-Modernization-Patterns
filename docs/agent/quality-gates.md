# Quality gates

Every gate is a `make` target, and CI calls the same targets, so a local run and a CI run inspect
the same tree with the same tool versions. `make all` runs everything and is the commit gate.

| Target | Tool | What it checks |
|---|---|---|
| `markdown` | markdownlint-cli2 (`.markdownlint-cli2.jsonc`) | Markdown style over `**/*.md` |
| `headings` | `tools/check_heading_style.py` | Japanese section headings are noun phrases |
| `role-labels` | `tools/check_role_labels.py` | No callout label claims a job title or persona |
| `ai-style` | `tools/ai_style_rules.py` | Writing-style signals; fail tier only |
| `diagram-fonts` | `tools/check_diagram_fonts.py` | Label size in `docs/_assets/diagrams/*.drawio` and the exported SVGs meets the readability floor |
| `diagram-flow` | `tools/check_diagram_flow.py` | Diagrams read rightwards and downwards, and labels sit under their icons |
| `table-sums` | `tools/check_table_sums.py` | A table marked with `sum-check` has a stated total equal to the sum of its rows (Decimal; strict at the total's own precision). Unmarked tables are not read |
| `python` | ruff (pinned in `requirements-dev.txt`) | Lint and format of `tools/` and `scripts/`; fails when the ruff on `PATH` is not the pinned version |
| `shell` | shellcheck | Every tracked `*.sh` |
| `cfn` | cfn-lint and cfn-guard | Every template under `templates/`, found recursively, against rules in `guard/` |
| `audit` | `tools/audit_public_output.py`, then `secrets` | Naming, neutrality, and PII over files git would commit |
| `secrets` | gitleaks (`.gitleaks.toml`) | Secrets in the working tree; CI scans full history |
| `links` | `tools/check_links.py` | Internal Markdown links resolve |
| `drift` | `tools/check_make_targets.py` | Every `make` target named in code spans or code blocks of `AGENTS.md` and `CONTRIBUTING.md` has a rule in the Makefile; a missing file is skipped |
| `inbound-probes` | `tools/check_inbound_probes.py` | Each string a sibling repository pins in this repository's files (listed in `docs/agent/inbound-probe-contract.txt`) occurs exactly once, in body text, in the file it is pinned to. Absent, quoted a second time, only in a heading, only in a code fence, and a missing file all fail |
| `app-modern` | dotnet SDK 10 | `dotnet build` and `dotnet test` of `app/modern/DocIntake.sln` in Release; fails when the SDK is missing or is not version 10 |
| `test` | unittest, `--selftest` | Detector tests, the irreversible-operation guard, and the audit's own rules |

`make pr-verify PR=<number>` is the merge gate and is not part of `make all`, because it needs
`gh` and a pushed pull request. It reads the PR's head SHA, lists the workflow runs for that SHA
(`tools/pr_verify.py`), and exits 0 only when every workflow that triggers on `pull_request`
completed with success. A workflow with no run for the SHA counts as a failure, since the
latest-results view of `gh pr checks` can return the previous commit's verdict. Exit 1 is a
failure or a missing run, exit 2 is "still running", and `WAIT=<seconds>` polls every 30 seconds.
A workflow with a `paths:` filter is required only when a changed file matches it.

`make inbound-probes-refresh` is not part of `make all` either, because it needs `gh` and the
network. It rewrites `docs/agent/inbound-probe-contract.txt` from the
`docs/agent/cross-repo-probe-contract.txt` that each sibling repository named in our Markdown
publishes, and keeps only the rows pinned here. Only a 404 records a sibling as unknown; any other
failure (rate limit, 5xx, timeout, no `gh`) stops the refresh before it writes, because recording
a failed fetch as "publishes no contract" would delete that sibling's rows. Coverage is a floor: a
string nobody published is not in the file. Refresh before editing a document the file names, and
read the diff before committing.

A table opts in to `table-sums` with a marker on the line directly above its header row. `cols=` lists
the 1-based numeric columns to verify. `tol=` (optional) accepts a total stated after rounding, within
that absolute difference; without it the sum must equal the total at the total's own decimal places.
`total=` (optional, last parameter) names the total row: `last` (default) or `row:<first-cell-text>`.
Rows whose first cell is wrapped in parentheses are left out of the sum, and cells with no digit
(`-`, `n/a`) are skipped and listed in the failure message. A marker with no table after it, an
unparsable table, or a non-numeric stated total fails. Mark only a table whose total equals the sum
of the rows above it; a comparison of estimate and observed columns, or a total that covers only some
rows, stays unmarked. The scan covers `docs/**/*.md`, `README.md`, `README.en.md`, and
`app/modern/README.md`, and ignores fenced code, so this example is not read as a marker:

```markdown
<!-- sum-check: cols=2,3 -->
| Item | Jan (USD) | Feb (USD) |
|---|---|---|
| Compute | 12.03 | 11.50 |
| Storage | 6.36 | 6.40 |
| Subtotal | 18.39 | 17.90 |
```

`app/legacy` (.NET Framework 4.8) is not built by any gate, because it does not build on the Linux CI runner.
Each detector runs its selftest before scanning, so a rule that stopped matching fails the gate
instead of reporting an empty result as a pass.

`tools/check_links.py`, `tools/frontmatter.py`, `tools/ai_style_rules.py`,
`tools/check_diagram_fonts.py`, `tools/check_diagram_flow.py`, and `tools/probe_strength.py` are
copied from the Hub (`probe_strength.py` byte for byte, so the exactly-once rule is the Hub's, not
a second reading of it); `tools/check_inbound_probes.py` is written for this repository after the
Hub's tool of the same name. `tools/check_heading_style.py`, `tools/check_role_labels.py`, and `tools/test_ai_style.py`
from the Container-Datastore Spoke. `scripts/guard_irreversible_ops.py` is a byte-identical copy
of the Hub's guard.

## Known pitfalls

| Pitfall | Resolution |
|---|---|
| A target named after an existing directory silently does nothing | Every target is declared `.PHONY` |
| A failing gate hidden by a pipe | Read the exit status directly: `make all > /tmp/all.log 2>&1; echo $?` |
| A one-level glob skips templates one directory deeper | `cfn` finds templates recursively |
| cfn-lint warnings read as a pass | Any non-zero exit fails the gate |
| A hand-added total can be wrong by a cent | Mark the table with `<!-- sum-check: cols=N -->` so the gate re-adds it |

Running `--help` on a script that does not parse arguments runs it. A script with no argument
handling treats `--help` as a normal invocation, so reading its usage can create resources. Every
executable script under `scripts/` handles `-h` and `--help` before any side effect and rejects an
unknown argument with exit 2. [`scripts/tests/check_help_is_safe.py`](../../scripts/tests/check_help_is_safe.py)
enforces this: it runs each script with `--help`, `-h`, and a bogus flag against stub `aws`, `ssh`,
`curl`, `gh`, and `scp` commands, and fails when a stub is called, a file appears, or the usage is
missing. A new executable script not covered by it fails the gate.
