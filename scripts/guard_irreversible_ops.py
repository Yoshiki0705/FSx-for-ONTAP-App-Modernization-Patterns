#!/usr/bin/env python3
"""Block an agent from unilaterally enabling immutability (WORM) features.

WHY THIS EXISTS
---------------
On 2026-08-06 an AI agent working in this repository created an Amazon FSx for NetApp
ONTAP SnapLock audit log volume in order to verify a documented claim. It did not ask
which retention period to use, and it read the relevant warning only *after* the
operation failed to reverse.

One 128 MiB volume made the volume, its storage virtual machine, and **the entire file
system** undeletable for six months. The agent had also already set privileged delete to
`PERMANENTLY_DISABLED`, which closed the only remaining escape route. The verification it
was performing produced no usable finding.

The general lesson is not about SnapLock. It is this:

    A feature whose stated purpose is to remove your ability to delete data must never
    be enabled by an agent acting on its own judgement.

Such a feature working correctly is indistinguishable from an outage you caused. There is
no rollback, no support escalation that reliably helps, and the blast radius is routinely
wider than the resource you named in the call.

WHAT THIS DOES
--------------
Reads a Kiro PreToolUse hook payload on stdin, extracts the command, and exits 2 (block)
when the command both (a) touches an immutability or terminal-state feature and (b) is
mutating. Read-only inspection is always allowed, because refusing to let an agent *look*
would just push it toward guessing.

Exit codes: 0 allow, 2 block (stderr is shown to the agent).

SELF-TEST
---------
Run `python3 guard_irreversible_ops.py --selftest`. It covers all three verdicts — block
(exit 2), ask (exit 0 plus a permissionDecision payload on stdout), and allow (exit 0,
silent) — because a guard that only proves it blocks has not shown that it lets ordinary
work through, and over-blocking is what gets guards switched off.

The cases live in this file rather than
in a separate harness for a concrete reason: once the guard is wired to a hook, **passing a
sample locking command on the command line gets the test run itself blocked.** That happened
during development. Keeping the cases inside the module means verifying the guard never
requires a matching string to cross the shell — and the alternative, rewriting the sample to
slip past the pattern, is exactly the bypass this guard exists to prevent.

REUSE
-----
Stdlib only, no repository-specific assumptions. Copy it into any project and wire it to
a PreToolUse hook. Extend IMMUTABILITY_PATTERNS as you meet new features; the categories
matter more than the exact list.
"""

from __future__ import annotations

import json
import re
import sys

# Features that deliberately remove the ability to delete, or that latch into a state with
# no documented return path. Grouped by service so the block message can name the risk.
IMMUTABILITY_PATTERNS: dict[str, list[str]] = {
    "FSx for ONTAP SnapLock": [
        r"snaplock",
        r"AuditLogVolume",
        r"PrivilegedDelete",
        r"SnaplockType",
        r"VolumeAppendModeEnabled",
        r"audit-logs",
    ],
    # Snapshot locking (Tamperproof Snapshot) is the same lock-in class as SnapLock and is
    # easy to miss, because it applies to volumes that are *not* SnapLock volumes and has no
    # AWS API parameter — so no IAM condition key or console warning can gate it. Enabling it
    # cannot be undone until every locked snapshot expires, and the volume cannot be deleted
    # until then.
    "Snapshot locking / Tamperproof Snapshot": [
        r"snapshot[-_]locking[-_]enabled",
        r"snaplock[-_]expiry[-_]time",
        r"modify-snaplock-expiry-time",
        r"snapshot\s+policy\s+create",
        r"retention[-_]period\d*",
        r"snapmirror\s+policy\s+add-rule",
    ],
    "S3 Object Lock": [
        r"object-lock",
        r"ObjectLock(?:Configuration|Retention|LegalHold)?",
        r"put-object-retention",
        r"put-object-legal-hold",
    ],
    "S3 Glacier Vault Lock": [
        r"initiate-vault-lock",
        r"complete-vault-lock",
    ],
    "AWS Backup Vault Lock": [
        r"backup-vault-lock",
        r"put-backup-vault-lock-configuration",
        r"ChangeableForDays",
    ],
    "EBS snapshot lock": [
        r"lock-snapshot",
        r"CoolOffPeriod",
    ],
    "Terminal / permanently-disabled states": [
        r"PERMANENTLY_DISABLED",
        r"permanently[-_]disabled",
    ],
}

