# FSx-for-ONTAP-App-Modernization-Patterns

[日本語](README.md) | **English**

This repository covers patterns for modernizing legacy applications and systems while their data remains on Amazon FSx for NetApp ONTAP. The current verified slice is one purpose-built `DocIntake` console application recorded from .NET Framework 4.8 / Windows / SMB to .NET 10 / Linux / NFS, and then to .NET 10 managed-runtime Lambda / FSx for ONTAP S3 Access Points. The broad scope is a project boundary, not verified support for every runtime or topology.

The application input and output remain in the `appdata` volume on FSx for ONTAP; the application side and the protocol side change one stage at a time.

## Repository scope

The durable scope of this repository is patterns for modernizing legacy applications and systems while their data remains on FSx for ONTAP. It covers pattern-specific requirements, designs, implementations, verification records, evidence boundaries, and navigation. The current `DocIntake` record is one example within this broad scope. It is not a compatibility statement for another runtime, application shape, execution topology, or access path.

## Legacy application examples and how to map them

This repository verifies one `DocIntake`, but the same approach reads across many legacy applications that keep their data on a file share while moving the runtime. The table below lists stacks that are common modernization targets. Start from the row closest to your own application, then follow it to this repository's verification (stages 0–3 and the container branch) and to the modernization means (AIMF, AWS Transform).

Each link is an external `documented` example; it does not expand this repository's verified coverage, and it does not assert any vendor or product superiority. It is an entry point to help you identify your own stack.

