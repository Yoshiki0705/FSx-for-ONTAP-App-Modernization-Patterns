# Stage 0: the SMB and NTFS starting point

> Evidence tier: shown per section. "Measured results", "Re-measurement on the rebuilt environment",
> "Withdrawn claims" and "Findings from this stage" are `verified` (each carries a run id and a date;
> one-off results say "single observation"). "Steps" and "What to confirm at the boundary" describe
> the plan. What this stage did not measure is listed under "Not measured" at the end.

Build the starting configuration where a .NET Framework 4.8 app on a Windows EC2 reads and writes
the NTFS-security-style target volume over SMB, and record the Probe's five behaviors as the stage-0
baseline.

## Steps

Carried out in two builds, 2026-10-07 (ONTAP 9.19.1P2) and 2026-10-11 (9.19.1P3), in this order.

1. Create the base stack (see [Verification environment](environment.md))
2. Configure ONTAP (SMB share, NTFS ACLs, create `seed/`, `probe/`, `out/`)
3. Place synthetic data in `seed/`
4. Mount SMB on the Linux EC2 (the second measuring client)
5. Build and deploy the source app
6. Take the inventory and the boundary record (`b0`)
7. Run the Probe (Windows and Linux, two clients)

## What to confirm at the boundary

- The SVM CIFS domain has a discovered domain controller (not judged by lifecycle alone)
- The target volume's security style is `ntfs`
- All five Probe `outcome` values are present and the two-client pairs are `cross-host` (judged by `probe_merge.py`; see "Withdrawn claims" below)
- The `seed/` inventory (relative path, size, SHA-256) is included in the boundary record

## Measured results (verified)

> Evidence tier: the bullets below are `verified`. Measured on 2026-10-07 (run id
> `s0-20261007T104712Z`), region ap-northeast-1, ONTAP 9.19.1P2, Amazon FSx for NetApp ONTAP
> (FSx for ONTAP below) Single-AZ (1,024 GiB / 128 MBps), NTFS volume `appdata`, on an SVM joined to
> AWS Managed Microsoft AD (Standard). IDs and IPs are masked
> (`fs-0123456789abcdef0` / `123456789012` / `10.0.x.x`). Each item is a single observation. The
> re-measurement on the rebuilt environment (2026-10-11) is in the next section.

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
- With no NFS path in stage 0, the `b0` inventory is the single Windows-side one (`inventories.linux`
  in `b0.json` is empty and there are 0 name mappings; confirmed in the `b0` of the rebuilt
  environment on 2026-10-11, ONTAP 9.19.1P3).
