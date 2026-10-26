# Verification environment: staged modernization base on Amazon FSx for NetApp ONTAP

> Evidence tier: stated per section. Measurements from the environment created once and deleted on
> 2026-10-07 in ap-northeast-1 are `verified`; the rest describes procedure or lists what is still
> unconfirmed.

This describes the environment, its creation and its teardown for the verification that modernizes a
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

![Test environment. The Windows EC2 instance reaches FSx for ONTAP over SMB, and the Linux EC2 instance over NFS 4.1 and SMB 3.0. A Lambda function in the VPC reaches FSx for ONTAP S3 Access Points through the S3 gateway endpoint. FSx for ONTAP joins AWS Managed Microsoft AD, and Secrets Manager holds the admin passwords.](../_assets/images/environment-architecture-en.svg)

Dark version: [SVG](../_assets/images/environment-architecture-en-dark.svg) / [PNG](../_assets/images/png/environment-architecture-en-dark@2x.png). Light version as PNG: [PNG](../_assets/images/png/environment-architecture-en@2x.png).

The access paths in the figure are the same as in the table below; no fact appears only in the figure.

| Origin | Path | Destination |
|---|---|---|
| Windows EC2 | SMB | FSx for ONTAP volume `appdata` |
| Linux EC2 | NFS 4.1; SMB 3.0 is also used | FSx for ONTAP volume `appdata` |
| Lambda in the VPC | S3 API, then the S3 gateway endpoint, then FSx for ONTAP S3 Access Points | FSx for ONTAP volume `appdata` |
| Both clients | Fetch credentials from Secrets Manager | Logon at run time and operations |
| FSx for ONTAP SVM | Domain join | AWS Managed Microsoft AD |
| Lambda in the VPC | The same S3 gateway endpoint | The artifacts bucket in Amazon S3 |

The three notes in the figure, in prose:

- The file system is Single-AZ. The second subnet exists because AWS Managed Microsoft AD needs two AZs. The file system, both clients, and the Lambda function sit in the first subnet.
- The network origin of the S3 Access Points is the VPC. A Lambda function inside the VPC reaches it through the S3 gateway endpoint, and the artifacts bucket goes through the same endpoint.
- Passwords are resolved from Secrets Manager secrets. They are read when AWS Managed Microsoft AD and the ONTAP `fsxadmin` account are created, and both clients also fetch them at run time. Operations ran through AWS Systems Manager Run Command.

## Creation steps and measured duration

> Evidence tier: `verified` (2026-10-07, ap-northeast-1). The approval time (06:07:17 UTC) comes
> from the approval record. The creation and deletion times of `appmod-base` come from the
> CloudFormation stack events, retrieved by stack ID after the stack was deleted (kept as a
> private run record).

1. Present an estimate and obtain approval (configuration, time, cost). Its unit prices were re-fetched from the AWS Price List API on 2026-10-07
2. Create the four secrets (passwords generated, never in argv)
3. Preflight the network and secrets (read-only)
4. Create the base stack `appmod-base`. It started at 06:09:58 UTC and completed at 06:42:39 UTC, about 33 minutes. Most of that was the directory (AWS Managed Microsoft AD) at 29.1 minutes; the file system's 15.1 minutes ran in parallel within it. The SVM (2.0 minutes) and the volume `appdata` (1.0 minute) were created after the directory completed. These are values from one creation, a guide to duration rather than a guarantee
5. Confirm the SVM domain join by discovered domain controller (not by lifecycle alone)
6. Confirm no locking on all volumes

The duration is from one creation and does not show that every creation takes the same time. It
was shorter than the reference used in the design (the Hub measurement of 43 minutes).

## How the base stack template is passed

Step 4 creates `appmod-base` through `deploy.sh base`. `templates/base.yaml` is 52,466 bytes, over
CloudFormation's 51,200-byte inline `--template-body` limit, so an inline `create-stack` fails with a
`ValidationError` before creating anything. `deploy.sh` therefore uploads the template to a dedicated
in-region deploy bucket (`appmod-deploy-<account>-ap-northeast-1`) and creates the stack with
`--template-url`. That bucket is separate from the artifacts bucket the base stack creates, and is
left in place across rebuilds rather than deleted; `teardown.sh` does not touch it.
`stage3-serverless.yaml` is 11,923 bytes, under the limit, so it stays on `--template-body` and does
not use `--template-url`. Either way, `--on-failure DELETE` and the approved `--parameters` are
unchanged.

