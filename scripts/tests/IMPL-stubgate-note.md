# Stub detector for the orchestration scripts — implementation note

This records what was run so a reviewer need not re-run it. Scope: make a stub orchestration script
fail `make test`, OFFLINE (no AWS calls, no deploy, no push).

## What changed

| File | Role |
|---|---|
| `scripts/tests/check_no_stub_scripts.py` | The checker. Walks every `*.sh` under `scripts/` (`scripts/tests/` excluded), parses it with a small shell lexer, and fails when an `external` script's real-mode path makes no real `aws` / `curl` / `gitleaks` / `atx` / `git clone` / `install.sh` / `pwsh` call, or a `fail-closed` script has no real-only `exit <non-zero>`. Wrappers and runners (`aws_r`, `ontap_ok`, `run`, `step`, ...) are derived from the function bodies, not listed. The file header states the parser's limits. |
| `scripts/tests/stub_check_classification.json` | One entry per script: class and a one-line reason. A new script fails the gate until it is classified. |
| `scripts/tests/test_no_stub_scripts.py` | Unit tests: the committed stub / control fixture pair, every dry-run branch form, and every non-invocation (`echo`, `printf`, `note`, `run echo`, comments, here-documents, quoted remote strings, `command -v`, array literals, case patterns). |
| `scripts/tests/fixtures/stub-check/` | `stub/` only echoes `aws ssm send-command ...` in its real branch (rejected); `control/` runs it (accepted). |
| `scripts/tests/check_test_coverage.py` | Also requires every `scripts/tests/check_*.py` to be invoked by `make test`. |
| `Makefile` | `make test` runs the checker and the new unittest module. |

## Evidence that it catches the historical stubs

The pre-destub trees were extracted with `git show <ref>:<path>` into `/tmp/stubhist/<ref>-parent/`
(never `git checkout <ref> -- <path>`), then checked against the current classification:

```bash
python3 scripts/tests/check_no_stub_scripts.py --root /tmp/stubhist/31d9ba5-parent --explain
python3 scripts/tests/check_no_stub_scripts.py --root /tmp/stubhist/8c716bc-parent --explain
```

Both exit 1. The scripts reported, by ref:

| Ref | Reported as a stub (external, no real invocation) | Reported as fail-closed without a real-only exit |
|---|---|---|
| `31d9ba5^` | `stage0-smb.sh`, `run-probe.sh`, `record-boundary.sh`, `stage1-nfs.sh`, `integration-clone.sh` | `run-atx.sh` |
| `8c716bc^` | `stage1-nfs.sh`, `integration-clone.sh` | `run-atx.sh` |

The two refs also report entries that did not exist yet (`lib-ontap-rest.sh` at both, `probe-launch.sh`
at `31d9ba5^`) as classified but not on disk, as intended. Every other script at those refs passes.
The historical `integration-clone.sh` is the `run echo ...` shape: its `run` runner really executes
`"$@"`, and the checker still rejects it because the command handed to `run` is `echo`.

`run-atx.sh` is caught through its classification, not by reading the `atx` syntax. The old script
ran `atx run --playbook ...` for real, and the checker cannot tell a wrong `atx` invocation from a
right one. What it enforces is the policy that replaced it: while the invocation is unverified, the
real path must refuse.

This check is not part of `make test`. Those two commits do not survive the planned squash merge,
and CI checks out a shallow clone, so a test keyed on them would break. The committed fixtures and
the unit tests carry the same shapes.

## What was run (OFFLINE)

- `python3 -m unittest scripts.tests.test_no_stub_scripts`: 30 tests pass. With the checker
  weakened in memory, they fail: dry-run branches counted as real (3 failures), `echo` arguments
  counted as calls (8), dry-run branch recognition removed (4).
- `python3 scripts/tests/check_no_stub_scripts.py --explain` on the current tree: 17 scripts
  classified, 14 external, 2 pure, 1 fail-closed, exit 0.
- `make all > /tmp/fam-stubgate.log 2>&1; echo $?`: 0.

## Known gaps

- The verdict is per script. `setup-workspace.sh` passes on its real `git clone` and `install.sh`
  calls while its step 4 still only prints the hook wiring it intends to write.
- A real invocation is not a correct one. The other live findings from stages 0-1 (a name passed
  where a UUID was required, a missing `curl -k`, a missing `apply_to`, IAM permissions) are outside
  what this gate can see.
