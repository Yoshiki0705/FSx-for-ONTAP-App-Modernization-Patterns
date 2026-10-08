# Stub detector for the orchestration scripts — implementation note

This records what was run so a reviewer need not re-run it. Scope: make a stub orchestration script
fail `make test`, OFFLINE (no AWS calls, no deploy, no push).

## What changed

| File | Role |
|---|---|
| `scripts/tests/check_no_stub_scripts.py` | The checker. Walks every `*.sh` under `scripts/` (`scripts/tests/` excluded), parses it with a small shell lexer, and fails when an `external` script's real-mode path makes no real `aws` / `curl` / `gitleaks` / `atx` / `git clone` / `install.sh` / `pwsh` call, when a `fail-closed` script has no real-only `exit <non-zero>`, and, per call, when a real-mode runner call (`run`, `step`, any function that runs `"$@"`) is handed `echo`, `printf` or an echo-only function such as `note`. Wrappers and runners (`aws_r`, `ontap_ok`, `run`, `step`, ...) are derived from the function bodies, not listed. The file header states the parser's limits. |
| `scripts/tests/stub_check_classification.json` | One entry per script: class and a one-line reason. A new script fails the gate until it is classified. |
| `scripts/tests/test_no_stub_scripts.py` | Unit tests: the committed stub / control fixture pair, every dry-run branch form, and every non-invocation (`echo`, `printf`, `note`, `run echo`, comments, here-documents, quoted remote strings, `command -v`, array literals, case patterns). |
| `scripts/tests/fixtures/stub-check/` | `stub/` only echoes `aws ssm send-command ...` in its real branch (rejected); `control/` runs it (accepted). |
| `scripts/tests/check_test_coverage.py` | Also requires every `scripts/tests/check_*.py` to be invoked by `make test`. |
| `Makefile` | `make test` runs the checker and the new unittest module. |

## Evidence that it catches the historical stubs

The pre-destub trees were extracted with `git show <ref>^:<path>` into `/tmp/stubhist2/<ref>-parent/`
(never `git checkout <ref> -- <path>`), then checked against the current classification:

```bash
python3 scripts/tests/check_no_stub_scripts.py --root /tmp/stubhist2/31d9ba5-parent
python3 scripts/tests/check_no_stub_scripts.py --root /tmp/stubhist2/8c716bc-parent
```

Both exit 1. The scripts reported, by ref:

| Ref | Stub (external, no real invocation) | Stub step (runner handed `echo`) | Fail-closed without a real-only exit |
|---|---|---|---|
| `31d9ba5^` | `stage0-smb.sh`, `run-probe.sh`, `record-boundary.sh`, `stage1-nfs.sh`, `integration-clone.sh` | `teardown.sh` (5 calls), `integration-clone.sh` (5 calls) | `run-atx.sh` |
| `8c716bc^` | `stage1-nfs.sh`, `integration-clone.sh` | `teardown.sh` (5 calls), `integration-clone.sh` (5 calls) | `run-atx.sh` |

The checker output, abridged (the stub message ends "... (echo/printf/note arguments and dry-run
branches do not count): a stub" and the stub-step message ends "... executes a command that only
prints: a stub step, whatever other steps invoke"; both are cut to `...` here). The tree for each
ref was rebuilt in `/tmp/stubhist2/` with `git show <ref>^:<path>` for every tracked `*.sh` outside
`scripts/tests/`.

