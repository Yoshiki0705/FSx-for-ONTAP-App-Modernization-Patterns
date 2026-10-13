# Stage 2: migrating to modern .NET on Linux

> Evidence tier: shown per section. The sections on results (the AIMF session, the two AWS Transform
> custom transformations, measurements of the modernized app, implementation pitfalls) are `verified`
> and carry a run id and a date; one-off results say "single observation". The ADR contents are a
> summary of the session record. The "Predictions" section includes `hypothesis`, with the tier
> written per item. Unless stated otherwise the environment is ap-northeast-1, ONTAP 9.19.1P3,
> Single-AZ, one volume `appdata` (security style `ntfs`).

Using the AI Modernization Flow (hereafter "AIMF", v0.11.0) `dotnetfw-to-modern-dotnet` playbook,
migrate the source app to modern .NET and run it on the Linux EC2 over NFS. The Windows client
stays through stage 3, so the security style is kept NTFS.

## Steps and what was done

| # | Step | Status |
|---|---|---|
| 1 | Re-check the hook wiring and make `fsxadmin` unreadable from the Linux instance role for stage 2 | The canary was run on 2026-10-12 with kiro-cli 2.29.1 (next section). The `fsxadmin` block was lifted only while taking the boundary record |
| 2 | Run two AWS Transform custom transformations (the analysis and, for comparison, the .NET transformation) once each, through the single entry (`run-atx.sh`), each after its own approval | Run on 2026-10-11 ("Results of the two AWS Transform custom transformations") |
| 3 | Work through AIMF Phase 0a to 4 and integrate the confluence points | Done in one session on 2026-10-12 ("What the AIMF session produced") |
| 4 | Deploy the migrated code and run the Probe | Done on 2026-10-12 ("Measurements of the modernized app") |
| 5 | Prepare the security-style ADR and wait for a human decision (no switch in this verification) | The user decided on 2026-10-12 to keep `ntfs` ("The security-style decision") |

## Engine for the AIMF sessions

The AIMF session used the default kiro-cli engine (V2) in interactive mode.

- 2026-10-10: a canary that checks hook firing was run with kiro-cli 2.28.0 on macOS.
- 2026-10-12: the same canary was run with kiro-cli 2.29.1 before the session started.

The 2.28.0 canary showed the following.

- V2 reads only the hooks written in the agent config. A matcher fires on an exact tool name and
  never fired on the regular expression `^(execute_bash|shell|use_aws|aws)$` or on
  `execute_bash|use_aws`. With a JSON list as the matcher, the agent itself failed to load.
  `setup-workspace.sh` wires matchers that name each tool individually
- A hook returning exit 2 stopped the tool call on both V2 and V3
- V2 in non-interactive mode refused to run shell even with `--trust-tools=execute_bash,use_aws`.
  The cause is not confirmed, so non-interactive mode is not used. This observation is from 2.28.0
  and was not re-checked on 2.29.1
- V3 (`--v3`) reads hooks from three places (agent config, workspace, global) and also fired on the
  regular-expression matcher, but has no `use_aws` tool. The hook that stops AWS calls would then
  target a different tool than on V2, so V3 is not used in this verification

The 2.29.1 canary result (2026-10-12, single observation):

| Call | Hook verdict |
|---|---|
| A harmless shell command | allow |
| A shell command on the block list | block (the command did not run and its passphrase was not printed) |
| `use_aws` | allow |

The key points for readers who want to reproduce this:

- Start V2 in interactive mode with the AWS credentials isolated by environment variables. Unset
  `AWS_PROFILE` and pass `AWS_CONFIG_FILE=/dev/null`, `AWS_SHARED_CREDENTIALS_FILE=/dev/null` and
  `AWS_EC2_METADATA_DISABLED=true`. After the isolation, `use_aws sts get-caller-identity` failed with
  NoCredentials (single observation).
- The approval prompt offers three choices (allow once / always allow for the session / deny). The
  session chose only "allow once", never "always allow". An approver script approved read and
  record operations on the allowlist one call at a time; anything else was held for the user to decide.
  The approver script itself and the count of approvals are not given in this document.

## What the AIMF session produced

The result of one session on 2026-10-12, from the record (one run).