# Verbs that change state. An immutability pattern alone is not enough to block, or the
# guard would stop an agent from reading the state it needs in order to ask a good question.
#
# The CLI verb is anchored to its command position on purpose. An unanchored `lock-` also
# matches inside `get-object-lock-configuration`, which turned a read into a block during
# testing — exactly the over-blocking that trains people to disable a guard.
MUTATING = re.compile(
    r"""(?xi)
    \baws\s+[\w-]+\s+
      (?:create|update|put|modify|delete|initiate|complete|lock|enable|associate|tag)-
  | -X\s*(?:POST|PATCH|PUT|DELETE)
  | --method\s*(?:POST|PATCH|PUT|DELETE)
  # ONTAP CLI. The verb must sit within two words of the object and be followed by a flag.
  # Real invocations carry flags - `volume delete` alone is not a complete ONTAP command, it
  # needs -vserver and -volume - and prose does not.
  #
  # Two words rather than one, because `volume snapshot policy create` exists. The `[\w-]*`
  # after the verb is for `modify-snaplock-expiry-time`, where the hyphenated tail follows the
  # verb with no space. Both were caught by the case table when a tighter version silently
  # stopped blocking them: **loosening a guard has to be measured in the direction that still
  # has to fail**, not only in the direction being relaxed.
  #
  # The looser form was `[\w\s-]*?` between object and verb, which let arbitrary words
  # intervene, so **prose describing the feature matched as if it were a command.** Writing a
  # pull request body about an audit log volume that cannot be deleted was blocked by this
  # guard. A guard that blocks documentation about the hazard it guards gets switched off, and
  # then it guards nothing.
  | \b(?:volume|vserver|snapmirror)(?:\s+[\w-]+){0,2}?\s+
      (?:create|modify|delete|add-rule)[\w-]*\s+-\w
    """
)

BLOCK_MESSAGE = """\
BLOCKED: this command enables or alters an immutability / terminal-state feature.

  Feature area : {areas}
  Matched      : {matches}

An agent must not decide this on its own. These features work by removing the ability to
delete, so a mistake cannot be undone by you, by the account owner, or reliably by AWS
Support. The affected scope is regularly wider than the resource named in the call — an
FSx for ONTAP SnapLock audit log volume, for example, locks its SVM and the whole file
system for a minimum of six months, in Enterprise mode too.

Do this instead:

  1. Stop. Do not retry, and do not look for an equivalent call that evades this check.
  2. Tell the human, in the conversation, exactly:
       - which resource the operation targets,
       - what becomes undeletable, and for how long,
       - the widest scope affected (volume? SVM? file system? account?),
       - the cost of holding that scope for the full period,
       - whether any documented path exists to reverse it early (usually: none).
  3. Ask for the retention value explicitly. Never infer it, never accept a service
     default silently, and state the minimum the service permits.
  4. Proceed only on an instruction that names the value and the scope.

If this is verification work, ask whether a disposable, dedicated file system or account
should be used first. Verification is never a reason to skip this gate — the incident that
produced this guard was verification work, and it yielded no usable finding.
"""


