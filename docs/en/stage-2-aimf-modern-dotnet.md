# Stage 2: migrating to modern .NET on Linux

> Evidence tier: `hypothesis` (not measured). The steps are a plan and the predictions are
> pre-measurement hypotheses, replaced by records.

Using the AI Modernization Flow (hereafter "AIMF", v0.11.0) `dotnetfw-to-modern-dotnet` playbook,
migrate the source app to modern .NET and run it on the Linux EC2 over NFS. The Windows client
stays through stage 3, so the security style is kept NTFS.

## Planned steps

1. Re-check the hook wiring and make `fsxadmin` unreadable from the Linux instance role for stage 2
2. Run two AWS Transform custom transformations (the analysis and, for comparison, the .NET
   transformation) once each, through the single entry (`run-atx.sh`), each after its own approval
3. Work through AIMF Phase 0a to 4 and integrate the confluence points
4. Deploy the migrated code and run the Probe
5. Prepare the security-style ADR and wait for a human decision (no switch in this verification)

## Engine for the AIMF sessions

The AIMF sessions use the default engine of kiro-cli 2.28.0 (V2) in interactive mode. On
2026-10-10, a hook-firing canary on macOS with kiro-cli 2.28.0 showed the following.

- V2 reads only the hooks written in the agent config. A matcher fires on an exact tool name and
  never fired on the regular expression `^(execute_bash|shell|use_aws|aws)$` or on
  `execute_bash|use_aws`. With a JSON list as the matcher, the agent itself failed to load.
  `setup-workspace.sh` wires matchers that name each tool individually
- A hook returning exit 2 stopped the tool call on both V2 and V3
- V2 in non-interactive mode refused to run shell even with `--trust-tools=execute_bash,use_aws`.
  The cause is not confirmed, so non-interactive mode is not used
- V3 (`--v3`) reads hooks from three places (agent config, workspace, global) and also fired on the
  regular-expression matcher, but has no `use_aws` tool. The hook that stops AWS calls would then target a different tool than on V2, so V3 is not
  used in this verification

## Two AWS Transform custom transformations and the comparison plan

> Evidence tier: `hypothesis` (not measured). Neither transformation has been run.

The stage 2 migration itself goes through the AIMF playbook. Two AWS-managed transformations of
AWS Transform custom are used, and the result of `AWS/dotnet-modernization` is recorded for
comparison.

| Transformation | Role in stage 2 | Directory sent | Changes the code |
|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | Analysis for AIMF Phase 0a | `DocIntake/` (the analysis copy, which AIMF does not modify) | No. [Managed Transformations](https://docs.aws.amazon.com/transform/latest/userguide/transform-aws-customs.html) places it in the group that produces reports |
| `AWS/dotnet-modernization` | Comparison | `DocIntake-atx-dotnet/` (a separate copy made from a committed state of `app/legacy/`) | Yes. Per [How to work with the .NET agent](https://docs.aws.amazon.com/transform/latest/userguide/dotnet-work-with-agent.html), the CLI replaces the original code |

On 2026-10-10, `atx custom def list --json` with `atx` 3.18.0 on the macOS workstation listed both
transformations in the ap-northeast-1 registry. Neither transformation was run.

The CLI form for `AWS/dotnet-modernization` follows the .NET page:
`atx custom def exec -n AWS/dotnet-modernization -p <path-to-solution> [-q] [-x] [-t]`. That form
has no build command (`-c`) or configuration file (`-g`), and the default target is net10.0.
`run-atx.sh` runs this transformation only on a separate copy that has at least one commit and no
uncommitted changes. It refuses a path that resolves to the analysis copy and records the commit it
sent in the run log.

The comparison is planned to look at three things: the extent of the rewrite, the build result,
and the agent minutes used.

## Agent-minute caps and cost ceilings

`run-atx.sh` passes the cap recorded in the approved estimate to
`atx custom def exec ... --limit <minutes>`. The cap and the transformation name are read from the
estimate only; no command-line flag or environment variable changes them.
Per `atx custom def exec --help` (3.18.0, checked 2026-10-10), `atx` exits with code 2 when the cap
is reached, and the run can be resumed with a higher cap.
The [AWS Transform pricing page](https://aws.amazon.com/transform/pricing/) says an interrupted
transformation can be resumed up to 24 hours later.
On exit code 2, `run-atx.sh` treats the minutes up to the cap as billed, marks the estimate used, and
stops with exit code 3. Raising the cap needs a new estimate, a new approval, and a new invocation
verification record.

| Transformation | `--limit` | Unit price | Cost ceiling | Expectation before the run |
|---|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | 120 | $0.035 / agent minute | $4.20 | Not measured |
| `AWS/dotnet-modernization` | 300 | $0.035 / agent minute outside the monthly no-cost quota | $10.50 (if run outside the quota) | $0 within the monthly 50,000 agent-minute no-cost quota. The remaining quota is not visible to the estimate script |

The unit price was checked on 2026-10-10 on the pricing page and through the AWS Price List API
(service `AWSTransform`, usage type `APN1-AgentMinute`, Asia Pacific (Tokyo), effective
2026-04-01). The minimum billing increment is 1 minute, and the pricing page excludes builds and
file reads on the workstation from billing.

The caps rest on the following. Both are pre-run hypotheses, to be revisited against the minutes
actually used.

- The sample is about 900 lines of C# (the `.cs` files under `app/legacy/`)
- `AWS/dotnet-modernization`: the pricing page example (a 50k-line .NET Framework solution at about
  2,800 agent minutes, about 0.056 per line) gives about 50 minutes at this size. Allowing for the
  fixed assessment and planning work and for repeated attempts when the build does not pass on the
  workstation, the cap is six times that, 300
- `AWS/comprehensive-codebase-analysis`: the pricing page has no example for this transformation.
  Its examples run from 20 to 72 agent minutes for 3,000 to 17,000 lines, the largest being a Java
  language version upgrade of 17,000 lines at about 72 agent minutes. With no example for this
  transformation, 120, above the largest example, is set as a judgement

## How to choose between the transformations

| Aspect | `AWS/comprehensive-codebase-analysis` | `AWS/dotnet-modernization` |
|---|---|---|
| What you get | A report on the codebase | Rewritten code, plus assessment, plan and transformation reports |
| Effect on the code sent | Not modified | Replaced, so a separate copy that keeps the original is needed |
| Cost | $0.035 / agent minute from the first minute | $0 within the monthly no-cost quota. The remaining quota is not visible, so the cap is estimated at the price outside the quota |
| Workstation prerequisites | The documented form has no build command | Whether .NET Framework 4.8 code builds on macOS is not confirmed. The .NET page recommends the web application for transforming from a Mac and the Visual Studio IDE for confirming a local build |

The first suits understanding the current code without changing it; the second suits obtaining
the transformed code itself. When using the second through the CLI, prepare a copy that keeps the
original code and an environment that can confirm the build first.

## What to confirm at the boundary

- The target volume UUID and the `seed/` inventory are identical to stage 0
- The security style is still `ntfs`
- The recovery queue, clone relationships and `it_*` snapshots are empty
- The Probe differences are compared against stages 0 and 1 and recorded

## The record and publication boundary

The whole AIMF record repository stays in the unpublished work area. Only audited excerpts (tables
and summaries) are published, with file-system IDs, account IDs and IPs replaced and `make audit`
run over them.

## Predictions (unverified)

- AIMF is expected to stop for a human decision rather than autonomously change the security style
  or the path format.
- On migrated Linux the `GetAccessControl` family is unavailable, so the ACL pre-check and the NFS
  result are expected to disagree.