| Legacy example | Typical modernization | Reference links (external) | Related verification here |
|---|---|---|---|
| Java: Struts / JSP web apps | Re-architect to Spring MVC / Spring Boot | [Struts to Spring MVC re-architecture](https://learn.microsoft.com/en-us/azure/developer/java/migration/github-copilot-app-modernization-for-java-rearchitecture), [why Spring is chosen as the target](https://www.stylez.co.jp/legacy_columns/struts_to_spring/why_spring_is_the_best_choice_for_migration_from_struts_framework/) | [Stage 2 (to a new runtime on Linux)](docs/en/stage-2-aimf-modern-dotnet.md) |
| Java: J2EE / EJB / WebSphere / WebLogic monoliths | Service decomposition, containerization | [Containerizing Java EE apps (AWS guidance)](https://docs.aws.amazon.com/prescriptive-guidance/latest/modernization-containerize-javaee/introduction.html), [decomposing an EJB monolith](https://bhavansh.hashnode.dev/decomposing-a-legacy-ejb-monolith-a-system-design-deep-dive) | [Container branch](docs/en/branch-containerization.md) |
| .NET Framework / ASP.NET monoliths (Windows-bound) | To .NET (Linux), or to containers | [Modernization pathways for a .NET Framework monolith](https://aws.amazon.com/blogs/architecture/modernization-pathways-for-a-legacy-net-framework-monolithic-application-on-aws/), [containerizing ASP.NET](https://aws.amazon.com/blogs/modernizing-with-aws/containerize-asp-net-application-using-aws-migration-hub-orchestrator/), [.NET modernization guidance](https://docs.aws.amazon.com/wellarchitected/latest/microsoft-workloads-lens/scenario-2-.net-application-modernization-on-aws.html) | [Stages 0–3 record](docs/en/stage-0-smb-ntfs.md) |
| COBOL / mainframe / end-of-life platforms | Re-platform, monolith decomposition | [Legacy system modernization](https://www.lastingdynamics.com/blog/legacy-system-modernization-guide/), [legacy code migration (IBM)](https://www.ibm.com/think/topics/legacy-code-migration) | [Document index](#document-index) |
| Apps that depend on a local filesystem / file share | Containerize, with shared storage as a persistent volume | [Containerizing legacy apps (what to watch for)](https://oneuptime.com/blog/post/2026-02-12-containerize-legacy-applications-for-aws/view), [containers lose state on restart (persistent storage is needed)](https://kubernetes.io/docs/concepts/storage/volumes/), [three ways to attach NAS to Kubernetes (NFS / SMB / iSCSI)](https://oneuptime.com/blog/post/2025-12-15-how-to-use-nas-storage-with-kubernetes/view) | [Container branch](docs/en/branch-containerization.md) |

A legacy application that depends on a file share loses its own filesystem state on restart once containerized, so it needs a persistent volume. Using FSx for ONTAP as the Kubernetes persistent volume lets the data follow the pod across rescheduling ([Kubernetes persistent volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)). The configuration and templates for this path are in the [container branch](docs/en/branch-containerization.md) and the adjacent Spoke.

The modernization means are AI Modernization Flow (AIMF) and AWS Transform, under "Related responsibilities" below. For any legacy example, you can read it through this repository's shape: keep the data on FSx for ONTAP, and change the application side and the access protocol one stage at a time.

### Block-storage starting point (documented)

The examples above assume file access, but the entry point is not only a file share. Workloads running on FC / iSCSI block storage on premises (databases, SQL Server FCI, and so on) can also be considered for modernization and optimization by moving them to FSx for ONTAP.

- Modernization (data use): changing from block to file access makes multiprotocol access (iSCSI / NFS / SMB / S3 Access Points) available on the same volume. With S3 API user mapping, you can extend to data use (analytics, AI integration) through S3 Access Points.
- Optimization (data protection, availability, loose coupling): you can consider access-pattern tiering (FabricPool), data protection with Snapshot / SnapMirror / FlexClone, high availability via Multi-AZ iSCSI multipath, and loose coupling / portability through non-disruptive testing with FlexClone.

This is a `documented` consideration, not a path this repository verified in a real environment (the verified coverage is limited to the file-originated `DocIntake`). The enabling capabilities are that the AWS Transform storage assessment supports File & Block and recommends FSx for ONTAP ([assess and migrate storage](https://aws.amazon.com/blogs/migration-and-modernization/assess-and-migrate-storage-to-the-cloud-with-aws-transform/)), and that block storage migration supports EC2 and FSx for ONTAP ([AWS Transform FSx for ONTAP support (preview)](https://aws.amazon.com/about-aws/whats-new/2026/06/aws-transform-vmware-fsx-for-ontap-preview/)).

The details of block storage migration, data protection and immutability, and analytics / AI use live in the adjacent Spokes that own those responsibilities (see "Related responsibilities" below). This repository covers the application side and does not host the block migration implementation.

## Current verified coverage

Current verified coverage is limited to one `DocIntake` console application and its recorded execution paths. The source is .NET Framework 4.8 / Windows / SMB. Stage 2 compares the old Windows build with the modernized Linux build; it is not Windows .NET 10 verification. Stage 3 records the .NET 10 managed-runtime Lambda and FSx for ONTAP S3 Access Points path. The table below is the repository's only full stage-detail table.

Each stage assumes the previous one, and volume data is never moved. The volume keeps the NTFS security style for as long as Windows clients remain.

| Stage | Application runtime | Access protocol | Main change |
|---|---|---|---|
| 0 | .NET Framework 4.8 on Amazon EC2 (Windows) | SMB (`ntfs` security style, SVM joined to AWS Managed Microsoft AD) | Reproduce the starting configuration |
| 1 | The same `DocIntake` as stage 0 | SMB plus NFS (multiprotocol) | Read and write the same `appdata` volume over NFS |
| 2 | .NET 10 on Amazon EC2 (Linux) | NFS | Compare the old Windows build with the modernized Linux build. This is not Windows .NET 10 verification |
| 3 | .NET 10 managed-runtime Lambda (`dotnet10`) | FSx for ONTAP S3 Access Points | Handle part of the processing through manual invocation. EventBridge Scheduler is disabled, and file locking is skipped in the current slice |
| Branch | Containers | NFS / SMB / S3 Access Points | A future navigation and ownership path. `DocIntake` container runtime is `open/not measured`. Adjacent Spokes publish records for containers and other workloads ([links](docs/en/branch-containerization.md)) |

## Evidence boundary

Current evidence applies only to the `DocIntake` slice, the dedicated verification Single-AZ file system in `ap-northeast-1`, its one `appdata` volume, its `ntfs` security style, the recorded ONTAP 9.19.1P2 and 9.19.1P3 versions, and the recorded execution paths. `verified` means a reproduced execution for the named slice and environment; `field-observation` means a one-off observation without confirmed reproduction; `documented` means a declaration in a repository artifact or cited documentation and not runtime execution; and `open/not measured` means unexecuted, unsupported by the current record, or outside the verified slice.

| Subject | Evidence label | Scope of the statement |
|---|---|---|
| Execution records for stages 0 to 3 | `verified` | Execution records for `DocIntake`. Each stage's configuration and unexecuted operations remain separate. |
| ONTAP 9.19.1P2 record | `verified` | Initial build record from 2026-10-07. |
| ONTAP 9.19.1P3 record | `verified` | Rebuild record from 2026-10-11 to 12. It is a separate historical record from P2. |
| One-off supporting observations | `field-observation` | Observations without confirmed reproduction. They are not generalized into service limits or general performance values. |
| Template declarations | `documented` | EventBridge Scheduler is declared disabled in the template, but it was not executed. |
| Fake S3 tests | `documented` | The tests use an in-memory fake. They do not prove real FSx for ONTAP S3 Access Points behavior. |
| Unexecuted or out-of-scope items | `open/not measured` | Listed in the non-generalization boundary in this section. |

Current .NET evidence is not generalized to Java, Python, Node.js, Go, ASP.NET, ASP.NET Core, Web API, Web UI, ECS, EKS, Multi-AZ, `unix` or `mixed` security style, or any other unverified runtime, topology, or access path. Another runtime or configuration requires evidence for its own pattern.

The current record does not establish Windows .NET 10 verification, universal S3 file-locking behavior, production performance, an SLO, a service limit, or a production cost estimate. A sample run remains distinct from a production estimate, this verification environment remains distinct from a general service limit, and a design consideration remains distinct from a legal or compliance judgment.

| Data | Evidence label | Destination |
|---|---|---|
| Application input and output | `verified` | The `appdata` volume on FSx for ONTAP. |
| Aggregate Probe JSON | `verified` | A separate artifacts bucket, distinct from the `appdata` input and output. |
| Lambda logs | `verified` | CloudWatch Logs. |

## Document index

| To read about | Go to |
|---|---|
| Reproducing the verification in your own environment, in order (prerequisites, estimate and approval, stages 0 to 3, teardown, what cannot be reproduced) | [Reproduction guide](docs/en/reproduce.md) |
| Stage 0: the SMB and NTFS starting point | [Stage 0](docs/en/stage-0-smb-ntfs.md) |
| Stage 1: adding NFS to the same volume | [Stage 1](docs/en/stage-1-multiprotocol.md) |
| Stage 2: modernizing to .NET on Linux (includes the AIMF session record) | [Stage 2](docs/en/stage-2-aimf-modern-dotnet.md) |
| Stage 3: partial serverless via S3 Access Points | [Stage 3](docs/en/stage-3-s3-access-points.md) |
| Lessons from the verification method (a measurement without a positive control is not a result, and others) | [Verification method lessons](docs/en/verification-method-lessons.md) |
| Environment structure and cost estimates | [Verification environment](docs/en/environment.md) |
| Sample application structure and the five behaviors | [Sample app](docs/en/sample-app.md) |
| Container branch (`DocIntake` not measured; links to adjacent Spoke records) | [Branch: containerization](docs/en/branch-containerization.md) |

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

## Future pattern roadmap

Future patterns remain `open/not measured` until they are independently specified and evidenced. Each pattern requires its own requirements, design, evidence ledger, verification conditions, and publication criteria. The current `DocIntake` records are not reused as evidence for another pattern. The record must keep sample runs separate from production estimates and this environment separate from general service limits.

## Related responsibilities

Only the Hub journey map holds the overall journey and the diagram of how the repositories relate. The links below are `documented` navigation and ownership context; their content does not expand this Spoke's verified coverage. AIMF remains the application-modernization workflow boundary. This Spoke adds the FSx for ONTAP storage, protocol, permission, and evidence boundaries that AIMF does not cover.

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow) ([Japanese documentation (source of truth)](https://github.com/aws-samples/sample-ai-modernization-flow/tree/main/ja), [introductory blog post, Japanese](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)) is a workflow that has an AI agent carry out an application migration through fixed phases. It runs the assessment-to-migration flow for a legacy application across three layers: Method (the migration methodology), Playbook (domain-specific knowledge), and Harness (AI execution discipline). The working-record repository AIMF creates stays in an unpublished workspace; only excerpts of the results are published. The session run on 2026-10-12, the ADR summaries, and what the harness does and does not enforce mechanically are in [stage 2](docs/en/stage-2-aimf-modern-dotnet.md).

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