def extract_command(payload: dict) -> str:
    """Pull the command text out of a hook payload without assuming one exact shape."""
    for key in ("command", "cmd", "input", "arguments", "toolInput", "tool_input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            for inner in ("command", "cmd", "text"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested.strip():
                    return nested
    # Fall back to the whole payload: a miss here should fail safe toward inspection.
    return json.dumps(payload, ensure_ascii=False)


def find_matches(command: str) -> tuple[list[str], list[str]]:
    """Return (feature areas, matched substrings) for immutability patterns."""
    areas: list[str] = []
    matches: list[str] = []
    for area, patterns in IMMUTABILITY_PATTERNS.items():
        for pattern in patterns:
            found = re.search(pattern, command, re.IGNORECASE)
            if found:
                if area not in areas:
                    areas.append(area)
                if found.group(0) not in matches:
                    matches.append(found.group(0))
    return areas, matches


# (expected_exit, description, command) — kept in this file so the cases travel with the
# guard when it is copied into another repository. A guard with no tests invites the
# over-blocking failure that gets guards switched off, so both directions are covered.
SELFTEST_CASES: list[tuple[int, str, str]] = [
    # Prose versus command, both directions. The looser ONTAP CLI pattern matched the first of
    # these, so writing about the hazard was blocked while the actual command it describes still
    # has to be. Both are needed: proving the guard stopped over-blocking says nothing about
    # whether it still blocks, and that is the direction that matters.
    (
        0,
        "prose describing an audit log volume that cannot be deleted",
        "gh pr create --title x --body 'the SnapLock audit log volume delete is refused'",
    ),
    (
        0,
        "a commit message about the same constraint",
        "git commit -m 'docs: snaplock volume delete refuses both forms'",
    ),
    (
        2,
        "the ONTAP CLI command that prose was being confused with",
        "volume delete -vserver svm1 -volume snaplock_audit_log",
    ),
    (
        2,
        "an ONTAP CLI retention rule, which carries flags too",
        "snapmirror policy add-rule -policy p1 -retention-period 6months",
    ),
    # SnapLock
    (
        2,
        "the call that caused the incident",
        (
            "aws fsx create-volume --volume-type ONTAP --ontap-configuration"
            ' \'{"SnaplockConfiguration":{"SnaplockType":"ENTERPRISE","AuditLogVolume":true}}\''
        ),
    ),
    (
        2,
        "privileged delete permanently disabled",
        (
            "aws fsx update-volume --ontap-configuration"
            " 'SnaplockConfiguration={PrivilegedDelete=PERMANENTLY_DISABLED}'"
        ),
    ),
    (
        2,
        "ONTAP REST audit-log POST",
        "curl -sk -X POST https://127.0.0.1:18443/api/storage/snaplock/audit-logs -d '{}'",
    ),
    # Snapshot locking / Tamperproof Snapshot
    (
        2,
        "enable snapshot locking on a new volume",
        "volume create -volume vol1 -aggregate aggr1 -size 100m -snapshot-locking-enabled true",
    ),
    (
        2,
        "enable snapshot locking on an existing volume",
        "volume modify -vserver vs1 -volume vol1 -snapshot-locking-enabled true",
    ),
    (
        2,
        "locking snapshot policy with a retention period",
        (
            "volume snapshot policy create -policy lock_policy -enabled true"
            " -schedule1 hourly -count1 24 -retention-period1 '1 days'"
        ),
    ),
    (
        2,
        "manual snapshot with a SnapLock expiry",
        (
            "volume snapshot create -vserver vs1 -volume vol1 -snapshot snap1"
            " -snaplock-expiry-time '11/10/2026 09:00:00'"
        ),
    ),
    (
        2,
        "set expiry on an existing snapshot",
        (
            "volume snapshot modify-snaplock-expiry-time -volume vol1 -snapshot snap2"
            " -snaplock-expiry-time '11/10/2026 09:00:00'"
        ),
    ),
    (
        2,
        "SnapMirror long-term retention rule",
        (
            "snapmirror policy add-rule -vserver vs1 -policy lockvault"
            " -snapmirror-label test1 -keep 10 -retention-period '6 months'"
        ),
    ),
    (
        2,
        "snapshot locking via ONTAP REST",
        (
            "curl -sk -X PATCH https://127.0.0.1:18443/api/storage/volumes/uuid"
            " -d '{\"snapshot_locking_enabled\":true}'"
        ),
    ),
    # Other immutability features
    (
        2,
        "S3 Object Lock",
        "aws s3api put-object-lock-configuration --bucket b --object-lock-configuration '{}'",
    ),
    (
        2,
        "Glacier vault lock completion",
        "aws glacier complete-vault-lock --vault-name v --lock-id x",
    ),
    (
        2,
        "Backup Vault Lock",
        (
            "aws backup put-backup-vault-lock-configuration --backup-vault-name v"
            " --changeable-for-days 3"
        ),
    ),
    (
        2,
        "EBS snapshot lock",
        "aws ec2 lock-snapshot --snapshot-id snap-1 --lock-mode compliance",
    ),
    (
        2,
        "S3 object retention",
        'aws s3api put-object-retention --bucket b --key k --retention \'{"Mode":"COMPLIANCE"}\'',
    ),
    # Reads and unrelated work must pass
    (
        0,
        "inspect SnapLock configuration",
        "aws fsx describe-volumes --query 'Volumes[0].OntapConfiguration.SnaplockConfiguration'",
    ),
    (
        0,
        "read audit-log configuration over REST",
        "curl -sk https://127.0.0.1:18443/api/storage/snaplock/audit-logs/?fields=**",
    ),
    (
        0,
        "read snapshot locking state",
        "curl -sk 'https://127.0.0.1:18443/api/storage/volumes?fields=snapshot_locking_enabled'",
    ),
    (0, "show a snapshot policy", "volume snapshot policy show -policy lock_policy"),
    (
        0,
        "show snapshot expiry times",
        "volume snapshot show -volume vol1 -fields snaplock-expiry-time",
    ),
    (0, "read Object Lock state", "aws s3api get-object-lock-configuration --bucket b"),
    (
        0,
        "read why a deletion failed",
        (
            "aws fsx describe-volumes --volume-ids fsvol-1"
            " --query 'Volumes[0].LifecycleTransitionReason'"
        ),
    ),
    (
        0,
        "unrelated volume creation",
        "aws fsx create-volume --name plain --ontap-configuration 'SizeInBytes=104857600'",
    ),
    # `aws fsx delete-volume` moved to ASK_CASES: it is not irreversible, but it
    # is destructive and can silently not happen behind an unexpired WORM log.
    (
        0,
        "assign a plain snapshot policy",
        "aws fsx update-volume --ontap-configuration 'SnapshotPolicy=none'",
    ),
    (0, "an unrelated command", "make all"),
]

# Tier 2 cases. Kept separate from SELFTEST_CASES because the third verdict did
# not exist when that corpus was written, and renumbering it would invalidate
# the "26 cases" figure quoted in AGENTS.md and CHANGELOG.md.
ASK_CASES: list[tuple[str, str, str]] = [
    (
        "ask",
        "file system deletion may silently not happen",
        "aws fsx delete-file-system --file-system-id fs-0123456789abcdef0",
    ),
    (
        "ask",
        "volume deletion behind an unexpired WORM log",
        "aws fsx delete-volume --volume-id fsvol-0123456789abcdef0",
    ),
    (
        "ask",
        "create-volume payload hidden in a file",
        "aws fsx create-volume --cli-input-json file://volume.json",
    ),
    (
        "allow",
        "reading a file system is not a deletion",
        "aws fsx describe-file-systems --file-system-id fs-0123456789abcdef0",
    ),
]


# --------------------------------------------------------------------------
# Tier 2: ASK. Not necessarily irreversible, but either destructive or opaque
# to this guard — a Tier 1 pattern can be hiding inside a payload the guard
# cannot read. Handing these to the human costs one prompt; guessing wrong
# costs data. Emitted as permissionDecision:ask, which is exit 0 plus stdout.
#
# Kept strictly narrower than BLOCK: an ask that fires on ordinary work trains
# people to click through, which is how a guard stops working without failing.
# --------------------------------------------------------------------------
ASK_PATTERNS: list[tuple[str, str]] = [
    (
        (
            r"\bfsx\b[^|;&]{0,60}\bdelete-(?:file-system|storage-virtual-machine|volume)\b|"
            r"\"?operation_?name\"?\W{0,4}Delete(?:FileSystem|StorageVirtualMachine|Volume)"
        ),
        (
            "FSx のファイルシステム / SVM / ボリュームの削除はデータ損失を伴います。未満了の WORM "
            "ファイルや監査ログがある場合、API は成功を返しながら無言で削除されません"
            "（数十秒後の Lifecycle と LifecycleTransitionReason で判定してください。"
            "効かないときにフラグを足して再試行しないこと）。"
        ),
    ),
    (
        r"\bcreate-volume\b[^|;&]{0,200}(?:--cli-input-json|file://)",
        (
            "create-volume の payload が外部ファイルにあり、このガードから中身が読めません。"
            "SnaplockConfiguration が含まれていないか確認してください（含まれていれば不可逆です）。"
        ),
    ),
]

ASK_MESSAGE = """\
CONFIRM REQUIRED: {why}

続行する前に、対象リソース・影響範囲・復旧可否を会話の中で提示してください。
"""


def verdict(command: str) -> str:
    """Return 'block', 'ask', or 'allow' for one command.

    Order matters: BLOCK is evaluated first so a broader ASK pattern can never
    downgrade an irreversible operation into a prompt the human might approve
    without seeing the retention consequence.
    """
    if evaluate(command) == 2:
        return "block"
    for pattern, _why in ASK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return "ask"
    return "allow"


def ask_reason(command: str) -> str:
    for pattern, why in ASK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return why
    return ""


def evaluate(command: str) -> int:
    """Return the exit code the guard would produce for one command."""
    areas, _ = find_matches(command)
    if not areas:
        return 0
    if not MUTATING.search(command):
        return 0
    return 2


def selftest() -> int:
    failures = 0
    cases = [("block" if want == 2 else "allow", d, c) for want, d, c in SELFTEST_CASES]
    cases += ASK_CASES
    for want, description, command in cases:
        got = verdict(command)
        if got == want:
            print(f"  pass ({got})  {description}")
        else:
            failures += 1
            print(f"  FAIL want={want} got={got}  {description}")
    total = len(cases)
    print(f"\n{total - failures}/{total} cases passed")
    return 1 if failures else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    # Run by hand with no piped input, json.load blocks on the terminal with no
    # indication why. Kiro always supplies stdin, so this only affects someone
    # verifying the guard manually — exactly when a silent hang is most
    # confusing. Point at the self-test instead.
    if sys.stdin.isatty():
        print(
            "This is a PreToolUse hook: it expects a JSON event on stdin.\n"
            "  To verify block / ask / allow behaviour, run:\n"
            "    python3 scripts/guard_irreversible_ops.py --selftest",
            file=sys.stderr,
        )
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"command": raw}

    command = extract_command(payload)
    areas, matches = find_matches(command)
    decision = verdict(command)

    if decision == "block":
        print(
            BLOCK_MESSAGE.format(areas=", ".join(areas), matches=", ".join(matches)),
            file=sys.stderr,
        )
        return 2

    if decision == "ask":
        # exit 0 + this payload on stdout is the only documented way to prompt a
        # human. A non-zero exit other than 2 is a warning and does not stop
        # anything, so it must never be used to mean "ask".
        json.dump(
            {
                "hookSpecificOutput": {
                    "permissionDecision": "ask",
                    "permissionDecisionReason": ASK_MESSAGE.format(
                        why=ask_reason(command)
                    ),
                }
            },
            sys.stdout,
        )
        return 0

    # Inspection and unrelated work stay allowed: an agent that cannot read the
    # current state will guess instead.
    return 0


if __name__ == "__main__":
    sys.exit(main())
