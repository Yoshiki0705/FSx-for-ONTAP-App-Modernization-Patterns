# AGENTS.md

<!-- audit-file-allow: naming,neutrality,pii -->
<!-- This file defines the naming, neutrality, and public-output rules, so it quotes the
     patterns it forbids. Do not copy this declaration into content files. -->

Project instructions for AI coding agents working in this repository. This file is committed;
`.kiro/` and `.private/` are gitignored, so anything an agent must know belongs here.

## Project overview

Patterns for modernizing an application whose data stays on Amazon FSx for NetApp ONTAP:
.NET Framework on Windows EC2 over SMB (stage 0), multiprotocol SMB + NFS (stage 1), modern .NET
on Linux EC2 over NFS via AI Modernization Flow (stage 2), and partial serverless through
FSx for ONTAP S3 Access Points (stage 3). Containerization is a branch handed to the
Container-Datastore Spoke. The stage table lives in [README.md](README.md).

This repository is a Spoke of
[FSx-for-ONTAP-Adoption-Playbook](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook).
Cross-references follow one rule: **the Hub journey map alone holds the diagram of how the
repositories relate.** This repository links only to that map and to specific pages of adjacent
Spokes. Do not copy general ONTAP knowledge from the Hub; link to it.

Bilingual: Japanese is primary (`README.md`, `docs/ja/`), English mirrors it (`README.en.md`,
`docs/en/`) with the same section structure. Code, identifiers, and commit messages are English.

## Core commands

```bash
make install   # .venv with the pins in requirements-dev.txt (ruff, cfn-lint)
make all       # lint + audit + links + test: the commit gate
```

Never read a gate's result off a pipe. `make all | tail` returns tail's status. Use
`make all > /tmp/all.log 2>&1; echo $?`. Every gate fails when its tool is missing. Gate
inventory: [docs/agent/quality-gates.md](docs/agent/quality-gates.md).

## Naming (applies to every file, diagram, comment, and commit)

- First mention: **Amazon FSx for NetApp ONTAP**. Thereafter: **FSx for ONTAP**. No other form.
- Forbidden, always corrected to "FSx for ONTAP": `FSxN`, bare `FSx`, `FSx ONTAP`, `FSx NetApp`.
- Write **S3 Access Points** in full, or "FSx for ONTAP S3 Access Points" where it could be read as
  an Amazon S3 access point. Never `S3 AP`.
- **Never propose** NetApp Workload Factory, NetApp Console, or BlueXP. Use the native equivalent:
  Amazon CloudWatch, the ONTAP REST API, FabricPool, AWS DataSync, Snapshot / FlexClone / SnapMirror.
- A verbatim external title containing a forbidden form carries `<!-- allow:naming -->` on its line.

## Vendor neutrality

No superiority or vendor-versus framing (`best`, `beats`, `inferior`, `competing tools`,
`競合ツール`, `より優れている`, `優位性`). State trade-offs symmetrically, including for the
recommended option, and give every comparison a "how to choose" section.

## Evidence tiers

| Tier | Meaning | Requirement |
|---|---|---|
| `verified` | Reproduced by the author in a named environment | Date, region, ONTAP version, and configuration stated inline |
| `documented` | Stated in AWS, NetApp, or AIMF documentation | Source URL; quote at most 30 consecutive words, paraphrase preferred |
| `field-observation` | Observed once, not reproduced | Say so in the body; do not generalize |
| `hypothesis` | Reasoned expectation, untested | Label as untested in the body |

Never promote a tier without adding its evidence; downgrading is always allowed. Keep these
distinct: sample run vs production estimate, this test environment vs a general service limit,
design consideration vs legal or compliance judgment, AI assistive signal vs final decision.

## Public-output safety

This repository will be public; git history is permanent. Never commit personal names, e-mail
addresses, AWS account IDs (use `123456789012`), internal IPs or hostnames (use `10.0.x.x` or
`<management-ip>`), file system IDs other than `fs-0123456789abcdef0`, support case numbers,
vendor-internal ticket IDs, customer names, personal paths, or unmasked screenshots. This covers
branch names, commit messages, and PR text too.

Do not label notes with job titles or persona names (`> **AppSec lens**:`, `> **… の視点**:`).
Use topic labels (`> **Security note**:`, `> **セキュリティに関する補足**:`). Do not add review
rounds, review dates, or persona review summaries to published files.

AIMF records: `install.sh` creates a working-record repository (`<name>-migration-<date>/`). Keep
it under `.private/` or another gitignored location (`.gitignore` also ignores `*-migration-*/`).
Publish only excerpts of results, after the audit passes.

## Japanese headings

Japanese section headings (`##` and deeper) are noun phrases: `自環境での確認手順`, not
`自分の環境で確かめる`; `この区分が必要な理由`, not `なぜこの区分が必要か`. Keep the assertion when
nominalizing (`片方の穴の存在`, not `片方の穴`). The H1 is exempt. `make headings` enforces this.

## Immutability and irreversible operations

**Never enable a feature whose purpose is to remove the ability to delete data on your own
judgement.** This repository does not use SnapLock or snapshot locking (Tamperproof Snapshot) at
all. Operations that require an explicit human instruction naming the retention value include:
FSx for ONTAP `SnaplockConfiguration` / `SnaplockType` / `AuditLogVolume` / `PrivilegedDelete` /
`RetentionPeriod`, ONTAP `-snapshot-locking-enabled` / `-snaplock-expiry-time` / snapshot or
SnapMirror policy `-retention-period`, S3 Object Lock, S3 Glacier Vault Lock, AWS Backup Vault
Lock, EBS `lock-snapshot`, and any value named `PERMANENTLY_DISABLED` or `COMPLIANCE`.

`scripts/guard_irreversible_ops.py` enforces this mechanically. It is a byte-identical copy of the
Hub's guard; update it by copying that file again, never by editing it here. Wire it to a
`PreToolUse` hook that runs the **tracked path**, not a copy under `.kiro/` or `$HOME`:

```bash
python3 scripts/guard_irreversible_ops.py   # hook command; reads the tool call on stdin
python3 scripts/guard_irreversible_ops.py --selftest   # run by make test
```

If the guard blocks a command, do not look for a call that evades it. Stop and ask.

Every AWS deployment needs explicit approval each time, after presenting the configuration,
duration, and cost estimate. Verification runs only on a dedicated file system created for it.

## Commits and pull requests

- Branch `<type>/<what>`, kebab-case, at most 40 characters, naming what the branch adds.
- Commit subject `<type>(<scope>): <what changed>`, at most 72 characters, imperative.
- PR title `<type>: <description>`, under 70 characters; enforced by
  `.github/workflows/pr-title-check.yml`. Squash merge is the default.
- Run `make all` after the final edit and before every commit.
