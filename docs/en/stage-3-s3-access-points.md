# Stage 3: partial serverless via S3 Access Points

> Evidence tier: `hypothesis` (not measured). The steps are a plan and the predictions are
> pre-measurement hypotheses, replaced by records.
> The AWS specifications below (supported operations, runtime deprecation dates, billing items) are
> facts stated in documentation, each with a source.
> Whether this configuration works has not been confirmed, because it has not been deployed.

Attach FSx for ONTAP S3 Access Points to the `appdata` target volume of Amazon FSx for NetApp ONTAP and
run the Worker and the Probe (S3 variant) from AWS Lambda. The volume is not cloned and the data is
not moved.

## Planned configuration

| Element | Content |
|---|---|
| Lambda function | `DocIntake.Lambda` on .NET 10. Handler `DocIntake.Lambda::DocIntake.Lambda.Function::Handler`, 512 MiB memory, 120-second timeout |
| Runtime | Default is `dotnet10` (managed runtime, Amazon Linux 2023). The other option is `provided.al2023`, which needs a separately built self-contained bootstrap (the current zip does not run on it) |
| Why `dotnet8` was dropped | In the Lambda runtime list, `dotnet8` is deprecated on 2026-11-10, less than a month away. `dotnet10` is 2028-11-14 (checked 2026-10-11). In newer Regions, Lambda does not offer a runtime that deprecates within 6 months |
| Network | The VPC and primary subnet come from the `appmod-base` outputs (`Fn::ImportValue`). The security group is dedicated to the Lambda, with no inbound rules and outbound limited to HTTPS (443). Traffic to S3 Access Points and the artifacts bucket goes through the S3 gateway endpoint in `appmod-base` |
| Environment variables | `S3_ACCESS_POINT_ARN` (`S3AccessPoint.ResourceARN`, passed as the SDK bucket argument), `ARTIFACTS_BUCKET` (the artifacts bucket), `STORE=s3`, `FS_IDENTITY` |
| IAM | `s3:ListBucket` on the S3 Access Points ARN, `s3:GetObject` and `s3:PutObject` on its `/object/*`, and read/write on the artifacts bucket only. `s3:DeleteObject` is not granted (stage-3 code does not delete) |
| Schedule | An Amazon EventBridge Scheduler schedule `appmod-stage3-worker-poll` (every 5 minutes) is created `DISABLED`. Measurements are invoked manually |

## Planned steps

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

## The five behaviors in the S3 variant

A behavior that does not hold under the S3 API is recorded as `skipped`, with the reason in
`observed.reason`.

| Behavior | Handling in the S3 variant | `outcome` |
|---|---|---|
| `case-sensitivity` | Request `Docs/Index.json` and `docs/index.json` with `GetObject` and record whether each was found | `measured` |
| `path-separator` | Run `PutObject` with a key containing `\` (`probe\sep-check-s3.txt`) and record whether it reads back. The file names produced on the SMB and NFS sides are checked in each host's inventory | `measured` |
| `file-locking` | Not measured, because the S3 API has no operation that opens exclusively | `skipped` |
| `acl-evaluation` | Recorded as `skipped` because the S3 API has no pre-check; the success or failure of an actual `PutObject` to `probe/<run-id>/deny-appsvc.txt` is recorded | `measured` |
| `write-visibility` | `PutObject` a marker object and record its key | `measured` |

`DeleteObject` is among the operations usable on S3 Access Points. The write-permission check in
`S3FileStore` (`CanWrite`) writes a unique key with `PutObject` and then removes it with `DeleteObject`.
The stage-3 Probe does not call this check, however. The outcome of `PutObject` and `GetObject` while
Windows holds the file open with `FileShare.None` (`observed.under_smb_lock`) is measured with a pair of
hosts, and is not part of this single invocation.

## Scope of the stage-3 estimate

`estimate.py --target stage3` does not re-estimate the `appmod-base` hours; it counts only the add-on
stack. The base hours are included in the `base` (and `extend`) estimates.

| Item | How it is counted | Source of the unit price (ap-northeast-1) |
|---|---|---|
| Lambda requests | Number of manual invocations (default 20, set with `--invocations`) | Price List `AWSLambda`, `APN1-Request` |
| Lambda duration | Invocations × 0.5 GB × the 120-second ceiling | Price List `AWSLambda`, `APN1-Lambda-GB-Second` (first-tier price) |
| S3 Access Points requests | An upper bound assuming 100 per invocation | Not retrievable. The FSx for ONTAP pricing page says S3 requests through S3 Access Points are charged but lists no per-request price, and Price List `AmazonFSx` has no matching usagetype (checked 2026-10-11). The estimate records `not retrieved` and excludes it from the total |
| CloudWatch Logs ingestion | An upper bound assuming 1 MiB per invocation | Price List `AmazonCloudWatch`, `APN1-DataProcessing-Bytes` |
| EventBridge Scheduler | 0 invocations because it is `DISABLED`, $0 | None |
| S3 data transfer | Not counted | Per the pricing page, data transfer through S3 Access Points follows S3 data transfer pricing |

The estimate also records the `parameters` to be approved (`BaseStackName`, `LambdaPackageKey`,
`LambdaHandler`, `LambdaRuntime`, `FileSystemIdentityUser`, and `ArtifactsBucketName` only when
specified). `deploy.sh stage3` passes them through unchanged, and rejects an estimate whose
`parameters` are missing or empty, as it does for `base` (exit code 2).

## What to confirm at the boundary

- The target volume UUID and the `seed/` inventory are identical to stage 0
- The security style is still `ntfs`
- The S3 Access Points target is `appdata`

## Sources for the claims

S3 Access Points constraints are cited per claim from Hub notes (contents are not copied).

- S3 Access Points constraints (unsupported APIs, single-identity authorization, ACLs not inherited, no event notification): [s3-access-point-constraints](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/data-utilization/notes/s3-access-point-constraints.md)
- The two-layer authorization: [access-point-authorization-layers](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/ja/domains/security-governance/notes/access-point-authorization-layers.md)
- Operations usable on S3 Access Points (including `DeleteObject`; Object Lock and conditional writes are not usable): [Access point compatibility](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/access-points-for-fsxn-object-api-support.html) (checked 2026-10-11)
- Lambda runtimes and deprecation dates: [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) (checked 2026-10-11)
- Billing for S3 requests and data transfer: [Amazon FSx for NetApp ONTAP pricing](https://aws.amazon.com/fsx/netapp-ontap/pricing/) (checked 2026-10-11)

## Predictions (unverified)

- S3 Access Points are expected to attach on this configuration (`SINGLE_AZ_1`, AD-joined SVM, NTFS volume) (unconfirmed).
- Because S3 Access Points emit no event notification, the trigger is a scheduled poll. In the stage-3 measurements, the schedule stays `DISABLED` and is invoked manually.
- Reads and writes by the Worker and the Probe are expected to pass within the IAM scope above (no `DeleteObject`) (unconfirmed).
