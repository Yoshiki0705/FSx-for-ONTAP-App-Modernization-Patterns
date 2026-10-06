# FSx-for-ONTAP-App-Modernization-Patterns

[日本語](README.md) | **English**

Verified patterns for moving a .NET Framework application on Windows to modern .NET on Linux, and then partly to serverless, while its data stays on Amazon FSx for NetApp ONTAP.
The data never leaves the FSx for ONTAP volume; the application side and the protocol side change one stage at a time.

> **Status**: scaffold only. Procedures, templates, and results for each stage are added as each one is verified.
> No stage has been verified yet.

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

## Verification environment

| Item | Value |
|---|---|
| Region | ap-northeast-1 (infrastructure including FSx for ONTAP) |
| FSx for ONTAP | A new file system dedicated to verification. Single-AZ, minimum storage and throughput capacity |
| Directory | AWS Managed Microsoft AD |
| IaC | AWS CloudFormation (YAML), checked with `cfn-lint` and `cfn-guard` |
| AIMF | Pinned to v0.11.0 |

**SnapLock and snapshot locking (Tamperproof Snapshot) are not used.** Both prevent a volume from being deleted until its retention expires, which on a verification file system leaves charges that cannot be stopped.
Handling of irreversible operations is in [AGENTS.md](AGENTS.md).

Every deployment to AWS is preceded by an estimate of configuration, duration, and cost, and runs only after approval.

## Relationship to AIMF

[AI Modernization Flow](https://github.com/aws-samples/sample-ai-modernization-flow) ([introductory blog post, Japanese](https://aws.amazon.com/jp/blogs/news/aidm-introducing-ai-modernization-flow/)) is a workflow that has an AI agent carry out an application migration through fixed phases.
It targets the application itself; designing and building the cloud environment is out of its scope.
This repository uses AIMF in stage 2 and supplies the storage-side prerequisites AIMF does not cover (shared paths, permission mapping, protocol switch-over).
The working-record repository AIMF creates stays in an unpublished workspace; only excerpts of the results are published.

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
