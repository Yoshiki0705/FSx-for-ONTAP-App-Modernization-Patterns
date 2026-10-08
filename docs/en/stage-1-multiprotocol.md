# Stage 1: adding NFS to the same volume
>
> Evidence tier: the "Measured results (verified)" and "Findings from the measurement" sections are
> `verified`. The other sections (steps, checks, predictions, untested hypotheses) are plans and
> hypotheses, not measured.
Add NFS access to the stage-0 target volume. The volume is not cloned, rebuilt or moved. The
security style stays NTFS.

## Planned steps

1. Configure the export policy and name mapping (both directions) on ONTAP
2. Mount NFS on the Linux EC2
3. Take the inventory (both Windows and Linux) and the boundary record (`b1`)
4. Confirm the single-volume invariant (`b0` vs `b1`)
5. Run the Probe (both SMB and NFS)

## What to confirm at the boundary

- The target volume UUID is identical to stage 0
- The `seed/` inventory matches on both the Windows (SMB) and Linux (NFS) sides
- The security style is still `ntfs`
- On an NFS denial, the Windows-side effective access and the name mapping are recorded too

## Measured results (verified)
>
> Measured on 2026-10-07, region ap-northeast-1, ONTAP 9.19.1P2. Amazon FSx for NetApp ONTAP
> (FSx for ONTAP below) Single-AZ (1,024 GiB / 128 MBps), NTFS volume `appdata`, on an SVM joined to
> AWS Managed Microsoft AD (Standard). Two clients: a Windows Server 2022 EC2 (SMB 3,
> DocIntake.Probe) and an Amazon Linux 2023 EC2 (SMB via `cifs` with `sec=ntlmssp`, NFS with
> `vers=4.1` and `sec=sys`, both through `probe_peer.py`). IDs and IPs are masked
> (`fs-0123456789abcdef0` / `123456789012` / `10.0.x.x`). Times come from the two hosts' clocks; the
> offset against the Amazon Time Sync Service was 1.5 to 2.1 ms on Windows and under 0.01 ms on Linux.

### ONTAP configuration and idempotency

- On its first run `stage1-nfs.sh` created export policy `appmod_nfs` (one rule: clients = the
  primary subnet CIDR, `nfs4`, `ro_rule`/`rw_rule` `sys`, `superuser` `none`) and assigned it to
  `appdata` by volume UUID (it was `default` before). It also created UNIX users `appsvc` (uid 10001)
  and `appreader` (uid 10002) and four name mappings (`win_unix` and `unix_win` for both users).
- The second run reported every object as already present and unchanged, with exit code 0.
- The security style was `ntfs` before and after. The volume was not cloned, rebuilt or moved
  (that adding NFS needs no clone is in the Hub note
  [adding-a-protocol-does-not-need-a-clone](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/adding-a-protocol-does-not-need-a-clone.md)).