- Of the single-client behaviors, `path-separator` and `acl-evaluation` were `outcome=measured` on
  both Windows (SMB) and Linux (SMB). `path-separator` on the Linux SMB client had the
  `\`-containing name rejected (`rejected=true`, `errno=22`); that rejection itself is recorded as the
  observation. This differs from the prior prediction that Linux would create a differently named
  file without an exception, and the comparison with NFS at stage 1 is the point.
- The `case-sensitivity` record from the same Probe is not used as a conclusion. The reference
  `Docs\Index.json` was resolved against the volume root while the real file is
  `seed/docs/index.json`, and the record has no positive control (`real`) that resolves an existing
  file by the same route. Windows (SMB) does not distinguish case, so `resolved=false` there is
  explained by the missing control. The re-measurement with a positive control is in the next
  section. A measurement without a positive control reads equally as "case-sensitive" or "not
  found" when the result is false, so it is not a conclusion. When taking a Probe from now on, treat
  only records whose `real_resolved` is true as a `case-sensitivity` result.

## Re-measurement on the rebuilt environment (verified, once each)

> Evidence tier: `verified`. Everything below is ap-northeast-1, ONTAP 9.19.1P3, Single-AZ, one file
> system, one SVM, one volume `appdata` (security style `ntfs`), measured before the stage-1 NFS setup
> (export policy `appmod_nfs` and name mappings) was applied. The pre-run boundary record
> (`s0-20261011T065443Z`) shows the two built-in export policies (one rule each), 0 name mappings and an
> empty Linux inventory. Each result is a single observation; repeatability is not confirmed.

### Two coordinated SMB clients (run id `s0-20261011T065443Z`, 2026-10-11)

Windows and Linux (cifs) shared one sync id (`s0-20261011T065443Z-p1` to `p4`), and a pair was called
`cross-host` only when both records showed overlapping time windows. `probe_merge.py` judged all 4
pairs `cross-host` (`reasons` empty).

| Pair | Holder / contender | How the holder holds | Contender while held | After release |
|---|---|---|---|---|
| 1 | Windows / Linux (cifs) | `FileShare.None` | read and write opens refused (`errno=16`) | acquired |
| 2 | Linux (cifs) / Windows | `fcntl.lockf LOCK_EX` | open succeeds, read is a lock violation (`0x80070021`), exclusive open is a sharing violation (`0x80070020`) | acquired |

| Pair | Writer / reader | Result | From save completed to readable |
|---|---|---|---|
| 3 | Windows / Linux (cifs) | read | 81 ms |
| 4 | Linux (cifs) / Windows | read | 8,122 ms |

- The reader repeated a listing and a read every 100 ms, so the resolution is about 100 ms. These are
  not performance claims.
- The difference, about 8 s when Linux writes and Windows reads against under 100 ms in the other
  direction, also appeared in the 2026-10-07 stage-1 run ([stage 1](stage-1-multiprotocol.md)). The
  cause was not investigated.
- What to do: where a Windows client reads files written by Linux, do not assume the file is readable
  right after the write; design for the delay (a wait or retry). The delay measured here is a value
  for one configuration; do not apply its length to other configurations.

### Case sensitivity (Windows SMB, with a positive control; run id `s4c-20261012T034403Z`, 2026-10-12)

Windows (SMB, UNC share) requested a reference with a changed spelling, `seed/Docs/Index.json`, and a
positive control, the real `seed/docs/index.json`. The reference with the wrong spelling resolved
(`resolved=true`) and so did the positive control (`real_resolved=true`). SMB does not distinguish
case. Single observation. Results per access path, including Linux cifs and NFS, are in
[stage 1](stage-1-multiprotocol.md).

### What the SMB results do not cover in this environment

- Other ONTAP versions, Multi-AZ, other volume layouts and multiple SVMs were not measured.
- The 2026-10-07 run (9.19.1P2) and the 2026-10-11 run (9.19.1P3) are separate builds with different
  versions. This document did not examine whether the version difference changed the results.

## Withdrawn claims

- The 2026-10-07 stage-0 record also stated `observed.topology=cross-host` for the two-client
  behaviors `file-locking` and `write-visibility`. That claim is withdrawn. In that run's Probe each
  host locked or wrote its own file independently; the interaction between the two hosts (one host's
  access while the other holds a lock, the time until one host's write is visible to the other) was
  not measured. `cross-host` was a label added when the results were merged, not an observation.
- `topology` is a self-reported label that is printed with a fixed value even when the Probe runs on
  one host, so it is not evidence that two hosts coordinated. A pair counts as coordinated only when
  `probe_merge.py` confirmed a matching sync id and overlapping time windows and judged it
  `cross-host` with empty `reasons`. What to do: judge coordination by whether the merged `reasons` is
  empty, not by the value of `topology`.
- The first coordinated two-client measurement was made at stage 1 on 2026-10-07
  ([stage 1](stage-1-multiprotocol.md)). That environment already had the NFS export, so the
  SMB-to-SMB pairs of that run are not read back as a stage-0 baseline. The two-client baseline for an
  SMB-only configuration was taken before the stage-1 NFS setup was applied, in the 2026-10-11 run
  `s0-20261011T065443Z` ("Two coordinated SMB clients" above). That baseline is a single observation
  per pair.

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
- When reading two-host coordination in a merged Probe result, use the `probe_merge.py` judgement,
  not `topology` (see "Withdrawn claims" above).

## Not measured

The following were not measured in this stage. Do not use them as conclusions.

- Behavior with security style `unix` and `mixed`
- ONTAP versions other than 9.19.1P2 and 9.19.1P3
- Multi-AZ, multiple SVMs, or layouts beyond one volume
- Invoiced amounts (cost estimates are in [Verification environment](environment.md) and are not invoices)
