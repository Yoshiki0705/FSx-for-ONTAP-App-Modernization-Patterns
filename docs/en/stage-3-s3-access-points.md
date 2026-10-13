# Stage 3: partial serverless via S3 Access Points

> Evidence tier: shown per section. AWS specifications (supported operations, runtime deprecation
> dates, billing items) are `documented`, each with a source. Deployment and Lambda invocation results
> are `verified`, results written as a single observation are `field-observation`, anything not run is
> `hypothesis`, and anything with neither a public primary source nor a reproduction record is `open`.
> Unless stated otherwise the environment is ap-northeast-1, ONTAP 9.19.1P3, Single-AZ, one volume
> `appdata` (security style `ntfs`). Measurement run ids are `s3-20261011T212200Z` (2026-10-11 to 12),
> `s3b-20261012T022900Z` (2026-10-12) and `s3m-20261012T104800Z` (2026-10-12).

Attach FSx for ONTAP S3 Access Points to the `appdata` target volume of Amazon FSx for NetApp ONTAP and
run the Worker and the Probe (S3 variant) from AWS Lambda. The volume is not cloned and the data is
not moved.

## Configuration

The design in the template `templates/stage3-serverless.yaml`:

| Element | Content |
|---|---|
| Lambda function | `DocIntake.Lambda` on .NET 10. Handler `DocIntake.Lambda::DocIntake.Lambda.Function::Handler`, 512 MiB memory, 120-second timeout |
| Runtime | Default is `dotnet10` (managed runtime, Amazon Linux 2023). The other option is `provided.al2023`, which needs a separately built self-contained bootstrap (the current zip does not run on it) |
| Why `dotnet8` was dropped | In the Lambda runtime list, `dotnet8` is deprecated on 2026-11-10, less than a month away. `dotnet10` is 2028-11-14 (checked 2026-10-11). In newer Regions, Lambda does not offer a runtime that deprecates within 6 months |
| Network | The VPC and primary subnet come from the `appmod-base` outputs (`Fn::ImportValue`). The security group is dedicated to the Lambda, with no inbound rules and outbound limited to HTTPS (443). Traffic to S3 Access Points and the artifacts bucket goes through the S3 gateway endpoint in `appmod-base` |
| Environment variables | `S3_ACCESS_POINT_ARN` (`S3AccessPoint.ResourceARN`, passed as the SDK bucket argument), `ARTIFACTS_BUCKET` (the artifacts bucket), `STORE=s3`, `FS_IDENTITY` |
| IAM | `s3:ListBucket` on the S3 Access Points ARN, `s3:GetObject` and `s3:PutObject` on its `/object/*`, and read/write on the artifacts bucket only. `s3:DeleteObject` is not granted (stage-3 code does not delete) |
| Schedule | An Amazon EventBridge Scheduler schedule `appmod-stage3-worker-poll` (every 5 minutes) is created `DISABLED`. Measurements are invoked manually |

Deployment result (`verified`): the stack was created and deleted 3 times (`s3-20261011T212200Z`,
`s3b-20261012T022900Z`, `s3m-20261012T104800Z`). `AWS::FSx::S3AccessPointAttachment` reached
`CREATE_COMPLETE` and the lifecycle reached `AVAILABLE`. The file-system identity type was WINDOWS
(`APPMOD\appsvc`) and the network origin was VPC. Deletion completed with `delete-stack`; the wait for
the deletion in `s3-20261011T212200Z` exceeded 20 minutes (single observation). No attachment whose
name starts with `appmod` remained after deletion.

## Steps

The deployment was carried out in this order. The creation time was not recorded.

