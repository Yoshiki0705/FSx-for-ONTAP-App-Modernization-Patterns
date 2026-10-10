# Stage 2: migrating to modern .NET on Linux

> Evidence tier: `hypothesis` (not measured). The steps are a plan and the predictions are
> pre-measurement hypotheses, replaced by records.

Using the AI Modernization Flow (hereafter "AIMF", v0.11.0) `dotnetfw-to-modern-dotnet` playbook,
migrate the source app to modern .NET and run it on the Linux EC2 over NFS. The Windows client
stays through stage 3, so the security style is kept NTFS.

## Planned steps

1. Re-check the hook wiring and make `fsxadmin` unreadable from the Linux instance role for stage 2
2. Run AWS Transform custom only through the single entry (`run-atx.sh`), after approval
3. Work through AIMF Phase 0a to 4 and integrate the confluence points
4. Deploy the migrated code and run the Probe
5. Prepare the security-style ADR and wait for a human decision (no switch in this verification)

## Engine for the AIMF sessions

The AIMF sessions use the default engine of kiro-cli 2.28.0 (V2) in interactive mode. On
2026-10-10, a hook-firing canary on macOS with kiro-cli 2.28.0 showed the following.

- V2 reads only the hooks written in the agent config. A matcher fires on an exact tool name and
  never fired on the regular expression `^(execute_bash|shell|use_aws|aws)$` or on
  `execute_bash|use_aws`. With a JSON list as the matcher, the agent itself failed to load.
  `setup-workspace.sh` wires matchers that name each tool individually
- A hook returning exit 2 stopped the tool call on both V2 and V3
- V2 in non-interactive mode refused to run shell even with `--trust-tools=execute_bash,use_aws`.
  The cause is not confirmed, so non-interactive mode is not used
- V3 (`--v3`) reads hooks from three places (agent config, workspace, global) and also fired on the
  regular-expression matcher, but has no `use_aws` tool. The hook that stops AWS calls would then target a different tool than on V2, so V3 is not
  used in this verification

## What to confirm at the boundary

- The target volume UUID and the `seed/` inventory are identical to stage 0
- The security style is still `ntfs`
- The recovery queue, clone relationships and `it_*` snapshots are empty
- The Probe differences are compared against stages 0 and 1 and recorded

## The record and publication boundary

The whole AIMF record repository stays in the unpublished work area. Only audited excerpts (tables
and summaries) are published, with file-system IDs, account IDs and IPs replaced and `make audit`
run over them.

## Predictions (unverified)

- AIMF is expected to stop for a human decision rather than autonomously change the security style
  or the path format.
- On migrated Linux the `GetAccessControl` family is unavailable, so the ACL pre-check and the NFS
  result are expected to disagree.
