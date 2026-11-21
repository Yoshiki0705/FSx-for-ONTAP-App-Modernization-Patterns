# Long-running session discipline (this repository)

The substance lives in global Kiro steering (`ai-agent-workflow` — the Parallel-by-default and
Auditor Sub-Agent sections — and `benchmark-and-evidence-standards`), which applies to every
session. This file is not a copy of it. It records only the three connections that are specific to
this repository: where a decision record goes, which evidence words it uses, and the task types
that recur here.

## Where a decision record goes

A multi-step task (a new pattern, a verification run, an IaC change) keeps a decision record with
three sections, kept together in one file, not split across files and not duplicated into a second
"template" of the same thing:

```markdown
## Decisions
- <what was decided>: <why; evidence is measurement / documentation / assumption>

## Rejected alternatives
- <alternative not taken>: <why; could it return if circumstances change>

## Assumptions
- <taken as given without checking>: <what breaks if it is wrong>
```

Placement follows this repository's existing habit, not the Hub's:

- `.agents/` is gitignored here (see `.gitignore`), and it already holds per-task records under
  `.agents/tasks/<task>/` (`impl-note.md`, `plan.md`, `report.md`). Put the decision record there.
- If a task has an existing record file, append the three sections to it. Do not add a new name
  (`impl-note.md` is the usual name, but match whatever the task already uses).
- A per-task decision record is process metadata (rejected options, untested assumptions). It
  stays on the gitignored side (`.agents/` or `.private/`), never in a committed file.

A reusable template or checklist — like the task-type lists below — belongs in this file, which is
tracked. The audit (`make audit`) runs over tracked files, so this file carries no PII, names, or
internal IDs.

## Evidence words

When a decision's basis is a verification result, tag it with the tier this repository already
uses. [AGENTS.md](../../AGENTS.md) defines them; do not invent a fifth word.

| Tier | When |
|---|---|
| `verified` | A reproduced run for the named slice and recorded environment (date, region, ONTAP version, configuration) |
| `field-observation` | A one-off observation, reproduction not confirmed; do not generalize |
| `documented` | A declaration in a repository artifact or cited source, not a runtime execution |
| `open/not measured` | Unexecuted, or outside the verified slice; kept apart from every evidence label |
| `hypothesis` | An explicitly untested expectation; never upgrades the tiers above |

There is no `check_verification_ledger.py` gate here. The tier words live in prose and in the
decision record, and `make audit` plus the detector selftests are what run.

## When to call an auditor

Before continuing, not after, call `semantic_reviewer` (re-examine the judgement) or
`context-gatherer` (check the assumptions against the implementation) when any of these appear, and
hand it the decision record and the diff:

- the same approach has failed twice,
- the same file has been re-edited three or more times,
- the same error message recurs across a multi-step task.

Point the auditor to doubt, not to reinforce: which bases in this record are assumption, which
rejected alternative should return now, which untested assumption would waste the work if it is
wrong. This repository has no evidence-ledger or ADR to host the review, so the auditor's output
goes in the task's decision record. A pre-commit review of public output is the persona review
board's job; keep it separate from the auditor.

Do not add an automatic hook for this.

## Measure variance before claiming an effect

Before claiming that a change moved a performance, throughput, latency, or cost number, measure the
run-to-run spread of the unchanged configuration first. If the change's difference sits inside that
spread, it is noise: say so, or raise the repetition count until the difference clears the spread. A
single "A is faster than B" with one run each gets the same treatment.

This repository's measurement scripts are under `scripts/measure/`. Note that `r1`/`r2` in
`pairs.py` name the two roles of a host pair, not repeated runs, so a reader must not read them as
run 1 and run 2. There is no variance-specific naming here yet, on purpose: it is introduced when a
multi-step task actually measures a spread, shaped to that measurement (how many repetitions, what
is held fixed), and the introduction is recorded in that task's `## Decisions` with its basis.
Naming repetitions ahead of a real measurement would be rework, so it is not done now. When you add
repeated runs, name them so a reader can tell a repetition from a role.

## Recurring task types (grow one line at a time)

A second failure of the same kind is a sign this checklist is missing a line. Add the line, do not
just fix the instance.

### Adding a runtime candidate (app/candidate-*)

- [ ] A gate target exists (`app-candidate-*` in `ci`), and `check_test_coverage.py` sees the tests
- [ ] The three-section decision record names why this candidate over the rejected shapes

### A real-environment verification run

- [ ] Deployment approved this time, after showing configuration, duration, and cost estimate
- [ ] A dedicated file system, not a shared one, for anything irreversible
- [ ] Artifacts that teardown destroys (witness-bucket objects, files on a disposable volume) are
      exported to the record **before** teardown — otherwise the claim cannot stay `verified`
- [ ] A handler fix found during the run is committed, not only claimed to be "in PR #N"
- [ ] The tier is downgraded, not held at `verified`, when an artifact could not be exported

### An IaC change under templates/

- [ ] `make cfn` passes (cfn-lint and cfn-guard, recursive)
- [ ] No immutability/WORM feature enabled without an explicit human instruction naming the value
