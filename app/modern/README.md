# app/modern: DocIntake migrated to .NET 10

This directory holds the migrated version of the sample application in [`../legacy`](../legacy):
.NET 10 (`net10.0`), with storage access through NFS and through Amazon FSx for NetApp ONTAP S3 Access
Points. It is the tracked-file export of one branch of a private working repository, so it has no
history of its own. Stage guides: [stage 2 (ja)](../../docs/ja/stage-2-aimf-modern-dotnet.md),
[stage 2 (en)](../../docs/en/stage-2-aimf-modern-dotnet.md),
[stage 3 (ja)](../../docs/ja/stage-3-s3-access-points.md),
[stage 3 (en)](../../docs/en/stage-3-s3-access-points.md).

## Layout

| Path | Contents |
|---|---|
| `src/DocIntake.Core` | `IFileStore` and its implementations: `SmbFileStore`, `NfsFileStore`, `S3FileStore`, plus `StorePaths` |
| `src/DocIntake.Worker` | Console Worker. Reads documents through `IFileStore` and writes `index.json` |
| `src/DocIntake.BackgroundWorker` | Candidate A: a separate .NET 10 `BackgroundService` for Linux/NFS/systemd. Deploy assets in [`deploy/`](deploy); guide: [candidate A (ja)](../../docs/ja/candidate-a-background-nfs.md), [candidate A (en)](../../docs/en/candidate-a-background-nfs.md) |
| `src/DocIntake.Probe` | Console Probe. Emits `appmod-probe/1` JSON records for the five measured behaviors (`--store s3` selects the S3 path) |
| `src/DocIntake.Lambda` | Stage-3 Lambda handler. Packaging steps are in [`BUILD.md`](src/DocIntake.Lambda/BUILD.md) |
| `tests/DocIntake.Core.Tests` | xUnit tests for Core, Worker, Probe, and the Lambda handler |

## Provenance

The code is the output of AI Modernization Flow (AIMF) v0.11.0 for `app/legacy`, followed by three
commits made after the AIMF session. The short SHAs below belong to the private record repository and
cannot be resolved from this repository. Comments in the source carry ledger labels (`AP-n`, `MP-n`,
`ADR-n`) and one commit reference that point into that private record; they are left as they were
exported.