```text
$ python3 scripts/tests/check_no_stub_scripts.py --root /tmp/stubhist2/31d9ba5-parent   # rc=1
scripts/ontap/lib-ontap-rest.sh: classified in stub_check_classification.json but not on disk
scripts/probe-launch.sh: classified in stub_check_classification.json but not on disk
scripts/aimf/run-atx.sh: classified fail-closed, but no literal `exit <non-zero>` sits in a branch that runs only when APPMOD_DRY_RUN is unset
scripts/ontap/integration-clone.sh: classified external, but its real-mode path invokes none of ...: a stub
scripts/ontap/integration-clone.sh: line 94: the real-mode runner call `run echo ...` executes ...
  (same for lines 95, 103, 104, 105)
scripts/ontap/record-boundary.sh: classified external, but its real-mode path invokes none of ...: a stub
scripts/ontap/stage0-smb.sh: classified external, but its real-mode path invokes none of ...: a stub
scripts/ontap/stage1-nfs.sh: classified external, but its real-mode path invokes none of ...: a stub
scripts/run-probe.sh: classified external, but its real-mode path invokes none of ...: a stub
scripts/teardown.sh: line 78: the real-mode runner call `step echo ...` executes ...
  (same for lines 91, 93, 98, 101)

$ python3 scripts/tests/check_no_stub_scripts.py --root /tmp/stubhist2/8c716bc-parent   # rc=1
scripts/ontap/lib-ontap-rest.sh: classified in stub_check_classification.json but not on disk
scripts/aimf/run-atx.sh: classified fail-closed, but no literal `exit <non-zero>` sits in a branch that runs only when APPMOD_DRY_RUN is unset
scripts/ontap/integration-clone.sh: classified external, but its real-mode path invokes none of ...: a stub
scripts/ontap/integration-clone.sh: line 94: the real-mode runner call `run echo ...` executes ...
  (same for lines 95, 103, 104, 105)
scripts/ontap/stage1-nfs.sh: classified external, but its real-mode path invokes none of ...: a stub
scripts/teardown.sh: line 105: the real-mode runner call `step echo ...` executes ...
  (same for lines 118, 120, 125, 130)
```

The "not on disk" lines are entries for scripts that did not exist yet at that ref, as intended.
Every other script at those refs passes.

`teardown.sh` at both refs is a partial stub: `step` really runs `"$@"`, most steps hand it
`aws_r ...`, and steps 9' (after-failed-create path), 4, 5, 7 and 9 handed it
`echo "<intended command>"`, e.g. `step "7 empty the artifacts bucket" echo "aws s3 rm ..."`. The
first version of this gate counted per script only, so the real `aws_r` steps let it pass. The
per-call runner rule now rejects each of those five calls on its own; the current tree has none.
The historical `integration-clone.sh` is the same `run echo ...` shape and is reported both ways.

`run-atx.sh` is caught through its classification, not by reading the `atx` syntax. The old script
ran `atx run --playbook ...` for real, and the checker cannot tell a wrong `atx` invocation from a
right one. What it enforces is the policy that replaced it: while the invocation is unverified, the
real path must refuse.

This check is not part of `make test`. Those two commits do not survive the planned squash merge,
and CI checks out a shallow clone, so a test keyed on them would break. The committed fixtures and
the unit tests carry the same shapes.

## What was run (OFFLINE)

- `python3 -m unittest scripts.tests.test_no_stub_scripts`: 34 tests pass.
- The same 34 tests against the checker with one function replaced in memory (a throwaway driver
  that monkeypatches the loaded module, runs the suite, and restores it; not committed):

  | Mutation (function replaced in `check_no_stub_scripts`) | Failing tests |
  |---|---|
  | `is_real_mode` returns `True` for every command, so dry-run branches count as real | 4 |
  | `invocation` also returns a hit when any word's first token is a tool name, so `echo "aws ..."` arguments count | 9 |
  | `classify_condition` returns `(None, None)`, so no DRY_RUN branch is recognised | 5 |
  | `echo_handoff` returns `None`, so the per-call runner rule is off | 4 |

- `python3 scripts/tests/check_no_stub_scripts.py --explain` on the current tree: 17 scripts
  classified, 14 external, 2 pure, 1 fail-closed, no runner handed `echo`, exit 0.
- `make all > /tmp/fam-stubgate.log 2>&1; echo $?`: 0.

## Known gaps

- Outside runner calls the verdict is per script. `setup-workspace.sh` passes on its real
  `git clone` and `install.sh` calls while `step4_wire`, called directly rather than through a
  runner, still only prints the hook wiring it intends to write.
- A real invocation is not a correct one. The other live findings from stages 0-1 (a name passed
  where a UUID was required, a missing `curl -k`, a missing `apply_to`, IAM permissions) are outside
  what this gate can see.
