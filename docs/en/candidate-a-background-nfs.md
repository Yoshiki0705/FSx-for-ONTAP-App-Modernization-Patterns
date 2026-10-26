# Candidate A: running the .NET 10 BackgroundService on Linux/NFS

> Evidence tier: the operational steps (systemd unit, environment variables, promotion-gate input)
> are `verified` from the Candidate A real-environment run (ap-northeast-1, ONTAP 9.18.1, NFS v4.1 on
> a disposable SVM/volume, Amazon Linux 2023 arm64, .NET 10). Environment-specific identifiers
> (account, IPs, file system / SVM / volume / instance IDs) are in the private ledger and are not
> reproduced here. A one-off observation says so in the body.

Candidate A is a separate .NET 10 `BackgroundService` implementation of the DocIntake sample app's
background processing, distinct from the `DocIntake.Worker` console. It mounts an
Amazon FSx for NetApp ONTAP volume over NFS on a Linux EC2 instance and runs as a systemd service.
This guide covers its deployment and the pitfalls found during the real-environment run.

The assets live in `app/modern/deploy/`.

| Path | Contents |
|---|---|
| `app/modern/deploy/docintake-bgworker.service` | Sample systemd unit (`Type=simple`, `RequiresMountsFor`) |
| `app/modern/deploy/docintake-bgworker.env.example` | Environment-variable template (copy to `/etc/docintake-bgworker.env` and edit) |

## Prerequisites

- A Linux host with the .NET 10 runtime installed (the run used Amazon Linux 2023, arm64). You may
  build elsewhere and deploy the published output (`DocIntake.BackgroundWorker.dll` and its deps).
- An FSx for ONTAP volume mounted over NFS v4.1. The run used `unix` security style and AUTH_SYS
  (`sec=sys`).
- An unprivileged service account (e.g. `docintake`) that can read and write the NFS mount.

## Environment variables

Every worker setting is a `DOCINTAKE_*` environment variable; the worker exits at startup if any is
missing or malformed. The meanings are in `docintake-bgworker.env.example`. Two matter most.

- `DOCINTAKE_ROOT` is the NFS mount root. `seed/inbox`, the `out/<stage>/reports/` output, and the
  `.claims` directory are all created under it.
- `DOCINTAKE_CONTROL_PLANE_PATH` is where control-plane records go; keep it OUTSIDE `DOCINTAKE_ROOT`
  (local disk) so postconditions survive an NFS-side cleanup. The worker rejects a control-plane path
  inside the appdata root.

## The systemd unit

Place `docintake-bgworker.service` in `/etc/systemd/system/`, copy the env file, and start it.

```bash
sudo cp app/modern/deploy/docintake-bgworker.service /etc/systemd/system/
sudo cp app/modern/deploy/docintake-bgworker.env.example /etc/docintake-bgworker.env
sudo "${EDITOR:-vi}" /etc/docintake-bgworker.env
sudo systemctl daemon-reload
sudo systemctl enable --now docintake-bgworker.service
systemctl status docintake-bgworker.service
```

### Why `Type=simple`

This worker is a plain .NET generic host (`Program.Main` -> `Host...RunAsync`). It does not reference
`Microsoft.Extensions.Hosting.Systemd` and does not call `UseSystemd()`, so it never sends the systemd
readiness notification (sd_notify `READY=1`). Under `Type=notify`, `systemctl start` waits for a
readiness that never arrives and blocks until the unit start-timeout. The run confirmed this (start
blocked under `Type=notify`; started normally under `Type=simple`). If you want `notify`, add the
systemd hosting integration to the worker. Without it, `Type=simple` is correct.

### Mount dependency

`RequiresMountsFor=/mnt/appdata` makes systemd order this unit after the NFS mount and stop the unit
if the mount goes away, so the worker never runs against an absent root.

### Stop behavior

`systemctl stop` sends SIGTERM. The generic host cancels the loop, records a normal stop to the
control plane, finishes the in-flight item, and releases its claim before exiting. `TimeoutStopSec`
is the grace window; the run stopped gracefully even with 30 seconds, below the default.

## Promotion-gate input (`source_revision`)

`NfsPromotionGate` takes the run evidence and decides promotion. `source_revision` is a **64-hex
SHA-256 content digest of the candidate source tree**, NOT a 40-hex Git commit SHA-1. A commit id
names a commit; it does not capture an uncommitted working-tree edit, so it cannot attest to the
exact bytes that ran. Derive the digest from the per-file SHA-256 manifest of the source.

```bash
# Hash each file in a stable order, then hash the whole listing again to get one 64-hex digest.
find app/modern/src -type f \( -name '*.cs' -o -name '*.csproj' \) | sort \
  | xargs shasum -a 256 \
  | shasum -a 256 | cut -d' ' -f1
```

Pass that 64-hex value as `source_revision`. The raw-artifact hash validation shares the same 64-hex
SHA-256 contract.

> Note: the local promotion gate always keeps `approved_runtime_provenance` missing and never
> self-promotes to `verified`. The runtime provenance comes from an external ledger that records the
> candidate-specific real-environment run. The gate assembles validation inputs; it is not the final
> decision.

## What the run verified

The Candidate A real-environment run (ONTAP 9.18.1, NFS v4.1 on a disposable SVM/volume, Amazon Linux
2023 arm64, .NET 10) marked the following `verified`, limited to this candidate and this recorded
environment.

| Aspect | Result |
|---|---|
| NFS processing | list -> claim -> read -> write report -> release holds; postconditions recorded outside NFS |
| systemd lifecycle | starts, processes, and stops gracefully under `Type=simple`; mount dependency enforced |
| Claim contention | an item held by another owner's live claim is declined; no double-processing |
| Timeout boundary | an unconfirmed-completion timeout is recorded outside NFS and the late completion reconciled |
| Case sensitivity | the NFS mount is case-sensitive (as expected for Linux/NFS, unlike SMB/Windows) |

Environment-specific identifiers are in the private ledger.