| Step | Private SHA | What it contains |
|---|---|---|
| AIMF output (branch `modernize/amzn2023-x86_64`) | `8d8e0b4` | .NET Framework 4.8 to `net10.0`; SDK-style projects; `System.Text.Json` for `index.json`; `S3FileStore` |
| Post-session commit 1 | `38610a4` | ADR-3 record shape: on non-Windows the `acl-evaluation` record carries `precheck="skipped"`, `precheck_reason`, and `io_can_write`; the Windows path emits `precheck_can_write`. Schema `appmod-probe/1` is unchanged |
| Post-session commit 2 | `adfffc4` | Probe `--store s3` with the five S3 behaviors. Two Core fixes: `S3FileStore.Key` no longer rewrites `\`, and HTTP 403 maps to `UnauthorizedAccessException` |
| Post-session commit 3 | `4e188df` | Lambda handler `DocIntake.Lambda::DocIntake.Lambda.Function::Handler` and the in-process entries it calls |

The `--store s3` Probe covers the five behaviors this way:

- Case sensitivity: a control pair, `seed/docs/index.json` against `seed/Docs/Index.json`.
- Path separator: the key `probe/sep\check-s3.txt`.
- File locking: skipped on S3, which has no exclusive-open operation.
- ACL evaluation: a real `PutObject`, recorded as `io_put_allowed`.
- Write visibility: the key `probe/vis-<guid>.txt`.

What was checked when these commits were made: on macOS with SDK 10.0.400 the build had 0 warnings and
0 errors and 54 unit tests passed. The commits themselves recorded behavior on a live host or against
the real access point as not verified. The live results are in the stage guides linked above.

## Design decisions ADR-1 to ADR-5

Summarized from the private decision record. The stage-2 guide has a one-line table of the same
five; this section adds the reasons.

| ADR | Decision | Reason |
|---|---|---|
| ADR-1 | Target .NET 10 (LTS, `net10.0`). Storage is NFS plus S3 Access Points. The Lambda handler and its placement are outside AIMF's scope | The requirement is two paths over the same data: Linux over NFS, and serverless through S3 Access Points. .NET 8 and 9 reach end of support on 2026-11-10; .NET 10 is supported to 2028-11 (checked 2026-10). S3 Access Points are not equivalent to a bucket, so unsupported APIs, single-identity authorization, no inherited ACLs, and no event notifications are tracked as ledger items |
| ADR-2 | Non-functional scope: performance out, resource leaks out, fault tolerance in (limited), security in (limited) | The app is a short-lived console pair plus a class library, and the goal is functional behavior on two paths. Fault tolerance covers the storage error paths (lock contention, access denied on S3 Access Points, wrong-case names on NFS, write-visibility delay). Security covers the NTFS ACL replacement and the S3 authorization model. Production penetration testing is out of scope |
| ADR-3 | `SmbFileStore.CanWrite`: Windows keeps the NTFS ACL evaluation under an `OperatingSystem.IsWindows()` guard. Linux decides by real I/O: open the existing file for write, otherwise `CreateNew` a temporary file in the parent directory and delete it. POSIX mode bits are not used | The volume uses NTFS security style, and ONTAP evaluates the NTFS ACL through name mapping. Mode bits are not a faithful image of that decision; the outcome of a real open is what ONTAP actually decided. The result therefore differs by platform and is recorded that way |
| ADR-4 | `index.json` is compact JSON from `System.Text.Json` | The file is an internal manifest read by a JSON parser, so whitespace is not a contract. The success criterion changes from byte-for-byte equality to JSON equivalence: same keys, values, and array order |
| ADR-5 | `S3FileStore.OpenExclusive` throws `NotSupportedException` and is tracked (ledger item AP-14) | S3 and S3 Access Points have no exclusive-open operation, and an emulated lock would give false assurance under the S3 consistency model. Removing the method from `IFileStore` would break exclusive access on NFS and SMB. The Worker does not call it, and the Probe skips file locking on S3 |

## Build and test

Requires the .NET SDK 10 (`dotnet --version` reports 10.x; built here with 10.0.400). NuGet package
versions are exact in the `.csproj` files and there are no lock files, so restore reads nuget.org.
Run the commands from this directory:

```bash
cd app/modern
dotnet build DocIntake.sln -c Release
dotnet test DocIntake.sln -c Release
```

From the repository root, `make app-modern` runs the same build and test and fails when the SDK is
missing or is not version 10.

## Publish for a host without .NET

The Probe and the Worker run on Amazon Linux 2023 (x86_64) as self-contained builds. One observation:
each published directory was about 81 MB.

```bash
dotnet publish src/DocIntake.Probe  -c Release -r linux-x64 --self-contained true -o <probe-publish-dir>
dotnet publish src/DocIntake.Worker -c Release -r linux-x64 --self-contained true -o <worker-publish-dir>
```

## Package the Lambda function

Framework-dependent publish for x86_64 with the assemblies at the zip root. The runtime is `dotnet10`;
`provided.al2023` is not supported by this package ([`BUILD.md`](src/DocIntake.Lambda/BUILD.md)).

```bash
dotnet publish src/DocIntake.Lambda -c Release -r linux-x64 --no-self-contained -o <lambda-publish-dir>
(cd <lambda-publish-dir> && zip -qr <package>.zip .)
unzip -l <package>.zip | grep DocIntake.Lambda.dll
```

The handler is `DocIntake.Lambda::DocIntake.Lambda.Function::Handler`. The payload is
`{"mode":"worker"|"probe","run_id":"...","stage":"s3"}`. `stage` accepts only `s3`, and `run_id` must
match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. An invalid payload throws before any S3 call.

| Environment variable | Meaning |
|---|---|
| `S3_ACCESS_POINT_ARN` | ARN or alias of the S3 Access Point attached to the FSx for ONTAP volume. Required |
| `ARTIFACTS_BUCKET` | Bucket that receives the Probe JSON at `probe/<run-id>/lambda-s3.json`. Required |

Missing either variable fails the function at initialization. The stage-3 template also sets `STORE`
and `FS_IDENTITY`; the handler does not read them. Uploading the zip to the artifacts bucket is a
deploy-time step; the zip is never committed.

## Environment variables of the Worker

| Variable | Default | Meaning |
|---|---|---|
| `DOCINTAKE_STORE` | `smb` | Store kind: `smb`, `local`, `nfs`, or `s3`. Any other value exits with an error |
| `DOCINTAKE_ROOT` | `\\APPMODSVM01\appdata` for SMB, `/mnt/appdata` for NFS | Share or mount root. For S3, the access point alias or ARN |
| `DOCINTAKE_S3_PREFIX` | empty | Optional key prefix for the S3 store |
| `DOCINTAKE_STAGE` | `s0` | Stage name for the `out/<stage>` output directory |

`APPMODSVM01` is the default NetBIOS name used by the scripts and templates in this repository. It is
a placeholder, not a live identifier.

## Limits

- Ledger labels and the one commit reference in comments cannot be resolved from this repository.
- Results from running this code on live hosts and in Lambda are single observations; their
  environment and evidence tier are stated in the stage guides, not here.
- `app/legacy` (.NET Framework 4.8) is not built by any gate. Only `app/modern` is.