The secret creation of step 2 (`scripts/create-secrets.sh`) must run before step 4's `deploy.sh
base`. Before the upload and `create-stack`, `deploy.sh base` confirms with `secretsmanager
describe-secret` that the two secrets the base stack resolves at create time exist
(`AdAdminSecretName` → `appmod/ad-admin`, `FsxAdminSecretName` → `appmod/fsxadmin`; the estimate's
value if it overrode the parameter). If either is missing it names the missing secret, points at
`scripts/create-secrets.sh`, and exits 2. The approved estimate is not consumed in that case (it is
not moved to `used/`), so the same estimate can be reused once the secrets exist. This enforcement
addresses the 2026-10-11 incident on a `base` run that skipped `scripts/create-secrets.sh`:
submitting `create-stack` with the secrets absent had CloudFormation start creating resources, the
directory fail with "Secrets Manager can't find the specified secret", and the whole stack roll back
via `--on-failure DELETE`. Nothing was left billing, but the approved estimate was consumed on
submission. The precheck stops while the estimate is still unused. The read-only
`scripts/preflight.sh --phase secrets` checks all four secrets and their keys as a separate step; the
precheck does not replace it but is a last-line gate inside `deploy.sh base` itself, so the step
cannot be skipped unnoticed.

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
- Step 7b removed the two out-of-band inline policies from the Windows role, then step 8 deleted the base stack. The stack deletion started at 17:24:53 UTC and completed at 17:40:34 UTC (about 16 minutes, from the stack events)
- Step 9 deleted the four secrets without a recovery window
- The API enumeration of step 10 found nothing left on its second check (file system, SVM, volumes, backups, S3 Access Points attachments, directory, tagged EC2, ENIs and endpoints, secrets). On the first check the four secrets were still listed

For step 11, Cost Explorer was checked per resource on 2026-10-09. All values are pre-close estimates (`Estimated`) and cover usage only (`RECORD_TYPE=Usage`), excluding credits and refunds.

| Resource | 2026-10-07 | 2026-10-08 |
|---|---|---|
| File system | $4.01 | $0.00 |
| Windows EC2 | $0.98 | $0.00 |
| Linux EC2 | $0.39 | $0.00 |
| Control: two other file systems in the same account that kept running | $8.70 each | $5.07-$5.08 each |

- The 10-08 data was partly in: the control file systems already carried about 60% of their usual daily amount, while the deleted resources showed nothing. That contrast is the basis for concluding that billing stopped after deletion. Until the 10-08 values close, the conclusion rests on estimates
- The EC2 amounts are lower than the arithmetic estimate, which counted every hour, because the instances were stopped between work sessions and the stopped time was not billed
- Cost Explorer shows no per-resource amount for AWS Managed Microsoft AD or the interface endpoints. Another directory in the same account kept running, so the service total cannot separate them

## Arithmetic estimate of billed hours

> Evidence tier: the times are `verified` (2026-10-07, ap-northeast-1). The start comes from the
> approval record and the end from the teardown run log. The amount is arithmetic, not an invoice
> figure.

The start is the approval record's `approved_at` (06:07:17 UTC). Billed resources were created
after approval, so this start is earlier than the actual creation start. The end is the end of the
deletion (about 17:41 UTC), and it is a lower bound. The four secrets were deleted at 17:40:58 UTC
and step 10 then ran at least one more 60-second check, so the actual completion was later.
The period is about 11.6 hours (11 h 34 min).
The approved estimate's unit prices (AWS Price List API, 2026-10-07, ap-northeast-1) sum to
$0.8015 per hour; times 11.6 hours this is about $9.3.

- Per hour: FSx for ONTAP SSD $0.2104, throughput $0.1589, AWS Managed Microsoft AD $0.146, Windows
  EC2 $0.1364, Linux EC2 $0.0544, EBS gp3 $0.0092, interface endpoints $0.084, Secrets Manager $0.0022
- Both EC2 instances are counted for the whole period. Data transfer, Systems Manager and S3
  requests are not included
- For the resources Cost Explorer shows per resource (the file system and the two EC2 instances), 10-07 totals $5.38, an estimate (see the table in the teardown record). The billed amount for AD and the endpoints cannot be confirmed per resource

## Observed costs of the rebuilt base (2026-10-11 to 2026-10-12)

> Evidence tier: `verified` (Cost Explorer read on 2026-10-13, ap-northeast-1, one observation each).
> The values carry `Estimated: true` (before billing closes) and are not invoice figures. The query
> method is in "How to attribute observed costs" in the [reproduction guide](reproduce.md).

Four conditions applied to the reads: the metric was `UnblendedCost`, the granularity was daily, dates are UTC, and
the `RECORD_TYPE` values Credit and Refund were excluded. With credits and refunds included, the values were near 0
because of account credits; no invoice was checked.
The account is shared with other uses, so a Cost Explorer service total is not this verification's cost.
Only two attributions were used: values filtered by the tag `aws:cloudformation:stack-name` that CloudFormation
applies (`appmod-base`, `appmod-stage3`), and values per resource ID (`GetCostAndUsageWithResources`).

The base ran from 2026-10-11T02:08:54Z (stack creation) to about 11:44Z on 2026-10-12 (`appmod-base` deletion complete), about 33.6 hours.

<!-- sum-check: cols=4 -->
| Service | 2026-10-11 (USD) | 2026-10-12 (USD) | Total (USD) |
|---|---|---|---|
| FSx for ONTAP (per resource ID of the file system; matches the tag-filtered value) | 7.837 | 4.191 | 12.03 |
| Amazon EC2 Compute (Windows t3.large, Linux t3.medium) | 4.160 | 2.199 | 6.36 |
| Amazon VPC (interface endpoints and others) | 1.848 | 0.924 | 2.77 |
| Amazon S3 (artifacts bucket) | 0.018 | 0.003 | 0.02 |
| AWS Lambda | 0 | 0 | 0 |
| Subtotal | - | - | 21.18 |

- The subtotal is the sum of the unrounded values (21.1801 USD) rounded to 21.18 USD, which also equals the sum of the rounded rows. Daily subtotals are not shown
- `EC2 - Other` was below 0.00001 on both days and is omitted from the table

Two costs could not be attributed.

- AWS Directory Service (AWS Managed Microsoft AD, Standard, two domain controllers) carries no resource ID in Cost Explorer (`NoResourceId`) and no CloudFormation tag. The account-wide value includes other uses, so this verification's share cannot be separated by resource. For comparison, the estimate's hourly rate gives 0.146 USD/h × 33.6 h ≈ 4.91 USD. The 4.91 USD is an estimate, not an observed amount. The difference-method inference of 4.80 USD is under the next table and in "Difference-method estimate for AWS Managed Microsoft AD"
- AWS Secrets Manager (four secrets) and Amazon CloudWatch were 0 in the tag-filtered query. The estimate puts them at about 0.07 USD together, which cannot be separated from the account-wide value

The estimate's hourly rates prorated to 33.6 hours agreed with the tag-attributed observed amounts within 3% per row (one observation each, ap-northeast-1, Single-AZ).

| Item | Estimate hourly rate × 33.6 h (USD) | Observed (tag, USD) | Difference |
|---|---|---|---|
| FSx for ONTAP (SSD 1,024 GiB + throughput 128 MBps) | 12.41 | 12.03 | −3% |
| EC2 (Windows + Linux) | 6.41 | 6.36 | −1% |
| VPC interface endpoints | 2.82 | 2.77 | −2% |
| AWS Managed Microsoft AD | 4.91 | 4.80 (difference method, inference) | −2% |
| Total (attributable rows only) | 21.64 | 21.18 | −2% |

The 4.80 USD in the AD row is an inference by the difference method, not an attribution by resource, and is excluded from the "attributable rows only" total (next section).
The approved 72-hour estimate totals 57.71 USD, which prorates to about 26.9 USD for 33.6 hours. The range confirmed as observed
amounts is 21.18 USD, which excludes AD.
The agreement confirms hourly rates, not an invoice. The AD share is not confirmed as an observed amount by resource.
The Cost Explorer value for AWS Transform cannot be attributed by tag and is not in this table ([AWS Transform usage and the credit line of the same amount](#aws-transform-usage-and-the-credit-line-of-the-same-amount)).

## Difference-method estimate for AWS Managed Microsoft AD

> Evidence tier: `verified` (Cost Explorer read on 2026-10-13, ap-northeast-1, one observation each). The values carry
> `Estimated: true`, and the assumption of the estimate is stated in the text. This is not an attribution by resource.

The AD of the second build (2026-10-11 and 2026-10-12) cannot be attributed by resource. Cost Explorer's Directory Service
carries no resource ID and no tag, and its value includes other directories in the same billing family. So the daily usage
(`APN1-Std-MicrosoftAD-DC-Usage`, DC-hours) was read with the steady baseline of the days without this project subtracted
(the difference method). The baseline is the mean of 2026-10-08 to 2026-10-10, 95.72 DC-hours per day.

<!-- sum-check: cols=4 -->
| Day (UTC) | Usage (DC-hours) | Baseline (DC-hours) | Difference (DC-hours) |
|---|---|---|---|
| 2026-10-11 | 137.12 | 95.72 | 41.40 |
| 2026-10-12 | 120.08 | 95.72 | 24.36 |
| Total | - | - | 65.76 |

- The 65.76 DC-hours of difference agrees within 2% with the expected 2 DC × about 33.6 h ≈ 67.2 DC-hours
- The unit price is 0.073 USD per DC-hour (7.012 USD ÷ 96.05 DC-hours on 2026-10-08), and 65.76 × 0.073 ≈ 4.80 USD, close to the estimate of 4.91 USD
- The 4.80 USD is an inference that assumes the baseline is constant. It is not an attribution by resource and is not treated as an observed amount
- It does not work for the first build (2026-10-07): usage on 2026-10-06 was 48.52 DC-hours, half of the baseline, so the baseline is not constant there. The first-build AD has only an estimate from the time-based rate (about 1.6 USD)

## Identified cost of the whole verification

> Evidence tier: `verified` (Cost Explorer read on 2026-10-13, ap-northeast-1, one observation each). The values carry
> `Estimated: true` and are not invoice figures.

The amounts attributed by the tags `appmod-base` and `appmod-stage3` (Usage only), per build.

<!-- sum-check: cols=3 -->
| Item | Day (UTC) | Amount (USD) |
|---|---|---|
| Tag-attributed: first build | 2026-10-07 | 6.31 |
| Tag-attributed: second build | 2026-10-11 and 2026-10-12 | 21.18 |
| Tag-attributed subtotal | - | 27.49 |

The next table adds the AD inference and the AWS Transform usage to that subtotal, giving the "identified" amount.

<!-- sum-check: cols=3 -->
| Item | Basis | Amount (USD) |
|---|---|---|
| Tag-attributed subtotal | Table above | 27.49 |
| AD, second build | Difference-method inference | 4.80 |
| AWS Transform usage | Cost Explorer Usage line | 3.62 |
| Identified | - | 35.91 |

Not in this 35.91 USD:

- AD of the first build: an estimate only, about 1.6 USD from the estimate's time-based rate
- Secrets Manager, CloudWatch and Lambda: not separable, estimated at 0.1 to 0.2 USD together
- The four secrets created by the accident: expected to be under one cent

The AWS Transform usage was offset by a credit of the same amount on a separate line, so its net was 0. Other credits in the account were not examined.
None of these figures is an invoice amount, and no invoice was checked.

## AWS Transform usage and the credit line of the same amount

> Evidence tier: `verified` (Cost Explorer read on 2026-10-13, ap-northeast-1, one observation). The cause of the gap between the minutes is unexplained.

On 2026-10-11, Cost Explorer shows AWS Transform Usage of 103.49 agent minutes (3.6221 USD) and a Credit line of
−3.6221 USD, a net of 0. The atx records show 92.46 minutes of analysis and 139.00 minutes of dotnet-modernization, 231.46 minutes in total.
The gap between 103.49 and 231.46 minutes is unexplained. No cause is guessed here.

## How estimates are produced

Before any billed operation, unit prices are re-fetched from the AWS Price List API to build an
estimate. Unit price times hours is arithmetic, not an invoice figure. An item that could not be
retrieved is written as "not retrieved".

## Still unconfirmed

- The 10-07 and 10-08 Cost Explorer values closing (still estimates as of 2026-10-09)
- With the interface endpoints in a single AZ, whether the Systems Manager traffic went through them
