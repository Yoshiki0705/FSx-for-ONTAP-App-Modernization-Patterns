# Stage 1: adding NFS to the same volume

> Evidence tier: `hypothesis` (not measured). The steps are a plan and the predictions are
> pre-measurement hypotheses, replaced by records.

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

## Sources for the claims

General multiprotocol findings are cited per claim from Hub notes (contents are not copied).

- Security style and permission evaluation: [security-style-and-permission-evaluation](https://github.com/Yoshiki0705/fsxn-adoption-playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/security-style-and-permission-evaluation.md)
- The NFS-side view does not explain NTFS denials: [nfs-side-view-does-not-explain-ntfs-denials](https://github.com/Yoshiki0705/fsxn-adoption-playbook/blob/main/docs/ja/domains/multiprotocol-identity/notes/nfs-side-view-does-not-explain-ntfs-denials.md)

## Predictions (unverified)

- `sec=ntlmssp` is expected to let Linux connect over SMB; if not, switch to `sec=krb5` (unconfirmed).
- A missing name mapping is expected to surface as a denial because no default user is set.
