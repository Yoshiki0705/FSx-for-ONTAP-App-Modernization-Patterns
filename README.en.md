# FSx-for-ONTAP-App-Modernization-Patterns

[日本語](README.md) | **English**

Verified patterns for moving a .NET Framework application on Windows to modern .NET on Linux, and then partly to serverless, while its data stays on Amazon FSx for NetApp ONTAP.
The data never leaves the FSx for ONTAP volume; the application side and the protocol side change one stage at a time.

> **Status**: stages 0 to 3 have been measured. The runs used ONTAP 9.19.1P2 (initial build, 2026-10-07) and 9.19.1P3 (rebuild, 2026-10-11 to 12) on a Single-AZ file system with one volume and the ntfs security style.
> Most values are single observations; each document states the run id, date, environment, and evidence tier.
> Not measured: the unix and mixed security styles, other ONTAP versions, Multi-AZ, the container branch, and invoice amounts (costs are estimates only; invoices have not been observed).
> These are treated as `hypothesis`.

## Scope

| In scope | Out of scope |
|---|---|
| Changing the application runtime and access protocol in stages while the FSx for ONTAP volume stays shared | Migrating data from on-premises or VMware itself (an adjacent Spoke covers this) |
| Per-stage CloudFormation templates, verification scripts, and a purpose-built sample application | Containerization details (handed to an adjacent Spoke as a branch) |
| FSx for ONTAP-specific supplements for moving .NET Framework to modern .NET with AI Modernization Flow (AIMF) | Copies of the AIMF procedure itself (the AIMF repository is referenced instead) |

## Stages

Each stage assumes the previous one, and volume data is never moved.
The volume keeps the NTFS security style for as long as Windows clients remain.

| Stage | Application runtime | Access protocol | Main change |
|---|---|---|---|
| 0 | .NET Framework on Amazon EC2 (Windows) | SMB (NTFS security style, SVM joined to AWS Managed Microsoft AD) | Reproduce the starting configuration |
| 1 | Same as stage 0 | SMB plus NFS (multiprotocol) | Configure Windows-to-UNIX user mapping and read and write the same volume over NFS |
| 2 | Modern .NET on Amazon EC2 (Linux) | NFS | Port the application with the AIMF `dotnetfw-to-modern-dotnet` playbook and replace the instance with Linux EC2 |
| 3 | Part of the processing moves to serverless | FSx for ONTAP S3 Access Points | Read files from AWS Lambda and similar services through S3 Access Points |
| Branch | Containers (Amazon ECS / Amazon EKS) | NFS / SMB / S3 Access Points | Possible from stage 1 onward; see the adjacent Spoke |

## Document index

| To read about | Go to |
|---|---|
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
| Region | ap-northeast-1 (infrastructure including FSx for ONTAP) |
| FSx for ONTAP | A new file system dedicated to verification. Single-AZ, minimum storage and throughput capacity. Measured ONTAP versions: 9.19.1P2 (2026-10-07) and 9.19.1P3 (2026-10-11 to 12) |
| Security style | ntfs (kept by the stage 2 decision; the decision was the user's) |
| Directory | AWS Managed Microsoft AD |
| IaC | AWS CloudFormation (YAML), checked with `cfn-lint` and `cfn-guard` |
| AIMF | Pinned to v0.11.0. The 2026-10-12 session ran in kiro-cli 2.29.1 V2 interactive mode |

**SnapLock and snapshot locking (Tamperproof Snapshot) are not used.** Both prevent a volume from being deleted until its retention expires, which on a verification file system leaves charges that cannot be stopped.
Handling of irreversible operations is in [AGENTS.md](AGENTS.md).

Every deployment to AWS is preceded by an estimate of configuration, duration, and cost, and runs only after approval.

## Relationship to AIMF

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow) ([introductory blog post, Japanese](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)) is a workflow that has an AI agent carry out an application migration through fixed phases.
It targets the application itself; designing and building the cloud environment is out of its scope.
This repository uses AIMF in stage 2 and supplies the storage-side prerequisites AIMF does not cover (shared paths, permission mapping, protocol switch-over).
The working-record repository AIMF creates stays in an unpublished workspace; only excerpts of the results are published.
The session run on 2026-10-12, the ADR summaries, and what the harness does and does not enforce mechanically are in [stage 2](docs/en/stage-2-aimf-modern-dotnet.md).

## Related repositories

Only the Hub journey map holds the overall journey and the diagram of how the repositories relate.

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
