# FSx-for-ONTAP-App-Modernization-Patterns

[日本語](README.md) | **English**

Amazon FSx for NetApp ONTAP keeps the data in place while one purpose-built `DocIntake` console application moves from .NET Framework 4.8 / Windows / SMB to .NET 10 / Linux / NFS, and then to .NET 10 managed-runtime Lambda / FSx for ONTAP S3 Access Points.
The application input and output remain in the `appdata` volume on FSx for ONTAP; the application side and the protocol side change one stage at a time.

> **Status**: Execution records use `verified`, one-off observations without confirmed reproduction use `field-observation`, template and test declarations use `documented`, and unexecuted or out-of-scope items use `open/not measured`. Values and behavior described here are sample runs in this verification configuration, not production performance, SLOs, service limits, or billing estimates.

## Status and evidence boundary

| Subject | Evidence label | Scope of the statement |
|---|---|---|
| Execution records for stages 0 to 3 | `verified` | Execution records for `DocIntake`. Each stage's configuration and unexecuted operations remain separate. |
| ONTAP 9.19.1P2 record | `verified` | Initial build record from 2026-10-07. |
| ONTAP 9.19.1P3 record | `verified` | Rebuild record from 2026-10-11 to 12. It is a separate historical record from P2. |
| One-off supporting observations | `field-observation` | Observations without confirmed reproduction. They are not generalized into service limits or general performance values. |
| Template declarations | `documented` | `provided.al2023` is an option declared in the template, but it was not executed. EventBridge Scheduler is declared disabled. |
| Fake S3 tests | `documented` | The tests use an in-memory fake. They do not prove real FSx for ONTAP S3 Access Points behavior. |
| Unexecuted or out-of-scope items | `open/not measured` | Listed in the unverified scope below. |

## Current coverage

| Subject | Evidence label | Current boundary |
|---|---|---|
| Application under test | `verified` | One purpose-built `DocIntake` console application. The coverage does not include a web layer or another runtime. |
| Starting slice | `verified` | .NET Framework 4.8 / Windows / SMB. |
| Stage 1 | `verified` | SMB plus NFS on the same `appdata` volume. The security style remains `ntfs`. |
| Stage 2 | `verified` | .NET 10 / Linux / NFS. The comparison is an old Windows build versus the modernized Linux build; it is not Windows .NET 10 verification. |
| Stage 3 | `verified` | .NET 10 managed-runtime `dotnet10` Lambda / FSx for ONTAP S3 Access Points. Invocation was manual; EventBridge Scheduler was disabled and its enabled execution is `open/not measured`. |
| Stage 3 file locking | `open/not measured` | Exclusive-open/file-locking was skipped in the current `DocIntake` S3 adapter slice. This makes no universal S3 file-locking claim. |

### Data destinations

| Data | Evidence label | Destination |
|---|---|---|
| Application input and output | `verified` | The `appdata` volume on FSx for ONTAP. |
| Aggregate Probe JSON | `verified` | A separate artifacts bucket, distinct from the `appdata` input and output. |
| Lambda logs | `verified` | CloudWatch Logs. |

## Scope

| In scope | Out of scope |
|---|---|
| Moving one purpose-built `DocIntake` console application from .NET Framework 4.8 / Windows / SMB to .NET 10 / Linux / NFS, and then to .NET 10 managed-runtime Lambda / FSx for ONTAP S3 Access Points | Data migration from on-premises or VMware itself, and behavior of Java, Python applications, Python Lambda, other unverified Lambda runtimes, Node.js, Go, ASP.NET, ASP.NET Core, Web API, or Web UI |
| Changing the application runtime and access protocol in stages while the FSx for ONTAP `appdata` volume stays shared | ECS / EKS, Multi-AZ, `unix` / `mixed` security styles, and other unverified runtime or topology claims |
| Per-stage CloudFormation templates, verification scripts, and a purpose-built sample application | Container runtime verification. Containerization is a future branch owned by the adjacent Spoke. |
| FSx for ONTAP-specific supplements for moving .NET Framework to modern .NET with AI Modernization Flow (AIMF) | Copies of the AIMF procedure or verification of other runtimes published by AIMF |

