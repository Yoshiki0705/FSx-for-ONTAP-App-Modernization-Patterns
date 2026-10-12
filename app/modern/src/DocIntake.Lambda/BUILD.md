# DocIntake.Lambda deployment package

Handler: `DocIntake.Lambda::DocIntake.Lambda.Function::Handler`.

Payload: `{"mode":"worker"|"probe","run_id":"...","stage":"s3"}`. `stage` accepts only `s3`; `run_id`
must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. An invalid payload throws before any S3 call.

Environment: `S3_ACCESS_POINT_ARN` (the access point ARN or alias) and `ARTIFACTS_BUCKET`. The
template also sets `STORE` and `FS_IDENTITY`; the handler does not read them.

## Managed runtime `dotnet10` (the template default)

Framework-dependent publish for x86_64 (the template sets no `Architectures`, so Lambda uses x86_64),
then zip the publish output with the assemblies at the zip root. Run from the repository root:

```bash
dotnet publish src/DocIntake.Lambda -c Release -r linux-x64 --no-self-contained -o /tmp/docintake-pkg-aimf-out
(cd /tmp/docintake-pkg-aimf-out && zip -qr /tmp/docintake-pkg-aimf.zip .)
unzip -l /tmp/docintake-pkg-aimf.zip | grep DocIntake.Lambda.dll
```

The zip is never committed. Uploading it to the artifacts bucket at the template's `LambdaPackageKey`
(default `lambda/worker.zip`) is a deploy-time step that needs an approved `stage3` estimate; it is not
part of the build.

## OS-only runtime `provided.al2023` (not supported)

`provided.al2023` runs a `bootstrap` executable, so it needs an executable entry point hosting
`Amazon.Lambda.RuntimeSupport` and a `--self-contained true` publish. This class library has neither;
selecting `provided.al2023` with this zip would fail at init.
