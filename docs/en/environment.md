# Verification environment: staged modernization base on Amazon FSx for NetApp ONTAP

> Evidence tier: stated per section. Measurements from the environment created once and deleted on
> 2026-10-07 in ap-northeast-1 are `verified`; the rest describes procedure or lists what is still
> unconfirmed.

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

## Environment as built

> Evidence tier: `verified` (2026-10-07, ap-northeast-1). `appmod-base` was created once with the
> parameters of the approved estimate: the all-new default (all five `Create<X>` switches `true`)
> and `EgressMode=endpoints`.

| Element | Configuration |
|---|---|
| Network | new dedicated VPC, subnets in 2 AZs, interface endpoints for 6 services in 1 AZ, S3 gateway endpoint |
| FSx for ONTAP | `SINGLE_AZ_1`, 1,024 GiB, 128 MBps (minimum, fixed values) |
| SVM | AD-joined, NetBIOS name `APPMODSVM01` |
| Target volume | `appdata` (NTFS, `SnapshotPolicy: none`, `DeletionPolicy: Retain`) |
| Directory | new AWS Managed Microsoft AD (Standard, 2 DCs) |
| Clients | one Windows EC2 (`t3.large`), one Linux EC2 (`t3.medium`) |

With the endpoints in a single AZ, both the SVM domain join (confirmed by a discovered domain
controller) and Systems Manager Run Command worked. The VPC also had an internet gateway, and
whether the Systems Manager traffic went through the endpoints was not checked.

## Creation steps and measured duration

> Evidence tier: `verified` (2026-10-07, ap-northeast-1). Times come from the CloudFormation stack
> events and the approval record.

1. Present an estimate and obtain approval (configuration, time, cost). Its unit prices were re-fetched from the AWS Price List API on 2026-10-07
2. Create the four secrets (passwords generated, never in argv)
3. Preflight the network and secrets (read-only)
4. Create the base stack `appmod-base`. It started at 06:08 UTC and completed at 06:43 UTC, about 35 minutes. Most of that time went to creating the directory and the file system
5. Confirm the SVM domain join by discovered domain controller (not by lifecycle alone)
6. Confirm no locking on all volumes

The duration is from one creation and does not show that every creation takes the same time. It
was shorter than the reference used in the design (the Hub measurement of 43 minutes).

## Gaps found after creation

> Evidence tier: `verified` (2026-10-07, ap-northeast-1, observed from the Windows Server 2022 EC2).

Preparing stage 0 exposed three gaps in the template and the procedure. Each was fixed before stage 0
continued.

| Gap | Observation | Response |
|---|---|---|
| Windows instance role permissions | The Windows role could not read the secret `appmod/app-users`, which holds the AD user passwords, or the artifacts bucket | The running stack was not updated. Two inline policies were added to the Windows role out of band, one scoped to that secret's ARN and one to the artifacts bucket ARN. `templates/base.yaml` now carries the same grants, so a new environment needs no out-of-band step. On deletion, `teardown.sh` removes the two policies before deleting the base stack |
| Where the AD users live | The delegated administrator of AWS Managed Microsoft AD could not write to `CN=Users` at the domain root (Access is denied) | `appsvc` and `appreader` were created in the delegated tree, `OU=Users,OU=APPMOD` (`scripts/create-ad-users.ps1`) |
| AD management path | Active Directory Web Services (TCP 9389) was unreachable from the Windows host. LDAP (389) and LDAPS (636) were reachable | User creation uses LDAP through `System.DirectoryServices` instead of the ActiveDirectory module |

## Teardown record

> Evidence tier: `verified` (2026-10-07, ap-northeast-1), from the `teardown.sh --apply` run logs.

The first `teardown.sh --apply` stopped at step 4. `--svm` was given the SVM ID from the FSx for ONTAP
API (a value starting with `svm-`) instead of the ONTAP SVM name (`appmodsvm`), so `integration-clone.sh sweep`
could not resolve the SVM UUID and failed. Steps 0 to 3 had passed (starting the Linux EC2,
confirming no locking, confirming `appmod-stage3` was absent, confirming 0 S3 Access Points
attachments), and none of them deletes anything. On the step-4 failure `teardown.sh` stopped with
exit 1, with `appdata` and every other resource still in place. Stopping on failure instead of
continuing worked as intended against this wrong input. `teardown.sh` and every script that takes
`--svm` now reject an `svm-<hex>` value with exit 2 before making any call.

The second run used the correct SVM name, passed steps 0 to 10, and finished with exit 0 at about
17:41 UTC.

- Step 1 confirmed no snapshot locking and no SnapLock on the two volumes scanned (`appdata` and the SVM root volume)
- Steps 4 and 5 confirmed no leftover FlexClone, no `it_*` Snapshot, and an empty recovery queue
- Step 6 deleted `appdata` with `SkipFinalBackup=true`
- Step 7b removed the two out-of-band inline policies from the Windows role, then step 8 deleted the base stack
- Step 9 deleted the four secrets without a recovery window
- The API enumeration of step 10 found nothing left on its second check (file system, SVM, volumes, backups, S3 Access Points attachments, directory, tagged EC2, ENIs and endpoints, secrets). On the first check the four secrets were still listed

Step 11 (the Cost Explorer check on a later day) has not been done yet.

## Arithmetic estimate of billed hours

> Evidence tier: the times are `verified` (2026-10-07, ap-northeast-1). The amount is arithmetic,
> not an invoice figure.

The environment existed for about 11.5 hours, from the start of the `appmod-base` creation
(06:08 UTC) to the end of the deletion (about 17:41 UTC). The approved estimate's unit prices (AWS
Price List API, 2026-10-07, ap-northeast-1) sum to $0.8015 per hour; times 11.5 hours this is about
$9.22.

- Per hour: FSx for ONTAP SSD $0.2104, throughput $0.1589, AWS Managed Microsoft AD $0.146, Windows
  EC2 $0.1364, Linux EC2 $0.0544, EBS gp3 $0.0092, interface endpoints $0.084, Secrets Manager $0.0022
- Both EC2 instances are counted for the whole period. Data transfer, Systems Manager and S3
  requests are not included
- The billed amount is not known until it is checked in Cost Explorer, and that check has not been done yet

## How estimates are produced

Before any billed operation, unit prices are re-fetched from the AWS Price List API to build an
estimate. Unit price times hours is arithmetic, not an invoice figure. An item that could not be
retrieved is written as "not retrieved".

## Still unconfirmed

- In Cost Explorer on a later day: that billing stopped after deletion, and the actual billed amount
- With the interface endpoints in a single AZ, whether the Systems Manager traffic went through them