1. Confirm the Lambda subnet's route tables carry the S3 prefix list
2. Build the `DocIntake.Lambda` zip (`dotnet publish -r linux-x64 --self-contained false`) and place it at `lambda/worker.zip` in the artifacts bucket
3. Produce the estimate with `estimate.py --target stage3` and obtain approval (including the `NetworkOrigin` and file-system identity values)
4. Create the add-on stack with `deploy.sh stage3` (including the S3 Access Points attachment). Pass the `parameters` recorded in the estimate to `create-stack` unchanged
5. Confirm connectivity with a data operation such as `GetObject` (`HeadBucket` is not evidence)
6. Take the inventory and the boundary record (`b3`), the Probe (S3 variant), and the comparison

## How to invoke the Lambda

The payload has the form `{"mode":"worker"|"probe","run_id":"...","stage":"s3"}`. `stage` accepts only `s3`.

| `mode` | Behavior | Where the result goes |
|---|---|---|
| `worker` | Reads `seed/inbox/` and writes `out/s3/reports/` and `out/s3/index.json` | `out/s3/` on `appdata` |
| `probe` | Measures the five behaviors in the S3 variant and builds an `appmod-probe/1` JSON | `probe/<run-id>/lambda-s3.json` in the artifacts bucket. The same JSON is also returned as the invocation result |

```bash
aws lambda invoke --region ap-northeast-1 --function-name appmod-stage3-worker \
  --cli-binary-format raw-in-base64-out \
  --payload '{"mode":"probe","run_id":"s3-20261011T000000Z","stage":"s3"}' result.json
```

To run the schedule, call `aws scheduler update-schedule` with the definition returned by
`aws scheduler get-schedule`, adding `--state ENABLED`. `update-schedule` replaces the whole definition,
so passing only `State` loses the other values. Return it to `DISABLED` after measuring (`teardown.sh`
also sets `DISABLED` before deleting).

### Lambda execution results

> `verified`, single observation each. Runtime `dotnet10`, ONTAP 9.19.1P3. Time and memory come from
> the Lambda record; items not in the record are written "not recorded".

| Run id | Invocation | Result | Duration | Max memory |
|---|---|---|---|---|
| `s3-20261011T212200Z` (earlier implementation) | worker | `exit_code` 0. `out/s3/reports/doc-001` to `005.txt.txt` and `index.json` confirmed from the NFS side | 2,505 ms (first invocation, Init 361 ms) | 115 MB |
| `s3-20261011T212200Z` | probe | of the 5 behaviors, 4 `measured` and 1 `skipped` (file-locking) | 821 ms | 120 MB |
| `s3m-20261012T104800Z` (AIMF-derived code) | worker | `exit_code` 0. `out/s3/` rewritten; `index.json` is 181 bytes (compact JSON, ADR-4), 5 reports | not recorded | not recorded |
| `s3m-20261012T104800Z` | probe | 4 `measured`, 1 `skipped` (file-locking) | not recorded | not recorded |

## The five behaviors in the S3 variant

A behavior that does not hold under the S3 API is recorded as `skipped`, with the reason in
`observed.reason`.

