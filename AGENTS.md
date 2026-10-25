# AGENTS.md

<!-- audit-file-allow: naming,neutrality,pii -->
<!-- This file defines the naming, neutrality, and public-output rules, so it quotes the
     patterns it forbids. Do not copy this declaration into content files. -->

Project instructions for AI coding agents working in this repository. This file is committed;
`.kiro/` and `.private/` are gitignored, so anything an agent must know belongs here.

## Project overview

This repository covers patterns for modernizing legacy applications and systems while their data remains on Amazon FSx for NetApp ONTAP. The current verified slice is one `DocIntake` console application with the recorded paths from .NET Framework 4.8 / Windows / SMB to .NET 10 / Linux / NFS, and to .NET 10 managed-runtime Lambda through FSx for ONTAP S3 Access Points. Stage 2 compares the old Windows build with the modernized Linux build; it is not Windows .NET 10 validation. This slice is evidence for the named application and recorded environment, not a compatibility statement for the broader scope. Containerization is a branch handed to the Container-Datastore Spoke. The stage table lives in [README.md](README.md).

This repository is a Spoke of
[FSx-for-ONTAP-Adoption-Playbook](https://github.com/Yoshiki0705/FSx-for-ONTAP-Adoption-Playbook).
Cross-references follow one rule: **the Hub journey map alone holds the diagram of how the
repositories relate.** This repository links only to that map and to specific pages of adjacent
Spokes. Do not copy general ONTAP knowledge from the Hub; link to it.

Other repositories pin sentences in our documents, and their gates break when one is reworded.
`docs/agent/inbound-probe-contract.txt` lists them, and `make inbound-probes` fails when a pinned
string is gone, quoted a second time, or left only in a heading. To change such a sentence, keep
the quoted substring or tell the repository named in that file first. Describe a pinned string in
other prose instead of quoting it.

Bilingual: Japanese is primary (`README.md`, `docs/ja/`), English mirrors it (`README.en.md`,
`docs/en/`) with the same section structure. Code, identifiers, and commit messages are English.

## Core commands

```bash
make install   # .venv with the pins in requirements-dev.txt (ruff, cfn-lint)
make all       # lint + audit + links + drift + app-modern + test: the commit gate
make app-modern  # build and test app/modern (needs the .NET SDK 10; fails without it)
```

Never read a gate's result off a pipe. `make all | tail` returns tail's status. Use
`make all > /tmp/all.log 2>&1; echo $?`. Every gate fails when its tool is missing. Gate
inventory: [docs/agent/quality-gates.md](docs/agent/quality-gates.md). `make drift` fails when this
file or `CONTRIBUTING.md` names a `make` target that the Makefile does not define.

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
| `verified` | A reproduced execution for the named slice and recorded environment | State the date, region, ONTAP version, and configuration |
| `field-observation` | A one-off observation without confirmed reproduction | Say so in the body and do not generalize |
| `documented` | A declaration in a repository artifact or cited documentation, not runtime execution | Identify the artifact or source and paraphrase cited material |
| `open/not measured` | Unexecuted, unsupported by the current record, or outside the verified slice | Keep it separate from every evidence label |
| `hypothesis` | An explicitly untested expectation | It never upgrades `open/not measured`, `documented`, or `verified` |

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
- Before merging, run `make pr-verify PR=<number>`. It keys every lookup on the head SHA, because
  `gh pr checks` shows the latest results and can return the previous commit's verdict after a
  push. It exits 0 only when every pull_request workflow completed with success for that SHA,
  1 on a failure or a workflow with no run yet, and 2 while runs are still going
  (`WAIT=<seconds>` polls). It needs `gh` and the network, so it is not part of `make all`.
