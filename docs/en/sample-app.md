# Sample app: DocIntake structure and the five behaviors

> Evidence tier: `hypothesis` (not measured). The "prediction" column is a hypothesis made before
> measurement; stages 0 and 1 replace it with records.

`DocIntake` is a small self-written app that inspects documents dropped in an inbox folder and
writes an index and summaries. It has no database and no authentication. It is self-written rather
than an existing open-source app so the observation points can be designed in.

## Sample app structure

| Project | Kind | Contents |
|---|---|---|
| `DocIntake.Core` | class library | `IFileStore` (`List`, `Read`, `Write`, `OpenExclusive`, `CanWrite`) and `SmbFileStore` |
| `DocIntake.Worker` | console | reads `seed/inbox/`, writes `out/<stage>/index.json` and `reports/` |
| `DocIntake.Probe` | console | measures the five behaviors and emits JSON |

The source is .NET Framework 4.8, C# 5, an old-style `.csproj`, no NuGet dependency. Storage is
selected by a single `Store` appSetting, shaped like Bob's Used Books Classic `FileService`, so the
AIMF observations MP-10 and MP-25 apply. Because the app is a console (no `Web` layer), the ASP.NET
web-layer migration and the case mismatch planted in a `Web` static reference do not apply; with no
NuGet dependency, the `packages.config` observations do not apply either.

## The five behaviors

The source code carries one "does not surface on Windows + SMB" construct per behavior, exactly
once each.

| ID | Behavior | Where planted | Prediction (unverified) |
|---|---|---|---|
| `case-sensitivity` | case | reference `Docs\Index.json` vs real `docs/index.json` (`StorePaths`) | SMB treats as equal, NFS does not |
| `path-separator` | separator | `\` concatenated in `StorePaths.Combine` | on Linux the `\` becomes part of the file name |
| `file-locking` | locking | `FileShare.None` in `OpenExclusive` (`SmbFileStore`) | SMB-to-SMB refuses; cross-protocol unconfirmed |
| `acl-evaluation` | ACL / evaluation | `File.GetAccessControl` pre-check (`SmbFileStore.CanWrite`) | on Linux the pre-check is unavailable and disagrees with I/O |
| `write-visibility` | write visibility | immediate read-back assumed visible (`Worker`) | NFS lags by the attribute-cache interval |

.NET Framework 4.8 does not run on Linux, so for stages 0 and 1 the Linux side is measured by
`scripts/probe_peer.py` (stdlib only), covering the same five behaviors.

## Probe output format

The Probe emits observations only, as JSON. `outcome` is one of `measured`, `error`, `skipped`.
The `ok` / `differs` / `not-comparable` verdict is assigned by `scripts/compare-probe.py` against
stage 0.

```json
{
  "schema": "appmod-probe/1",
  "run_id": "s1-20261007T010203Z",
  "started_at": "2026-10-07T01:02:03Z",
  "stage": 1,
  "role": "writer",
  "behaviors": [
    {"id": "file-locking", "outcome": "measured", "observed": {"topology": "cross-host"}}
  ]
}
```

## Build prerequisites

AWS Tools for PowerShell is preinstalled on Windows-based AMIs (the option varies by AMI). Whether
the Windows Server 2022 AMI ships MSBuild for .NET Framework 4.x is unconfirmed (U18); if it does
not, the build switches to an SDK-style `.csproj` with reference assemblies, built for `net48` from
the work terminal. That switch means the old-style `.csproj` observations no longer apply, which is
recorded in stage 2.

## Predictions (unverified)

- Every prediction above is a hypothesis and is unmeasured on FSx for ONTAP.
- The Probe continues past any behavior that fails to measure, recording the failure as
  `outcome: error` with the exception type.
