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

## Measured ONTAP version

> Evidence tier: `verified` (2026-10-07, ap-northeast-1, `SINGLE_AZ_1`, 1,024 GiB / 128 MBps).

The file system for this verification was created by CloudFormation without specifying a version,
and ran `NetApp Release 9.19.1P2` (build date 2026-08-19). The stage-0 and stage-1 boundary
records (`b0`, `b1`) captured it, and a direct `GET /api/cluster?fields=version` at
2026-10-07 17:06 UTC, just before deletion, returned the same value.

- All stage 0-1 results were measured on this version. The baseline for findings carried over by
  the Hub is 9.17.1P7D1; this verification ran on a newer version.
- Two behaviors were confirmed on 9.19.1P2. `snaplock.type` on a non-SnapLock volume returned
  `non_snaplock`. Domain controller discovery could be confirmed through
  `/api/protocols/cifs/domains/{svm.uuid}?fields=discovered_servers`, while
  `/api/protocols/active-directory` returned 0 records.
- This is the version observed for one creation. It does not show that every newly created file
  system gets 9.19.1P2.
- The version could not be read from the AWS management API. The keys `describe-file-systems` returned
  under `OntapConfiguration` were `DeploymentType`, `DiskIopsConfiguration`, `Endpoints`,
  `HAPairs`, `PreferredSubnetId`, `ThroughputCapacity`, `ThroughputCapacityPerHAPair` and
  `WeeklyMaintenanceStartTime`, with no version key (2026-10-07). To check the version, run the
  ONTAP REST call `GET /api/cluster?fields=version` or the ONTAP CLI command `version`, from a host
  inside the VPC that can reach the management endpoint, using the `fsxadmin` credentials.

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
