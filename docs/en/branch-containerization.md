# Branch: containerization

> Evidence label: `open/not measured`. This document defines a future navigation and ownership path. Verified container runtime behavior is outside this Spoke's scope.

## Branch position

This Spoke covers the staged modernization of `DocIntake` through stages 0 to 3.
Containerization is a future branch owned by the Container-Datastore Spoke. This Spoke has not verified the container runtime, deployment, or data-store connection behavior.

From stage 1 (multiprotocol) onward, the path can be considered as either modernizing to .NET 10 on Linux over NFS (stage 2) or pursuing containerization. Container procedures and results are not duplicated into this Spoke.

## Ownership link

- Container-Datastore Spoke: [FSx-for-ONTAP-as-Container-Datastore](https://github.com/Yoshiki0705/FSx-for-ONTAP-Container-Datastore-Patterns)

This link is `documented` navigation and ownership context for the adjacent Spoke. Its content does not expand this Spoke's verified coverage.

## Public records in adjacent Spokes

For containers, and for other workloads that use the same FSx for ONTAP, the owning Spokes publish their own records. None of them concerns `DocIntake`, and each link is pinned to a commit.

- Persistent volumes on EKS with Trident: [Container-Datastore verification record](https://github.com/Yoshiki0705/FSx-for-ONTAP-Container-Datastore-Patterns/blob/24b411dbec9d4f7136f688bde3120fa18c548e9c/docs/en/atx-containerization-fsxn-storage-verification.md) (section 4.3)
- Parquet reads through Amazon Athena: [Lakehouse evidence record](https://github.com/Yoshiki0705/FSx-for-ONTAP-Lakehouse-Integrations/blob/a0d2cc1dd4a959c69eb2c1a12f144f585d4005cd/verification-pack/athena-parquet-read/evidence/2026-10-28/evidence-record.yaml)
- Permission-aware RAG search: [Agentic RAG evaluation record](https://github.com/Yoshiki0705/FSx-for-ONTAP-Agentic-Access-Aware-RAG/blob/3dd74b5ddea63f05aef1790b8100dff4975abdfb/docs/en/evaluation.md) (section 3.3)

These are `documented` context. This Spoke's verified scope is unchanged, and containerizing `DocIntake` remains `open/not measured` here.

## Open boundary

Container-Datastore containerization remains `open/not measured` in this Spoke. ECS, EKS, NFS / SMB / S3 Access Points from containers, cluster execution, deployment, and failure behavior have not been verified.
