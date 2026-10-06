# Verification environment: staged modernization base on Amazon FSx for NetApp ONTAP

> Evidence tier: `hypothesis` (not measured). The procedures and predictions here are a plan made
> before execution; each is promoted to `verified` once a measurement exists (with environment,
> date and ONTAP version in the body).

This describes the environment, its creation and its teardown for the verification that moves a
Windows .NET Framework application through four stages while the data stays on
Amazon FSx for NetApp ONTAP (hereafter "FSx for ONTAP"). One environment is built in
ap-northeast-1; stages 0 to 3 run on it in order, then it is deleted.

## Irreversible features are not used

This verification does not use the following, and stops them mechanically before creation with two
layers: template static checks (cfn-guard) and a pre-execution hook.

- SnapLock (Compliance / Enterprise, audit-log volume)
- snapshot locking (Tamperproof Snapshot); no retention-bearing Snapshot or SnapMirror policy either
- S3 Object Lock, AWS Backup Vault Lock, EBS snapshot lock

Snapshot locking has no AWS API parameter, so cfn-guard cannot stop it. During stage 2 the fsxadmin
path is closed off, and at every stage boundary and before deletion all volumes are enumerated to
confirm locking is disabled.

## Planned environment

| Element | Configuration |
|---|---|
| FSx for ONTAP | `SINGLE_AZ_1`, 1,024 GiB, 128 MBps (minimum, fixed values) |
| SVM | AD-joined, NetBIOS name `APPMODSVM01` |
| Target volume | `appdata` (NTFS, `SnapshotPolicy: none`, `DeletionPolicy: Retain`) |
| Directory | AWS Managed Microsoft AD (Standard, 2 DCs) |
| Clients | one Windows EC2, one Linux EC2 |

## Planned creation steps

1. Present an estimate and obtain approval (configuration, time, cost)
2. Create the four secrets (passwords generated, never in argv)
3. Preflight the network and secrets (read-only)
4. Create the base stack
5. Confirm the SVM domain join by discovered domain controller (not by lifecycle alone)
6. Confirm no locking on all volumes

## Planned teardown steps

Teardown reports only by default and runs only after the deletion list is confirmed. The deletion
order and the post-deletion existence checks are recorded in the body at execution time.

## How estimates are produced

Before any billed operation, unit prices are re-fetched from the AWS Price List API to build an
estimate. Unit price times hours is arithmetic, not an invoice figure. An item that could not be
retrieved is written as "not retrieved".

## Predictions (unverified)

- The base stack is expected to take about 45 minutes (the Hub measurement is the reference; not
  measured in this Spoke).
- Endpoints in a single AZ are expected to still allow the domain join and Systems Manager
  (unconfirmed).