## Stages

Each stage assumes the previous one, and volume data is never moved.
The volume keeps the NTFS security style for as long as Windows clients remain.

| Stage | Application runtime | Access protocol | Main change |
|---|---|---|---|
| 0 | .NET Framework 4.8 on Amazon EC2 (Windows) | SMB (`ntfs` security style, SVM joined to AWS Managed Microsoft AD) | Reproduce the starting configuration |
| 1 | The same `DocIntake` as stage 0 | SMB plus NFS (multiprotocol) | Read and write the same `appdata` volume over NFS |
| 2 | .NET 10 on Amazon EC2 (Linux) | NFS | Compare the old Windows build with the modernized Linux build. This is not Windows .NET 10 verification |
| 3 | .NET 10 managed-runtime Lambda (`dotnet10`) | FSx for ONTAP S3 Access Points | Handle part of the processing through manual invocation. EventBridge Scheduler is disabled, and file locking is skipped in the current slice |
| Branch | Containers | NFS / SMB / S3 Access Points | A future navigation and ownership path. Container runtime behavior is `open/not measured`; see the adjacent Spoke for details |

## Document index

| To read about | Go to |
|---|---|
| Reproducing the verification in your own environment, in order (prerequisites, estimate and approval, stages 0 to 3, teardown, what cannot be reproduced) | [Reproduction guide](docs/en/reproduce.md) |
| Stage 0: the SMB and NTFS starting point | [Stage 0](docs/en/stage-0-smb-ntfs.md) |
| Stage 1: adding NFS to the same volume | [Stage 1](docs/en/stage-1-multiprotocol.md) |
| Stage 2: migrating to modern .NET on Linux (includes the AIMF session record) | [Stage 2](docs/en/stage-2-aimf-modern-dotnet.md) |
| Stage 3: partial serverless via S3 Access Points | [Stage 3](docs/en/stage-3-s3-access-points.md) |
| Lessons from the verification method (a measurement without a positive control is not a result, and others) | [Verification method lessons](docs/en/verification-method-lessons.md) |
| Environment structure and cost estimates | [Verification environment](docs/en/environment.md) |
| Sample application structure and the five behaviors | [Sample app](docs/en/sample-app.md) |
| Container branch (not measured) | [Branch: containerization](docs/en/branch-containerization.md) |

The Japanese documents under [docs/ja/](docs/ja/stage-0-smb-ntfs.md) are the reference; the English ones keep the same section structure.

## Verification environment

| Item | Value |
|---|---|
| Region | `ap-northeast-1` (infrastructure including FSx for ONTAP) |
| FSx for ONTAP configuration | Dedicated verification Single-AZ file system, one `appdata` volume, and `ntfs` security style |
| ONTAP 9.19.1P2 record | `verified`. Initial build record from 2026-10-07. |
| ONTAP 9.19.1P3 record | `verified`. Rebuild record from 2026-10-11 to 12. A separate historical record from P2. |
| Security style | `ntfs`, kept by the stage 2 decision. `unix` and `mixed` are `open/not measured`. |
| Directory | AWS Managed Microsoft AD |
| IaC | AWS CloudFormation (YAML), checked with `cfn-lint` and `cfn-guard` |
| AIMF | Pinned to v0.11.0. The 2026-10-12 session ran in kiro-cli 2.29.1 V2 interactive mode |

**SnapLock and snapshot locking (Tamperproof Snapshot) are not used.** Both prevent a volume from being deleted until its retention expires, which on a verification file system leaves charges that cannot be stopped.
Handling of irreversible operations is in [AGENTS.md](AGENTS.md).

Every deployment to AWS is preceded by an estimate of configuration, duration, and cost, and runs only after approval.

