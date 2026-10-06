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