- Phase 0a was an ingestion of the AWS Transform custom analysis (`AWS/comprehensive-codebase-analysis`)
  already run on 2026-10-11; `atx` was not run inside the session.
- The design decisions are in ADR-1 to ADR-5.

| ADR | Summary of the decision |
|---|---|
| ADR-1 | Target .NET 10 (LTS, `net10.0`). Cover two storage routes, NFS and S3 Access Points. The Lambda handler and its deployment are outside AIMF's scope |
| ADR-2 | Of the four non-functional items, performance and resource leaks are out of scope; resilience and security are covered in a limited way |
| ADR-3 | The `CanWrite` NTFS ACL pre-check keeps the current evaluation on Windows and, on Linux, is decided by the outcome of real I/O, not by POSIX mode bits |
| ADR-4 | `index.json` becomes compact JSON (the `System.Text.Json` default). The success check changes from byte-for-byte to JSON equivalence (keys, values, array order) |
| ADR-5 | `OpenExclusive` on S3 stays `NotSupportedException` and is tracked in ledger item AP-14, because S3 has no exclusive-open operation |

- Independent verification ran once. The missing-feature check was REFUTED and the scope check was UPHELD (1 finding). The finding was handled in ADR-5 and AP-14, with 0 items carried over.
- Negative test: a temporary violating file was injected to confirm the detectors catch AP-1 to AP-13 (13 caught, 0 missed, 2026-10-12).
- The session used about 284 Kiro credits. The user set the cap to 5,000. This document does not
  convert credits into an amount.

## What the harness enforces mechanically and what it does not

| Category | Content |
|---|---|
| Stopped mechanically | Changes to WORM-type irreversible features (`guard_irreversible_ops.py`). Direct `atx` execution that bypasses `run-atx.sh` (`block_direct_atx.py`). Hook firing was confirmed in the canary (allow / block / allow) |
| Not stopped mechanically | AWS changes in general other than irreversible operations. `git push` (there is no hook) |
| Held by operation | The range in the second row is constrained by the user's instructions, the credential isolation, the approver's allowlist, and not configuring a remote on the record repository. No hook stops a general `aws` command |

The isolation was confirmed only within the single canary above. Whether it holds for other tools or
other ways of calling was not checked.

## The two AWS Transform custom transformations and the role of the comparison

The stage 2 migration itself goes through the AIMF playbook. Two AWS-managed transformations of
AWS Transform custom are used, and the result of `AWS/dotnet-modernization` is recorded for
comparison. The main migration is AIMF; the output of `AWS/dotnet-modernization` was not used for it.