- The name-mapping replacement `APPMOD\\appsvc` (`\\\\` in the REST JSON) resolved, in ONTAP's
  mapping result, to the Windows name `APPMOD\appsvc` with a single `\`. Both directions, UNIX to
  Windows and Windows to UNIX, resolved as intended for `appsvc` and `appreader`.
- The script sets no default user. The SVM already had CIFS `default_unix_user` set to `pcuser`, and
  no default Windows user on the NFS side.

### NFS mount and the single-volume invariant

- The Linux EC2 mounted `/appdata` over NFSv4.1 (`sec=sys`). The stage-0 SMB mount stays on the same
  host alongside it.
- At `b1` the target volume UUID was identical to `b0`. The inventory of the 6 `seed/` files
  (relative path, size, SHA-256) matched `b0` from Windows (SMB) and from Linux (NFS, uid 10001), and
  the two matched each other. The only top-level paths were `seed/`, `probe/` and `out/`, and the
  security style was `ntfs`.
- No volume had snapshot locking enabled and no SnapLock volume existed (`appdata` and the SVM root
  volume were scanned).

### The three single-client behaviors

| Behavior | Windows (SMB) | Linux (SMB) | Linux (NFS) |
|---|---|---|---|
| `path-separator` (creating a name containing `\`) | treated as a separator | rejected (`errno=22`) | created; `\` is part of the name |
| `case-sensitivity` (two files differing only in case) | the reference `Docs\Index.json` does not resolve | not two files | two separate files |
| `acl-evaluation` | the pre-check reports writable | no pre-check available | no pre-check available |

- `sep\check.txt` created over NFS appears on Windows under the 8.3 short name `SEPCHE~1.TXT` and can
  be read. The name the Linux SMB client rejected at stage 0 can be created over NFS on the same
  volume.
- `casecheck.txt` and `CaseCheck.txt` created over NFS appear on Windows as two entries,
  `casecheck.txt` and `CASECH~2.TXT`, and both can be read.
- The three behaviors on Windows and Linux (SMB) were the same observations as at stage 0
  (`compare-probe.py` verdict `ok`). Linux (NFS) has no stage-0 counterpart to compare with.

### Behaviors that need two clients

The two hosts acted on the same file at the same time and synchronized through the artifacts S3
bucket, not through the volume under test. A pair was labeled `cross-host` only when both sides
carried the same sync id and both records showed the timelines overlapping (the contender's attempt
during the holder's lock, the reader's observation after the writer's save). All 8 pairs met this.
The SMB-to-SMB pairs are values under the stage-1 configuration (with the NFS export present), not a
stage-0 baseline.

| Holder / contender | How the holder holds | Contender while held | After release |
|---|---|---|---|
| Windows (SMB) / Linux (SMB) | `FileShare.None` | read and write opens refused (`errno=16`) | acquired |
| Linux (SMB) / Windows (SMB) | `fcntl` exclusive lock | open succeeds, read is a lock violation, exclusive open is a sharing violation | acquired |
| Windows (SMB) / Linux (NFS) | `FileShare.None` | read and write opens refused (`errno=13`) | acquired |
| Linux (NFS) / Windows (SMB) | `fcntl` exclusive lock | open succeeds, read is a lock violation, exclusive open is a sharing violation | acquired |

| Writer / reader | Result | From save completed to readable |
|---|---|---|
| Windows (SMB) / Linux (SMB) | read | 99 ms |
| Linux (SMB) / Windows (SMB) | read | 7,849 ms |
| Windows (SMB) / Linux (NFS) | read | 88 ms |
| Linux (NFS) / Windows (SMB) | read | 8,085 ms |

- The reader repeated a listing and a read every 100 ms, so the resolution is about 100 ms. Each pair
  was measured once and repeatability is not established. These are not performance claims.
- The time until Windows could read over SMB a file written over NFS (8,085 ms) was close to the time
  for a file written over SMB (7,849 ms). In this configuration the delay of about 8 s came with
  Windows being the reader, not with the writer's protocol. The reverse direction (Windows writes,
  Linux reads) was within about 100 ms (one polling interval) over both SMB and NFS.
- Windows `FileShare.None` also refused the NFS opens. The Linux `fcntl` lock stopped the Windows read
  as a lock violation. Exclusion held across protocols in both directions.

### NFS denials and their causes

The NFS-side view (`ls -l`) showed mode `d---------` and owner `root` or `nobody` even to `appsvc`,
which could read and write, so it gave no clue to the cause of a denial. Causes were attributed with
ONTAP effective-permissions, the name-mapping result and EMS events (that the NFS-side view does not
explain NTFS denials is in the Hub note
[nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)).

| NFS principal | Result | Windows-side mapping | Cause |
|---|---|---|---|
| root (uid 0) | denied from the top level | `superuser none` makes it the anonymous user `pcuser`, which maps to no Windows name | missing name mapping; EMS logged `secd.nfsAuth.noNameMap`, naming the absence of a default Windows user |
| uid 10001 (`appsvc`) | read and write succeed | `APPMOD\appsvc` | no denial |
| uid 10002 (`appreader`) | lists the top level but cannot enter `seed/` | `APPMOD\appreader` | NTFS ACL; effective-permissions has no `execute` (traverse) |

- `appreader`'s effective-permissions, looked up by Windows name or by UNIX name, are `read`,
  `read_ea`, `read_attributes`, `read_control` and `synchronize`, without `execute`. The ACL gives
  `appreader` read rights and a write deny only, and no traverse right.
- The same `appreader`, under the same ACL, could read the files under `seed/` over SMB. The ACL was
  not changed. Why SMB and NFS differ here is under "Untested hypotheses" below.
- On an NTFS-style volume the Windows ACL is what is evaluated, and an NFS principal is first mapped
  to a Windows name and then evaluated (Hub note
  [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)).
  root was denied at the mapping step and `appreader` at the ACL step, in that order.

## Findings from the measurement

- NFS principals with no mapping (root included) surfaced as denials because no default Windows user
  is set. This matched the prediction, and EMS confirmed the cause.
- The default user for the opposite direction (Windows name to UNIX name, CIFS `default_unix_user`)
  has the SVM default `pcuser`. In that direction "an unmapped principal is denied" does not hold.
  Whether this value affects SMB access was not tested.
- The NFS Probe and inventory had to run as the UNIX user `appsvc` (uid 10001), not as the Run Command
  root: with `sec=sys` the NFS principal is the local uid, and root is replaced by the anonymous user.
- The stage-0 `file-locking` and `write-visibility` did not measure the two-host interaction. The
  corresponding claim in the stage-0 document is withdrawn ([stage 0](stage-0-smb-ntfs.md)).

## Sources for the claims

General multiprotocol findings are cited per claim from Hub notes (contents are not copied).

- Adding NFS needs no clone: [adding-a-protocol-does-not-need-a-clone](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/adding-a-protocol-does-not-need-a-clone.md)
- Security style and permission evaluation: [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)
- The NFS-side view does not explain NTFS denials: [nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)

## Predictions (unverified)

- `sec=ntlmssp` is expected to let Linux connect over SMB; if not, switch to `sec=krb5` (connection
  with `sec=ntlmssp` was confirmed at stage 0).
- A missing name mapping is expected to surface as a denial because no default user is set (held for
  NFS access in the measurement above; does not hold in the opposite direction, where the default
  `pcuser` exists).

## Untested hypotheses

- That `appreader` can read under `seed/` over SMB without a traverse right but cannot enter it over
  NFS is presumed to be because SMB access benefits from the user right that skips traverse checks
  (Bypass traverse checking), while NFS evaluates `execute` on each directory. Untested.
- The delay of about 8 s in the pairs where Windows reads is presumed to come from the Windows SMB
  client caching directory contents or a "file not found" result for some time; the reader had been
  asking for the file since before it was written. Neither the client settings nor the
  repeatability of the delay were checked.