| Behavior | Handling in the S3 variant | `outcome` |
|---|---|---|
| `case-sensitivity` | With `GetObject`, request `seed/Docs/Index.json` (changed spelling) and the real `seed/docs/index.json` (positive control), and record whether each was found. `measured` only when the positive control was found | `measured` |
| `path-separator` | Run `PutObject` with a key containing `\` and record whether it reads back. The key is `probe/sep\check-s3.txt` (the earlier implementation used `probe\sep-check-s3.txt`; it was moved under `probe/` to avoid the trap in "Effect of a `\` key written at the volume root on the SMB side" below). The file names produced on the SMB and NFS sides are checked in each host's inventory | `measured` |
| `file-locking` | Not measured, because the S3 API has no operation that opens exclusively | `skipped` |
| `acl-evaluation` | Recorded as `skipped` because the S3 API has no pre-check; the success or failure of an actual `PutObject` to `probe/<run-id>/deny-appsvc.txt` is recorded | `measured` |
| `write-visibility` | `PutObject` a marker object and record its key | `measured` |

`DeleteObject` is among the operations usable on S3 Access Points. The write-permission check in
`S3FileStore` (`CanWrite`) writes a unique key with `PutObject` and then removes it with `DeleteObject`.
The stage-3 Probe does not call this check, however. The outcome of `PutObject` and `GetObject` while
Windows holds the file open with `FileShare.None` (`observed.under_smb_lock`) is measured with a pair of
hosts, and is not part of this single invocation.

## Measured results of the five behaviors in the S3 variant

> `verified`, single observation each. The run id and date are on each row. The result is `measured` or
> `skipped`, in the same categories as the table above.

| Behavior | Result | Run id |
|---|---|---|
| `case-sensitivity` | The reference `seed/Docs/Index.json` did not resolve (`resolved=false`) and the positive control `seed/docs/index.json` resolved (`real_resolved=true`). Through S3 Access Points, keys are case-sensitive | `s3b-20261012T022900Z`, `s3m-20261012T104800Z` (both 2026-10-12; same result) |
| `path-separator` | `PutObject` with a key containing `\` succeeded and read back (`read_back=true`). `\` is treated as an ordinary character. Where the object lands on the volume depends on how the key is written (next section) | `s3b-20261012T022900Z` (key `probe\sep-check-s3.txt`), `s3m-20261012T104800Z` (key `probe/sep\check-s3.txt`) |
| `file-locking` | `skipped` (the S3 API has no exclusive-open operation) | the same in all 3 runs |
| `acl-evaluation` | `precheck` was `skipped`, and `PutObject` to `probe/<run-id>/deny-appsvc.txt` was allowed (`io_put_allowed=true`). A case where it is not allowed (the false side) was not observed | `s3-20261011T212200Z`, `s3b-20261012T022900Z`, `s3m-20261012T104800Z` |
| `write-visibility` | A marker object was written and its key recorded. The time difference was not measured (it needs a pair of hosts) | the same in all 3 runs |

- The `case-sensitivity` of the earlier `s3-20261011T212200Z` had a positive control that did not resolve
  (`real_resolved=false`) and is not used as a conclusion. It was re-measured in `s3b-20261012T022900Z`
  with the reference moved under `seed/`. A measurement without a positive control reads equally as
  "case-sensitive" or "not found" when the result is false, so it is not a conclusion.
- The output of this Probe prints `topology: cross-host` even for a single Lambda invocation. This is a
  fixed label and not evidence that two hosts coordinated. Two-host coordination counts only the pairs
  that `probe_merge.py` judged `cross-host` (`reasons` empty).
- What to do: keys through S3 Access Points are case-sensitive. Code that worked over SMB will not find a
  file if it requests the same name from S3 with a different spelling. S3 has no exclusive-open
  operation, so put processing that needs exclusive control on the NFS or SMB route.

## Effect of a `\` key written at the volume root on the SMB side

> `verified` (the failure and the later pass of the check). The correspondence between the short name
> and the long name is an inference (`field-observation`). ONTAP 9.19.1P3, Single-AZ, one volume.

In an S3 key `\` is an ordinary character. When a key containing `\`, such as `probe\sep-check-s3.txt`,
was written directly under the bucket, a one-byte file whose name contains `\` appeared directly under
the volume root. The name showed up in the SMB-side top-level listing as the 8.3 short name
`PROBES~1.TXT`, and `check-invariant.py`, which allows only `out`, `probe` and `seed` at the top level,
failed (the `b3` of `s3-20261011T212200Z`, the `b2` of `s2m-20261012T090934Z`, and the `b3` of
`s3m-20261012T104800Z`; 2026-10-12).

- The correspondence between the short name and the long name is an inference. The long name was not
  requested and matched on the SMB side; it is read from `PROBES~1.TXT` also disappearing from the listing
  after the long-named file was deleted on the NFS side.
- Fix 1: the Probe key was moved under `probe/` (`probe/sep\check-s3.txt`). In `s3m-20261012T104800Z` the
  new key did not appear at the volume root and became a file inside `probe/`.
- Fix 2: the file already at the volume root was deleted by running `rm` once over NFS as UNIX uid 10001.
  When `b3` was taken again after the deletion (`s3m-20261012T104800Z-clean`), `top_level_paths` was the
  three entries `out`, `probe` and `seed`, and `check-invariant.py` passed (exit code 0). The deletion is
  a write, so it was done with separate approval. There were 0 snapshots, so there is no way to undo this
  deletion.
- What to do: run checks that write keys containing `\` only under a dedicated prefix such as `probe/`,
  and never directly at the volume root. If one is written there, the SMB-side top level is polluted and
  undoing it needs a delete (a write).

## Name mapping that appears only while S3 Access Points are attached

> `field-observation`. A single observation in `s3-20261011T212200Z`; reproduction in other
> configurations is unconfirmed.

In the `b3` taken while attached, ONTAP had 5 name mappings, one of which was `s3_win` (replacement
`APPMOD\appsvc`). Read again after `appmod-stage3` was deleted, `s3_win` was gone and the count was back
to the same 4 as `b2`. It appeared with the attachment and disappeared with the deletion. It matches
the access point's file-system identity (WINDOWS, `APPMOD\appsvc`) and the replacement. In
`s3m-20261012T104800Z`, with AIMF-derived code, the boundary was taken after the stack was deleted, so
this mapping was not observed (4 mappings, no `s3_win`).

- Whether the attachment process of FSx for ONTAP created this mapping has not been confirmed in the
  ONTAP and AWS documentation.
- What to do: when comparing the name-mapping count across boundary records, note whether the access
  point was attached. A record taken while attached and one taken after deletion can differ by one.

## Reachability on an AD-joined SVM and `HeadBucket`

> `open`. This Spoke did not run a test that makes the Active Directory domain controller (DC)
> unreachable.

Two statements, "on an AD-joined SVM every data operation on S3 Access Points needs reachability to the
AD DC" and "when the DC is unreachable only `HeadBucket` succeeds", have neither a public primary source
nor a reproduction record, and the Hub note
[What an endpoint needs to reach S3 Access Points](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/client-access/notes/what-an-endpoint-needs-to-reach-s3-access-points.md)
(Japanese) also treats them as `open`. This document treats them as `open` too and does not call them
`verified` or `documented`.

What can be said with confidence is limited. `HeadBucket` checks that the bucket (access point) exists
and does not show whether files can be read or written. What to do: do not judge connectivity from a
successful `HeadBucket` alone; confirm it with the data operations you actually use (`GetObject`,
`ListObjectsV2`, `PutObject`). Stage 3 of this Spoke confirmed connectivity with data operations
(`s3b-20261012T022900Z`, `s3m-20261012T104800Z`).

## Scope of the stage-3 estimate

`estimate.py --target stage3` does not re-estimate the `appmod-base` hours; it counts only the add-on
stack. The base hours are included in the `base` (and `extend`) estimates.

| Item | How it is counted | Source of the unit price (ap-northeast-1) |
|---|---|---|
| Lambda requests | Number of manual invocations (default 20, set with `--invocations`) | Price List `AWSLambda`, `APN1-Request` |
| Lambda duration | Invocations × 0.5 GB × the 120-second ceiling | Price List `AWSLambda`, `APN1-Lambda-GB-Second` (first-tier price) |
| S3 Access Points requests | An upper bound assuming 100 per invocation, all priced at the PUT/COPY/POST/LIST rate | Amazon FSx for NetApp ONTAP pricing page (English), the S3 Access Points tab. Asia Pacific (Tokyo) PUT/COPY/POST/LIST: $0.00108 per 1,000 requests, confirmed on the pricing page tab and in the AWS Price List API (service code `AmazonS3`), read 2026-10-12 (see the note below the table) |
| S3 Access Points requests (GET rate) | The same count, shown for reference and excluded from the total | Same tab. GET and all other requests: $0.000029 per 1,000 requests, read 2026-10-12 |
| CloudWatch Logs ingestion | An upper bound assuming 1 MiB per invocation | Price List `AmazonCloudWatch`, `APN1-DataProcessing-Bytes` |
| EventBridge Scheduler | 0 invocations because it is `DISABLED`, $0 | None |
| S3 data transfer | Not counted. It depends on object sizes the estimate does not assume | Per the pricing page, data transfer through S3 Access Points is billed as for an S3 bucket |

The request line is an upper bound. The English
[Amazon FSx for NetApp ONTAP pricing](https://aws.amazon.com/fsx/netapp-ontap/pricing/) page
has an "S3 Access Points for FSx" tab. It says you pay for S3 requests made against S3 Access Points, charged <!-- allow:naming -->
per 1,000 requests by request type (PUT/COPY/POST/LIST, and GET and all other requests), at the same rate as
requests made with the API or SDK. The values in the "FSx for NetApp ONTAP" row for Asia Pacific (Tokyo) are
$0.00108 per 1,000 PUT/COPY/POST/LIST requests and $0.000029 per 1,000 GET and all other requests (read
2026-10-12; evidence tier: documented, two vendor sources, below). The tab is rendered in the
browser, so a static HTML fetch does not show it. The Japanese page as of 2026-10-12 had no such tab that we could
find. The estimate uses these values. Because the Worker and the Probe mix reads and writes and the split is not
measured, every assumed request is priced at the higher PUT/COPY/POST/LIST rate. At the GET and all-other rate
(0.000000029 USD per request) the same 2,000 requests would be 0.000058 USD, against 0.00216 USD at the PUT/LIST
rate (20 invocations × 100 requests; arithmetic, not an invoice figure). Amazon S3 list prices are higher and were
not used in this estimate.

S3 data transfer is not included in the estimate's total. It depends on object sizes the estimate does not assume.
The same unit prices were confirmed in the AWS Price List API, read 2026-10-12, Asia Pacific (Tokyo). The SKUs are
under service code `AmazonS3`, not `AmazonFSx`; `AmazonFSx` has no S3 Access Points request SKU in this region.
`APN1-Requests-FSXONTAP-Tier1` (SKU `ZE45RAAH9EX5HNT4`) is $0.00108 per 1,000 PUT/COPY/POST/LIST requests, and
`APN1-Requests-FSXONTAP-Tier2` (SKU `W647VRU94Q4WFUS7`) is $0.000029 per 1,000 GET and all other requests. Both
match the pricing page tab. The sibling usagetypes `APN1-Requests-FSXOPENZFS-Tier1` and `-Tier2` belong to a
different product and are not used. The evidence tier stays `documented`: these are two vendor sources, and no
bill has been observed, so the estimate is not a prediction that the invoice will match. The request counts
(100 per invocation) are an assumption. If a price key is missing from the price file, the line is recorded as `not retrieved` with no
subtotal rather than defaulted. That line is left out of the total, so the total is then lower than the real
figure. Check that no line reads `not retrieved` before using the total.

The estimate also records the `parameters` to be approved (`BaseStackName`, `LambdaPackageKey`,
`LambdaHandler`, `LambdaRuntime`, `FileSystemIdentityUser`, and `ArtifactsBucketName` only when
specified). `deploy.sh stage3` passes them through unchanged, and rejects an estimate whose
`parameters` are missing or empty, as it does for `base` (exit code 2).

## Estimates used for the runs

The estimates approved for the three stage-3 runs totaled $0.04 (2026-10-11), $0.01 (2026-10-12 02:19Z)
and $0.01 (2026-10-12 10:35Z), in that order. These are upper-bound calculations (unit price × quantity),
not invoiced amounts.

The observed amounts were read from Cost Explorer on 2026-10-13 (`Estimated: true`, Credit and Refund excluded, no invoice checked).
In the values filtered by the tags `appmod-base` and `appmod-stage3`, AWS Lambda was 0 USD on both 2026-10-11 and 2026-10-12.
Amazon S3 was 0.02 USD for the artifacts bucket. No observed amount isolates the three runs.
Which service row carried the S3 Access Points request charges was not checked.
The observed amounts for the whole base are in [Verification environment](environment.md).

## What to confirm at the boundary

- The target volume UUID and the `seed/` inventory are identical to stage 0
- The security style is still `ntfs`
- The S3 Access Points target is `appdata`

Result (`verified`): the UUID, security style, `seed/` inventory, export policy, snapshots and snapshot
locking were unchanged from `b0`, and `check-no-locking.sh` passed. `check-invariant.py` failed because
of `PROBES~1.TXT` at the root and passed after that file was deleted and `b3` was taken again
(`s3m-20261012T104800Z-clean`). Details are in "Effect of a `\` key written at the volume root on the SMB
side" above.

## Sources for the claims

S3 Access Points constraints are cited per claim from Hub notes (contents are not copied).

- S3 Access Points constraints (unsupported APIs, single-identity authorization, ACLs not inherited, no event notification): [s3-access-point-constraints](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/data-utilization/notes/s3-access-point-constraints.md)
- The two-layer authorization: [access-point-authorization-layers](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/security-governance/notes/access-point-authorization-layers.md)
- Operations usable on S3 Access Points (including `DeleteObject`; Object Lock and conditional writes are not usable): [Access point compatibility](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/access-points-for-fsxn-object-api-support.html) (checked 2026-10-11)
- Lambda runtimes and deprecation dates: [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) (checked 2026-10-11)
- The unit prices of S3 requests through S3 Access Points, and that data transfer is billed: [Amazon FSx for NetApp ONTAP pricing](https://aws.amazon.com/fsx/netapp-ontap/pricing/), English page, tab "S3 Access Points for FSx" (checked 2026-10-12) <!-- allow:naming -->
- The same unit prices as AWS Price List API SKUs: service code `AmazonS3`, usagetypes `APN1-Requests-FSXONTAP-Tier1` (SKU `ZE45RAAH9EX5HNT4`) and `APN1-Requests-FSXONTAP-Tier2` (SKU `W647VRU94Q4WFUS7`), Asia Pacific (Tokyo), read 2026-10-12
- DC reachability on an AD-joined SVM and `HeadBucket` (`open`): [What an endpoint needs to reach S3 Access Points](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/client-access/notes/what-an-endpoint-needs-to-reach-s3-access-points.md) (Japanese)

## Results and predictions by tier

Moved to results (`verified`):

- S3 Access Points could be attached on this configuration (`SINGLE_AZ_1`, AD-joined SVM, NTFS volume)
  (3 times, "Configuration" above).
- Within the IAM scope above (no `DeleteObject`), the Worker's and the Probe's reads and writes passed.
  The Probe does not call `CanWrite`, and the Worker does not delete (`s3-20261011T212200Z`,
  `s3m-20261012T104800Z`).

Left as `hypothesis`:

- Because S3 Access Points emit no event notification, the trigger is a scheduled poll. In the stage-3
  measurements the schedule stayed `DISABLED` and was invoked manually. A polling invocation was not run.
- Running on `provided.al2023` rather than the managed runtime `dotnet10`. Not tried.

`open`: whether every data operation on an AD-joined SVM needs DC reachability, and whether only
`HeadBucket` succeeds when the DC is unreachable ("Reachability on an AD-joined SVM and `HeadBucket`" above).

Not measured: security style `unix` and `mixed`, ONTAP versions other than 9.19.1P3, Multi-AZ, other
volume layouts, invoiced amounts, and the time to create the Lambda.