## Relationship to AIMF

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow) ([introductory blog post, Japanese](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)) is a workflow that has an AI agent carry out an application migration through fixed phases.
For this Spoke, AIMF is only the application-modernization workflow boundary. This Spoke adds the FSx for ONTAP storage, protocol, permission, and evidence boundaries that AIMF does not cover. Other runtimes and topologies published by AIMF are not generalized from this Spoke's results.
The working-record repository AIMF creates stays in an unpublished workspace; only excerpts of the results are published.
The session run on 2026-10-12, the ADR summaries, and what the harness does and does not enforce mechanically are in [stage 2](docs/en/stage-2-aimf-modern-dotnet.md).

## Unverified scope

| Scope | Evidence label | Boundary |
|---|---|---|
| Other runtimes | `open/not measured` | Java, Python applications, Python Lambda, other unverified Lambda runtimes, Node.js, and Go are out of scope. |
| Other application shapes | `open/not measured` | ASP.NET, ASP.NET Core, Web API, Web UI, HTTP ingress, authentication middleware, and long-running services are out of scope. |
| Containers and topology | `open/not measured` | ECS, EKS, and Multi-AZ are out of scope. The container branch has no runtime verification. |
| Security styles and versions | `open/not measured` | `unix`, `mixed`, other ONTAP versions, and multiple-volume behavior are out of scope. |
| Unexecuted Stage 3 paths | `open/not measured` | Enabling EventBridge Scheduler and `provided.al2023` were not executed. `provided.al2023` is only an option declared in the template. |
| S3 exclusive control | `open/not measured` | Exclusive-open/file-locking was skipped in the current DocIntake S3 adapter slice. This makes no universal S3 file-locking claim. |

The current .NET results are not generalized as evidence for another runtime, application shape, security style, or topology.

## Future candidates

Candidates A to D are future verification candidates, all `open/not measured`. This section gives only their names and responsibility boundaries. It does not set provisional runtime constants, configuration values, execution conditions, or candidate-specific designs.

| Candidate | Future verification unit | Status |
|---|---|---|
| A | .NET ASP.NET Core Web API or background service | `open/not measured` |
| B | Java / Spring runtime verification | `open/not measured` |
| C | Python Worker / Lambda with FSx for ONTAP S3 Access Points | `open/not measured` |
| D | ECS / EKS containerization | `open/not measured`. Container responsibility is coordinated with the Container-Datastore Spoke. |

### Evidence requirements for each candidate

Each candidate must be treated as a separate pattern. It must have an independent evidence ledger and independent verification conditions; the current `verified` records are not evidence for it. Candidate measurement, adoption, and publication scope will be decided in a separate future task after that evidence exists.

## Related repositories

Only the Hub journey map holds the overall journey and the diagram of how the repositories relate.
The adjacent Spoke links below are `documented` navigation and ownership context. Their content does not expand this Spoke's execution records or verified coverage.

| To read about | Go to |
|---|---|
| The overall journey and where this repository fits | [Modernization journey map](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/reference/modernization-journey-map.md) (Hub) |
| Moving data and servers with AWS Transform | [AWS Transform migration procedure](https://github.com/Yoshiki0705/VMware-Migration-EC2-ONTAP/blob/main/docs/en/aws-transform-migration-procedure.md) |
| Branch: containerization with FSx for ONTAP | [Deriving containerization and FSx for ONTAP integration](https://github.com/Yoshiki0705/FSx-for-ONTAP-Container-Datastore-Patterns/blob/main/docs/en/atx-containerization-fsxn-derivation.md) |
| Beyond stage 3: processing patterns with S3 Access Points | [FSx-for-ONTAP-S3AccessPoints-Serverless-Patterns](https://github.com/Yoshiki0705/FSx-for-ONTAP-S3AccessPoints-Serverless-Patterns/blob/main/README.en.md) |

## Quality gates

```bash
make install   # install the pinned tools into .venv
make all       # lint, audit, links, and tests
```

Each gate is described in [docs/agent/quality-gates.md](docs/agent/quality-gates.md).