| Transformation | Role in stage 2 | Directory sent | Changes the code |
|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | Analysis for AIMF Phase 0a | `DocIntake/` (the analysis copy, which AIMF does not modify) | No. [Managed Transformations](https://docs.aws.amazon.com/transform/latest/userguide/transform-aws-customs.html) places it in the group that produces reports |
| `AWS/dotnet-modernization` | Comparison | `DocIntake-atx-dotnet/` (a separate copy made from a committed state of `app/legacy/`) | Yes. Per [How to work with the .NET agent](https://docs.aws.amazon.com/transform/latest/userguide/dotnet-work-with-agent.html), the CLI replaces the original code |

On 2026-10-10, `atx custom def list --json` with `atx` 3.18.0 on the macOS workstation showed both
transformations in the ap-northeast-1 registry.

The CLI form for `AWS/dotnet-modernization` follows the .NET page:
`atx custom def exec -n AWS/dotnet-modernization -p <path-to-solution> [-q] [-x] [-t]`. That form
has no build command (`-c`) or configuration file (`-g`), and the default target is net10.0.
`run-atx.sh` runs this transformation only on a separate copy that has at least one commit and no
uncommitted changes. It refuses a path that resolves to the analysis copy and records the commit it
sent in the run log.

## Results of the two AWS Transform custom transformations

> Evidence tier: `verified`. 2026-10-11, ap-northeast-1, atx 3.18.0. Each transformation was run
> once, against the same sample app (about 900 to 1,089 lines of `.cs`).

The run ids for running the transformed code on Linux are `s2-20261011T125351Z`,
`s2f-20261011T152320Z` and `s2g-20261011T154203Z`. Costs are estimates (agent minutes × unit price),
not invoiced amounts.

| Item | `AWS/comprehensive-codebase-analysis` | `AWS/dotnet-modernization` |
|---|---|---|
| Run (UTC) | 09:48 to 10:03, exit 0 | 11:05 to 11:25, exit 0 |
| agent minutes | 92.46 / 120 (77% of `--limit`) | 139.00 / 300 (46%) |
| Cost (estimate) | about $3.24 (92.46 × $0.035) | $0 within the no-cost quota; about $4.87 outside it. The remaining quota is not observable |
| Code changes | none (28 files generated in `ATXDocumentation`, `.cs` unchanged) | 11 files, +55 / −162 |
| Content | a technical-debt report; the recommended transformation is `AWS/dotnet-modernization` | `net10.0`, SDK-style csproj, `ConfigurationManager` to `IConfiguration`, `App.config` to `appsettings.json`, `AssemblyInfo` removed |
| Build | not applicable (no code change) | `dotnet build` succeeded on the macOS workstation (0 errors, 7 CA1416 warnings) |

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

| Transformation | `--limit` | Unit price | Cost ceiling | Expectation before the run | Actual (2026-10-11, once) |
|---|---|---|---|---|---|
| `AWS/comprehensive-codebase-analysis` | 120 | $0.035 / agent minute | $4.20 | Not measured | 92.46 minutes |
| `AWS/dotnet-modernization` | 300 | $0.035 / agent minute outside the monthly no-cost quota | $10.50 (if run outside the quota) | $0 within the monthly 50,000 agent-minute no-cost quota. The remaining quota is not visible to the estimate script | 139.00 minutes |

The unit price was checked on 2026-10-10 on the pricing page and through the AWS Price List API
(service `AWSTransform`, usage type `APN1-AgentMinute`, Asia Pacific (Tokyo), effective
2026-04-01). The minimum billing increment is 1 minute, and the pricing page excludes builds and
file reads on the workstation from billing.

The caps rested on pre-run hypotheses, which were compared with the actual minutes after the run.

- The sample is about 900 lines of C# (the `.cs` files under `app/legacy/`). The analysis transformation took 92.46 minutes for about 900 to 1,089 lines. This is a single observation and does not carry over to other samples
- `AWS/dotnet-modernization`: the pricing page example (a 50k-line .NET Framework solution at about
  2,800 agent minutes, about 0.056 per line) gives about 50 minutes at this size. Allowing for the
  fixed assessment and planning work and for repeated attempts when the build does not pass on the
  workstation, the cap is six times that, 300. The actual was 139.00 minutes, above the roughly 50-minute expectation and below the cap of 300
- `AWS/comprehensive-codebase-analysis`: the pricing page has no example for this transformation.
  Its three non-.NET examples run from 20 to 72 agent minutes for 3,000 to 17,000 lines (the .NET
  example, about 500,000 lines at about 25,000 agent minutes, is a different order of magnitude and
  is not used as a small-codebase guide). With no example for this transformation, 120, above the
  largest example, was set as a judgement. The actual was 92.46 minutes

## How to choose between the transformations

| Aspect | `AWS/comprehensive-codebase-analysis` | `AWS/dotnet-modernization` |
|---|---|---|
| What you get | A report on the codebase | Rewritten code, plus assessment, plan and transformation reports |
| Effect on the code sent | Not modified | Replaced, so a separate copy that keeps the original is needed |
| Cost | $0.035 / agent minute from the first minute | $0 within the monthly no-cost quota. The remaining quota is not visible, so the cap is estimated at the price outside the quota |
| Workstation prerequisites | The documented form has no build command | Whether .NET Framework 4.8 code builds on macOS was not measured before the transformation. The transformed `net10.0` code built on macOS (2026-10-11). The .NET page recommends the web application for transforming from a Mac and the Visual Studio IDE for confirming a local build |

The first suits understanding the current code without changing it; the second suits obtaining
the transformed code itself. When using the second through the CLI, prepare a copy that keeps the
original code and an environment that can confirm the build first.

## Comparing AWS Transform custom and AIMF, and how to choose

> Each is a single run against the same sample app. The AIMF side used additional inputs (the
> analysis, the ADRs and the ledger), so this is not a like-for-like comparison. Costs in different
> units are not converted to an amount and set side by side.

| Aspect | `AWS/dotnet-modernization` (atx) | AIMF (`dotnetfw-to-modern-dotnet`) |
|---|---|---|
| Cost unit | agent minutes (139.00 minutes; the estimate is $0 within the no-cost quota, about $4.87 outside it) | Kiro credits (about 284) |
| Elapsed time | about 20 minutes (11:05 to 11:25 UTC) | One session on 2026-10-12. It includes time for human approvals and questions, so elapsed time is not set alongside |
| Output | 11 files rewritten. Five embedded behaviors remained (next section) | Ledger AP-1 to AP-14, ADR-1 to ADR-5, `NfsFileStore` and `S3FileStore`, and the independent verification record |
| First run on Linux | some behaviors `error` (`s2-20261011T125351Z`); `measured` after manual fixes (`s2f`, `s2g`) | all 19 Probe records `measured`, 0 `error` (`s2m-20261012T090934Z`) |
| What it needs beforehand | A separate copy that keeps the original code. A workstation that can confirm the build | Session design, user approvals, credential isolation, Kiro credits |
| Constraints | In the single 2026-10-11 run, no NFS store implementation was added (item 5 in the next section). Billing is in agent minutes | It needs human judgement and approval time. It uses credits |

The choice depends on the use. If you want the transformed code with one command and plan to fix it
up yourself, atx `AWS/dotnet-modernization` suits. If you want the migration decisions (ADRs) and the
verification record kept together, or need a storage layer added, AIMF suits. Either way, you need a
copy that keeps the original code and a procedure to check the result in a real environment. This
document is an observation on one sample and does not predict the results or costs for other apps.

## Implementation pitfalls and fixes

> `verified`, once each. The atx-derived code was checked in `s2-20261011T125351Z`,
> `s2f-20261011T152320Z` and `s2g-20261011T154203Z` (2026-10-11), and the AIMF-derived code in
> `s2m-20261012T090934Z` (2026-10-12).

The five items atx `AWS/dotnet-modernization` did not fix, and what to do:

| # | What remained | What to do |
|---|---|---|
| 1 | ACL evaluation. `File.GetAccessControl` was only replaced with `FileInfo.GetAccessControl`, which does not work on Linux. The Linux run raised `PlatformNotSupportedException` | On Linux, decide by the outcome of real I/O, not by an ACL pre-check |
| 2 | `StorePaths.Combine` joins with `\`. On Linux `/mnt/appdata\probe\...` became one file name and pointed at the local filesystem directly under `/mnt`, outside the mount | Use `Path.Combine` instead of joining separators as strings. `measured` does not always mean the write reached the share, so check where it was written |
| 3 | `FileShare.None` | Keep it only after measuring that exclusion works on Linux NFS and cifs. It was kept deliberately here (ledger AP-4) |
| 4 | The reference to `Docs\Index.json` | Differences in case and separators appear per access path. Pin the reference to a real file and carry a positive control |
| 5 | `NfsFileStore` was not added (it stayed `SmbFileStore`) | Provide a separate store implementation for NFS |

The three fixes AIMF made:

- Path joining now uses `Path.Combine` (ledger AP-1)
- `CanWrite` now decides by the outcome of real I/O (ledger AP-3, ADR-3)
- The case-sensitivity reference was pinned under `seed/` and a positive control was added to the record (ledger AP-2)

In the manual fix of the atx-derived code (2026-10-11, `s2f-20261011T152320Z`), `Path.Combine`,
`NfsFileStore` and real-I/O `CanWrite` were applied, and four behaviors on Linux (NFS and cifs)
became `measured`. cifs rejects a name containing `\` with `errno=22` (`EINVAL`) and .NET turns this
into an `IOException`, so the Probe's handling was changed in `s2g-20261011T154203Z` (it records
`rejected=true` and `hresult`).

## Measurements of the modernized app

> `verified`, once each. Run ids `s2m-20261012T090934Z`, `s2n-20261012T104314Z` and
> `s0w-20261012T102314Z` (all 2026-10-12). ONTAP 9.19.1P3, Single-AZ, one volume. The NFS runs used
> UNIX uid 10001 (root is squashed to anonymous and denied). The Windows-side Probe was the earlier
> build (.NET Framework 4.8) and the Linux side the modernized build (net10.0).

- All 19 Probe records (3 standalone, 16 in pairs) were `measured`, with 0 `error`. The case-sensitivity
  positive control (`real_resolved`) was true on all three routes: Windows, Linux cifs and Linux NFS.
- `probe_merge.py` judged all 8 cross-host pairs `cross-host` (`reasons` empty). In the 4 file-locking
  pairs the contender was refused and succeeded after release. The 4 write-visibility pairs had
  `seen: true`: Windows writes → Linux reads took 66 ms and 35 ms, and Linux writes → Windows reads
  took 7,968 ms and 8,342 ms.
- The Worker exited 0 and wrote 5 reports and `out/s2m/index.json`. Compared with `out/s0/index.json`,
  made by running the earlier Windows Worker once on the same volume (`s0w-20261012T102314Z`), the keys,
  values and array order matched (JSON equivalence). The byte counts were 201 and 181; the difference
  was only 20 bytes of separator whitespace (the difference anticipated in ADR-4). The sha256 of the 5
  reports matched, and the individual hashes of the 6 `seed/` files were unchanged before and after.
  The array order was ascending by name in 5 files and one observation. Whether `Directory.GetFiles` or
  SMB guarantees name order was not checked.
- The re-measurement that aligned the acl-evaluation record shape with ADR-3 (`s2n-20261012T104314Z`)
  gave, on both Linux NFS and cifs, `precheck` `skipped`, a non-empty `precheck_reason` and
  `io_can_write` true, and `precheck_can_write` no longer appears in the record.
- What to do: compare `index.json` by JSON equivalence, not by bytes. When the output changes to the
  `System.Text.Json` default (compact), the bytes change but the keys, values and array order do not.

Not measured in this section: the false side of acl-evaluation (no value was observed for a case where
writing is not allowed; all three routes were true, once), and re-observing an NFS name containing `\`
with the modernized Probe (the modernized Probe writes no name containing `\`, so `exists_literal`
only quotes the 2026-10-07 observation of the Python version).

## The security-style decision

`ntfs` is kept. Decision: user (the user decided on 2026-10-12; the AI did not decide). This is a
design decision, not a legal, compliance or regulatory judgement.

| Aspect | Keep `ntfs` | Switch to `unix` | Switch to `mixed` |
|---|---|---|---|
| Clients that can change permissions | SMB | NFS | NFS or SMB. The type of the last client to change takes effect |
| When it fits | While Windows clients remain. When the NTFS ACL stays the authority for permissions | After Windows clients are retired. When permissions are managed with UNIX tools | When administrators on both sides must write permissions to the same data |
| How it looks from Linux | The `ls` mode display does not indicate whether access is allowed (confirmed in this environment) | Mode bits indicate whether access is allowed (general statement from documentation; untested in this environment) | The type is mixed per file (general statement from documentation; untested in this environment) |
| Constraints | Linux `chmod` and `chown` are not a way to change permissions. Whether Linux is allowed depends on name mapping and the ACL of the Windows principal | While Windows clients remain, the way ACLs are managed changes. Equivalence of permissions before and after the switch is unverified | Permission types are mixed and effective permission is decided per file. Behavior in this environment is untested |

- How to choose: while Windows clients remain and you want the NTFS ACL to stay the authority for
  permissions, `ntfs` suits. Once Windows clients are gone and you want to manage permissions with UNIX
  tools, `unix` suits. Consider `mixed` only when operations require changing permissions from both
  protocols.
- Tiers: the per-route behavior under `ntfs` (case sensitivity, visibility delay, pre-check, NFS root
  denial) is `verified` (the sections above). The general effects of the security styles are `documented`
  ([Learn about ONTAP NAS security styles](https://docs.netapp.com/us-en/ontap/nfs-admin/security-styles-their-effects-concept.html),
  opened and checked on 2026-10-12; a summary, and version-specific differences were not checked). There
  is no result from trying `unix` or `mixed` in this environment, so behavior here is `hypothesis` (untested).
- This document does not claim that the per-route differences (case sensitivity, visibility delay,
  pre-check) go away when the security style changes. They were not tried, so that cannot be said.
- Conditions to revisit: when no Windows clients use the target volume, when a request arises to
  consolidate permission management onto Linux tools, when handling Multi-AZ, another ONTAP version or
  another volume, or when maintaining name mapping becomes an operational burden.
- A switch is treated as a separate step after this verification. Switching with data on the volume,
  and what happens to the existing NTFS ACLs, are untested.

## What to confirm at the boundary

- The target volume UUID and the `seed/` inventory are identical to stage 0
- The security style is still `ntfs`
- The recovery queue, clone relationships and `it_*` snapshots are empty
- The Probe differences are compared against stages 0 and 1 and recorded

Results (`verified`):

- The 2026-10-11 boundary `b2` passed (the UUID, security style, `seed/` inventory and top-level paths
  matched `b0`, and `check-no-locking` also passed).
- In the 2026-10-12 `b2` (`s2m-20261012T090934Z`) and `b3`, the 8.3 short name `PROBES~1.TXT` appeared at
  the top level and `check-invariant.py` failed. The cause was a file the S3 Probe of
  [stage 3](stage-3-s3-access-points.md) created directly under the volume root; stage 3 covers it.
  The UUID, security style, `seed/` inventory, export policy, snapshots and snapshot locking values
  were unchanged, and `check-no-locking.sh` passed.

## Cost estimates and observed costs

The table below is estimates (unit price × quantity), not invoiced amounts. The observed amounts are values read from Cost Explorer on 2026-10-13, with `Estimated: true` (before billing closes) and Credit and Refund excluded. No invoice was checked.

| Item | Estimate | Note |
|---|---|---|
| Base (approval for 72 hours) | $57.71 (approved 2026-10-11T02:07:07Z) | Torn down after about 33.6 hours. The estimate prorated to 33.6 hours and the tag-attributed observed amount of $21.18 (excluding AD) are in [Verification environment](environment.md) |
| atx analysis transformation | about $3.24 | The ceiling estimate is $4.20 |
| atx `AWS/dotnet-modernization` | $0 within the no-cost quota, about $4.87 outside it | The ceiling estimate is $10.50. The remaining quota is not observable |

The Kiro credits of the AIMF session (about 284) are not converted to an amount.

The observed AWS Transform amount cannot be attributed by tag the way the base can. It is a service-level value, and the
account is shared with other uses, so it cannot be asserted to be this verification's cost alone. Cost Explorer showed AWS Transform at
3.62215 USD on 2026-10-11 (0 on 2026-10-12). Divided by the rate of $0.035 per agent minute, that is 103.49 minutes.
atx's own records are 92.46 minutes for the analysis and 139.00 minutes for dotnet-modernization (231.46 minutes in total),
and 103.49 minutes matches neither 92.46 nor 231.46. The difference is unexplained. The scope of the free tier or billing rounding are possible
causes, and neither was checked. The 3.62215 USD is within the estimated ceilings (analysis $4.20, dotnet $10.50).
It is one observation and not an invoice.

## The record and publication boundary

The whole AIMF record repository stays in the unpublished work area. Only audited excerpts (tables
and summaries) are published, with file-system IDs, account IDs and IPs replaced and `make audit`
run over them.

## Predictions and what is untested

- AIMF is expected to stop for a human decision rather than autonomously change the security style
  or the path format. `hypothesis`. The session neither proposed nor performed a security-style
  switch, but there is no direct record showing that it "stopped".
- That the `GetAccessControl` family is unavailable on migrated Linux is `verified` (the atx-derived
  code raised `PlatformNotSupportedException`, `s2-20261011T125351Z`, once). That the ACL pre-check and
  the actual result over NFS disagree stays `hypothesis`: `io_can_write` was true on three routes and
  matched the real NFS write, but a case where the write is not allowed (the false side) was not observed.
- Not measured: security style `unix` and `mixed`, ONTAP versions other than 9.19.1P2 and 9.19.1P3,
  Multi-AZ, other volume layouts, whether `Directory.GetFiles` guarantees return order, the cause of
  the roughly 8-second delay when Linux writes and Windows reads, and invoiced amounts.
