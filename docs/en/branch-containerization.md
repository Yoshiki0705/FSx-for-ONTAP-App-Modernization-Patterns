# Branch: containerization

> Evidence label: `open/not measured`. This document defines a future navigation and ownership path. Verified container runtime behavior is outside this Spoke's scope.

## Branch position

This Spoke covers the staged modernization of `DocIntake` through stages 0 to 3.
Containerization is a future branch owned by the Container-Datastore Spoke. This Spoke has not verified the container runtime, deployment, or data-store connection behavior.

From stage 1 (multiprotocol) onward, the path can be considered as either moving to .NET 10 on Linux over NFS (stage 2) or pursuing containerization. Container procedures and results are not duplicated into this Spoke.

## Ownership link

- Container-Datastore Spoke: [FSx-for-ONTAP-as-Container-Datastore](https://github.com/Yoshiki0705/FSx-for-ONTAP-Container-Datastore-Patterns)

This link is `documented` navigation and ownership context for the adjacent Spoke. Its content does not expand this Spoke's verified coverage.

## Open boundary

Container-Datastore containerization remains `open/not measured` in this Spoke. ECS, EKS, NFS / SMB / S3 Access Points from containers, cluster execution, deployment, and failure behavior have not been verified.
