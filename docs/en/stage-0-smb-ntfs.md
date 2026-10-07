# Stage 0: the SMB and NTFS starting point

> Evidence tier: `hypothesis` (not measured). The steps are a plan and the predictions are
> pre-measurement hypotheses, replaced by records.

Build the starting configuration where a .NET Framework 4.8 app on a Windows EC2 reads and writes
the NTFS-security-style target volume over SMB, and record the Probe's five behaviors as the stage-0
baseline.

## Planned steps

1. Create the base stack (see "Verification environment")
2. Configure ONTAP (SMB share, NTFS ACLs, create `seed/`, `probe/`, `out/`)
3. Place synthetic data in `seed/`
4. Mount SMB on the Linux EC2 (the second measuring client)
5. Build and deploy the source app
6. Take the inventory and the boundary record (`b0`)
7. Run the Probe (Windows and Linux, two clients)

## What to confirm at the boundary

- The SVM CIFS domain has a discovered domain controller (not judged by lifecycle alone)
- The target volume's security style is `ntfs`
- All five Probe `outcome` values are present and the two-client pairs are `cross-host`
- The `seed/` inventory (relative path, size, SHA-256) is included in the boundary record

## Predictions (unverified)

- With no NFS path in stage 0, the `b0` inventory is expected to be the single Windows-side one.
- The five behavior predictions are in the "Sample app" table and are all hypotheses.

## Measured results (verified)

> Evidence tier: the bullets below are `verified`. Measured on 2026-10-07, region ap-northeast-1,
> ONTAP 9.19.1P2, FSx for ONTAP Single-AZ (1,024 GiB / 128 MBps), NTFS volume `appdata`, on an SVM
> joined to AWS Managed Microsoft AD (Standard). IDs and IPs are masked
> (`fs-0123456789abcdef0` / `123456789012` / `10.0.x.x`). Everything outside this section stays an
> untested hypothesis.

- The SVM CIFS domain has a discovered domain controller (an `ms_dc` in `state=ok`), confirmed with
  the ONTAP REST `GET /api/protocols/cifs/domains/{svm-uuid}?fields=discovered_servers`. The
  `active-directory` collection alone returns zero records and is not sufficient to judge this.
- The target volume `appdata` has security style `ntfs`, `snaplock.type` `non_snaplock`, and
  `snapshot_locking_enabled` false. The SVM root volume is also `non_snaplock`; every irreversible
  feature is off.
- The Windows .NET Framework 4.8 app (DocIntake) built with the MSBuild v4.8 that ships with Windows
  Server, without going to the internet (the SDK-style fallback was not needed).
- The second SMB client on the Linux EC2 connected with `sec=ntlmssp` (`sec=krb5` was not needed).
- The synthetic `seed/` data (6 files) was placed from Windows over SMB and its Windows inventory
  (relative path, size, SHA-256) was captured into `b0`. `appsvc` can read every file over SMB and
  the SHA-256 values match the source. The NTFS ACLs grant `appsvc` modify and `appreader` read with
  an explicit deny-write ACE.
- The three single-client behaviors (`case-sensitivity`, `path-separator`, `acl-evaluation`) were
  `outcome=measured` on both Windows (SMB) and Linux (SMB). `path-separator` on the Linux SMB client
  had the `\`-containing name rejected (`rejected=true`, `errno=22`); that rejection itself is
  recorded as the observation. This differs from the prior prediction that Linux would create a
  differently named file without an exception, and the comparison with NFS at stage 1 is the point.

## Withdrawn claim

- The stage-0 record also stated `observed.topology=cross-host` for the two-client behaviors
  `file-locking` and `write-visibility`. That claim is withdrawn. In the stage-0 Probe each host
  locked or wrote its own file independently; the interaction between the two hosts (one host's
  access while the other holds a lock, the time until one host's write is visible to the other) was
  not measured. `cross-host` was a label added when the results were merged, not an observation.
  These two behaviors have no stage-0 baseline.
- The coordinated two-client measurement was first made at stage 1
  ([stage 1](stage-1-multiprotocol.md)). The environment then already had the NFS export, so the
  SMB-to-SMB pair is also treated as a stage-1 configuration value, not read back as a stage-0
  baseline.

## Findings from this stage (folded back into the procedure)

- The ONTAP REST share-ACL, file-security and `files` endpoints require the SVM / volume UUID in the
  path, not the name. NTFS ACEs must set `apply_to` to `this_folder`/`sub_folders`/`files`; without
  it the inherited ACEs on child files are folder-scoped, so a directory lists but file content
  cannot be read.
- The FSx for ONTAP management endpoint's certificate CN is the management DNS name while the
  scripts reach it by the management IP, so the ONTAP REST `curl` needs its TLS verification
  adjusted.
- SSM Run Command runs as SYSTEM, so the share is accessed only after establishing an `appsvc` SMB
  session.
