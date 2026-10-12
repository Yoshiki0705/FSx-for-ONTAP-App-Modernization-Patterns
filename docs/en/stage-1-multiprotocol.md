# Stage 1: adding NFS to the same volume
>
> Evidence tier: the sections "Measured results (verified)", "Reproduction (verified)" and "Findings
> from the measurement" are `verified`. Each carries a run id and a date, and one-off results say
> "single observation". "Steps" and "What to confirm at the boundary" describe the plan, and
> "Untested hypotheses" is `hypothesis`.
Add NFS access to the stage-0 target volume. The volume is not cloned, rebuilt or moved. The
security style stays NTFS.

## Steps

Carried out in two builds, 2026-10-07 (ONTAP 9.19.1P2) and 2026-10-11 (9.19.1P3), in this order.

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
> Measured on 2026-10-07 (run id `s1-20261007T150116Z`; the two-client pairs are
> `s1-20261007T164155Z`), region ap-northeast-1, ONTAP 9.19.1P2. Amazon FSx for NetApp ONTAP
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

### Single-client behaviors (path-separator and acl-evaluation)

| Behavior | Windows (SMB) | Linux (SMB) | Linux (NFS) |
|---|---|---|---|
| `path-separator` (creating a name containing `\`) | treated as a separator | rejected (`errno=22`) | created; `\` is part of the name |
| `acl-evaluation` | the pre-check reports writable | no pre-check available | no pre-check available |

- `sep\check.txt` created over NFS appears on Windows under the 8.3 short name `SEPCHE~1.TXT` and can
  be read. The name the Linux SMB client rejected at stage 0 can be created over NFS on the same
  volume.
- `path-separator` and `acl-evaluation` on Windows and Linux (SMB) were the same observations as at
  stage 0 (`compare-probe.py` verdict `ok`). `case-sensitivity` is excluded from this comparison: both
  compared records have no positive control, and agreement between invalid records is not evidence
  even when the verdict is `ok`. Linux (NFS) has no stage-0 counterpart to compare with.
- The handling of `\` differs by protocol. In an NFS file name and in an S3 key `\` is an ordinary
  character, and a Linux cifs mount rejects it with `errno=22`. The trap where an S3 key containing `\`
  written at the volume root shows up on the SMB side as an 8.3 short name is described in
  [stage 3](stage-3-s3-access-points.md). What to do: when testing creation of names containing `\`,
  create them only under a dedicated directory such as `probe/`.

### Case sensitivity (per access path, with a positive control)

> Run id `s4c-20261012T034403Z`, 2026-10-12, ONTAP 9.19.1P3, Single-AZ, one volume (security style
> `ntfs`). One observation per path. Only records in which the positive control (the real
> `seed/docs/index.json`, requested by the same route) resolved are treated as results.

| Path | Reference (the changed spelling `seed/Docs/Index.json`) | Positive control (`seed/docs/index.json`) |
|---|---|---|
| Windows (SMB) | resolved (`resolved=true`) | resolved (`real_resolved=true`) |
| Linux (cifs, `vers=3.0`, `sec=ntlmssp`) | resolved (true) | resolved (true) |
| Linux (NFS 4.1, `sec=sys`, uid 10001) | did not resolve (`resolved=false`; not a denial by error) | resolved (true) |

- On the same ntfs volume, SMB (Windows and cifs) does not distinguish case and NFS does.
- The cifs mount options carried no case setting such as `nocase`, and other options were not tried.
  Which option decided the cifs result cannot be said.
- What to do: write the application so it does not depend on the case of file names, or design for the
  difference per path. Code that worked over SMB can get "file not found" over NFS.
- The 2026-10-07 run (`s1-20261007T150116Z`, ONTAP 9.19.1P2) has an observation from a different
  design. `casecheck.txt` and `CaseCheck.txt` created over NFS appeared on Windows as two entries,
  `casecheck.txt` and `CASECH~2.TXT`, and the contents read back differed (`l` and `u`). It shows that
  NFS can create two files whose names differ only in case, and it is a different design of
  measurement from the values in the table above. Single observation.
- The `case-sensitivity` records of the earlier Probe (the `s0` and `s1` series, stages 0 to 2) have
  no positive control and are not used as conclusions. The result through S3 Access Points is in
  [stage 3](stage-3-s3-access-points.md).

### Two-client behaviors (2026-10-07)

Run id `s1-20261007T164155Z`, ONTAP 9.19.1P2. The two hosts acted on the same file at the same time and
synchronized through the artifacts S3 bucket, not through the volume under test. A pair was labeled
`cross-host` only when both sides carried the same sync id and both records showed the timelines
overlapping (the contender's attempt during the holder's lock, the reader's observation after the
writer's save). All 8 pairs met this. The SMB-to-SMB pairs are values under the stage-1 configuration
(with the NFS export present), not a stage-0 baseline.

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

- The reader repeated a listing and a read every 100 ms, so the resolution is about 100 ms. This run
  measured each pair once; the reproduction is in the next section. These are not performance claims.
- The time until Windows could read over SMB a file written over NFS (8,085 ms) was close to the time
  for a file written over SMB (7,849 ms). In this configuration the delay of about 8 s came with
  Windows being the reader, not with the writer's protocol. The reverse direction (Windows writes,
  Linux reads) was within about 100 ms (one polling interval) over both SMB and NFS.
- Windows `FileShare.None` also refused the NFS opens. The Linux `fcntl` lock stopped the Windows read
  as a lock violation. Exclusion held across protocols in both directions.

### Reproduction (2026-10-11 and 2026-10-12, ONTAP 9.19.1P3)

> The environment is ap-northeast-1, Single-AZ, one volume `appdata` (security style `ntfs`). Every
> value is a single observation per pair. Two-client coordination counts only the pairs that
> `probe_merge.py` judged `cross-host` (`reasons` empty) after confirming a matching sync id and
> overlapping time windows.

| Run id | Content | file-locking | write-visibility (Windows writes → Linux reads / Linux writes → Windows reads) |
|---|---|---|---|
| `s1-20261011T075058Z` (2026-10-11) | earlier Probe (Python on the Linux side), 4 SMB pairs and 4 NFS pairs | in all 4 pairs the contender was refused and succeeded after release | SMB: 38 ms / 8,546 ms. NFS: 18 ms / 8,168 ms |
| `s2m-20261012T090934Z` (2026-10-12) | modernized Probe (net10.0 on the Linux side), earlier build on the Windows side, 8 pairs | in all 4 pairs the contender was refused and succeeded after release. The Linux holder used `FileShare.None` | SMB: 66 ms / 7,968 ms. NFS: 35 ms / 8,342 ms |

- For file-locking, a Linux contender against a Windows holder was refused with `errno=16` (cifs) or
  `errno=13` (NFS). Against a Linux holder, the Windows contender's open succeeded, the read was a lock
  violation (`0x80070021`) and the exclusive open a sharing violation (`0x80070020`). The Linux
  holder's mechanism differed (`fcntl.lockf` in the earlier Probe, `FileShare.None` in the modernized
  one), and the result for the Windows contender was the same.
- The shape "about 8 s when Linux writes and Windows reads, under 100 ms in the other direction"
  appeared in four runs (`s1-20261007T164155Z`, `s0-20261011T065443Z` (SMB only),
  `s1-20261011T075058Z`, `s2m-20261012T090934Z`). It had the same shape whether the Linux writer was
  Python (with `os.fsync`) or the modernized Probe (`File.WriteAllBytes`, no fsync). The cause was not
  investigated.
- What to do: where a Windows client reads files written from Linux (SMB or NFS), put a wait or retry
  that allows for the delay before the file becomes readable (about 8 s in this configuration). Do not
  design without a delay on the grounds that the reverse direction is fast. The seconds measured here
  are values for one configuration and do not show the delay in other configurations.
- Only the case where one side takes `FileShare.None` or an exclusive lock and the other tries to open
  in the meantime was measured. Whether a writer that takes no lock can be stopped was not measured.

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
  In `s2m-20261012T090934Z` on 2026-10-12, `cd /mnt/appdata` as root returned `Permission denied`, and
  the modernized Probe and Worker were run after switching to uid 10001 with
  `setpriv --reuid=10001 --regid=10001 --clear-groups` (ONTAP 9.19.1P3, `sec=sys`, single
  observation). On an ntfs volume permissions are evaluated by the ACL, and an NFS principal is
  evaluated only when name mapping maps it to a Windows name.
  What to do: run NFS clients as a uid that name mapping resolves. Run as root, the client is denied
  from the top level of the volume.
- The mode shown by NFS `ls -l` does not indicate whether access is allowed. In the 2026-10-12 NFS-side
  listing too, the top-level `out`, `probe` and `seed` showed as `d---------` (owned by root) while
  uid 10001 could read and write (a single listing). What to do: do not take the `ls` mode as evidence
  of permission; check with ONTAP effective-permissions and the name-mapping result.
- The 2026-10-07 stage-0 record had not measured the two-host interaction for `file-locking` and
  `write-visibility`. The corresponding claim in the stage-0 document is withdrawn
  ([stage 0](stage-0-smb-ntfs.md)).

## Sources for the claims

General multiprotocol findings are cited per claim from Hub notes (contents are not copied).

- Adding NFS needs no clone: [adding-a-protocol-does-not-need-a-clone](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/adding-a-protocol-does-not-need-a-clone.md)
- Security style and permission evaluation: [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)
- The NFS-side view does not explain NTFS denials: [nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook/blob/main/docs/en/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)

## Untested hypotheses

- That `appreader` can read under `seed/` over SMB without a traverse right but cannot enter it over
  NFS is presumed to be because SMB access benefits from the user right that skips traverse checks
  (Bypass traverse checking), while NFS evaluates `execute` on each directory. `hypothesis`. Untested.
- The delay of about 8 s in the pairs where Windows reads is presumed to come from the Windows SMB
  client caching directory contents or a "file not found" result for some time; the reader had been
  asking for the file since before it was written. `hypothesis`. The client settings were not checked.
  That the delay appears at all was confirmed in the four runs under "Reproduction" above; its cause
  was not investigated.
- The NFS name containing `\` (`exists_literal`) quotes an observation of the 2026-10-07 Python
  Probe. The modernized Probe writes no name containing `\`, so it was not re-observed.

## Not measured

The following were not measured in this stage. Do not use them as conclusions.

- Behavior with security style `unix` and `mixed`
- ONTAP versions other than 9.19.1P2 and 9.19.1P3
- Multi-AZ, multiple SVMs, or layouts beyond one volume
- Other cifs mount options (including `nocase`), Kerberos, and NFS versions other than 4.1
- Invoiced amounts
