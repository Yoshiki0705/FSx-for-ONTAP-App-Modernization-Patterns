# De-stub of the three orchestration scripts — implementation note

This records what was run so a reviewer need not re-run the suites. Scope: replace the three STUB
orchestration scripts with real implementations, OFFLINE (no AWS calls, no deploy, no push).

## What changed

| File | Before | After |
|---|---|---|
| `scripts/ontap/stage0-smb.sh` | echoed `note` lines only | real `curl` calls to the ONTAP REST API: create SMB share `appdata`, set the share ACL, set NTFS ACLs (appsvc read/write; appreader read-only with an explicit `access_deny` write ACE), create `seed/`/`probe/`/`out/`, create the `appmod_itclone` and `appmod_readonly` REST roles and the `appmod-itclone` user, set `volume_delete_retention_hours=0`. Idempotent (GET before POST). Asserts DC discovery via `GET /api/protocols/cifs/domains/{svm-uuid}?fields=discovered_servers` (exit 4 when no `ms_dc` is in state `ok`). |
| `scripts/run-probe.sh` | echoed `say` lines only | real `aws ssm send-command` for the Windows `DocIntake.Probe` and the Linux `probe_peer.py` (holder/contender, writer/reader), real `aws s3 cp` to collect each side's JSON from the artifacts bucket, then a per-behavior merge into `.private/runs/<run-id>/merged.json` that forces `observed.topology=cross-host` on the two-client behaviors and keeps `outcome` in `{measured,error,skipped}` under schema `appmod-probe/1`. |
| `scripts/ontap/record-boundary.sh` | wrote a JSON skeleton with literal `<...>` placeholders | real ONTAP REST GETs (`/api/cluster?fields=version`, the volume list with `nas.security_style`/`snapshot_locking_enabled`/`snaplock.type`, the target volume UUID, export policies, name mappings, snapshots) and the inventory path+SHA-256; writes captured values with no `<...>`; a real run exits 1 if a required ONTAP field is missing. Output path `.private/runs/<run-id>/<boundary>.json`. |

All three never echo the `fsxadmin` secret: it is read from Secrets Manager via the instance role
inside the script and redacted (`fsxadmin:<redacted>`) under `APPMOD_DRY_RUN`. None of them enables
SnapLock or snapshot locking; they only READ `snapshot_locking_enabled`/`snaplock.type`, which the
irreversibility guard permits. Live `fs-`/`i-`/account/bucket values are passed as arguments or env,
never hardcoded.

## Arguments / env contract (new)

- `stage0-smb.sh`: `--file-system-id` `--svm` `--volume` `--mgmt-ip` `--region` (or `APPMOD_FS_ID`,
  `APPMOD_SVM`, `APPMOD_VOLUME`, `APPMOD_ONTAP_MGMT_IP`, `APPMOD_REGION`).
- `run-probe.sh`: `--stage` `--run-id` `--windows-instance` `--linux-instance` `--bucket`
  `--region` `--svm-netbios`.
- `record-boundary.sh`: `--boundary` `--run-id` `--file-system-id` `--mgmt-ip` `--svm` `--volume`
  `--windows-inventory` `--linux-inventory` `--region`.

Management IP is resolved at runtime from `aws fsx describe-file-systems ... Endpoints.Management`
when `--mgmt-ip` is not supplied.

## Verified facts baked in (not re-derived)

- ONTAP version 9.19.1P2; non-SnapLock `snaplock.type` is `non_snaplock` (U25 resolved; verified
  live 2026-10-07). The staged path overall remains `hypothesis`.
- DC discovery via `cifs/domains` `discovered_servers` (the `active-directory` collection alone
  returns 0 records and is not sufficient).

## What was run (OFFLINE)

- `make all > /tmp/fam-impl3.log 2>&1; echo $?` → **0** (never piped).
- `shellcheck scripts/ontap/stage0-smb.sh scripts/run-probe.sh scripts/ontap/record-boundary.sh` →
  clean.
- `python3 tools/check_heading_style.py .kiro/specs/app-modernization-stages/design.md` → all
  Japanese section headings are noun phrases.
- `scripts/tests/dryrun_shell_tests.sh` (run by `make test`) → all cases passed, including the new
  assertions that each dry-run BUILDS the real command (ONTAP REST path, `aws ssm send-command`,
  `aws s3 cp`), that the credential is redacted, and that no lock endpoint is touched.
- `python3 scripts/guard_irreversible_ops.py --selftest` → 33/33 cases passed; the share-create
  `curl` is allowed (exit 0, silent).

No AWS/ONTAP/network call was made: every exercise used `APPMOD_DRY_RUN=1` and local fixtures.
`design.md` is under `.kiro/` (gitignored) and is not part of `make all`'s tracked-file scan; it was
checked with `check_heading_style.py` directly.
