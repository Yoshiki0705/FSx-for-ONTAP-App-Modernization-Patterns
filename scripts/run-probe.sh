#!/usr/bin/env bash
#
# Coordinate the two-client probes and collect the results. The behaviors that need two clients
# (file-locking, write-visibility) run with a role on each host: holder/contender and writer/reader.
# This drives DocIntake.Probe on the Windows host and probe_peer.py (or the migrated .NET Probe) on
# the Linux host over SSM Run Command, then collects both JSON outputs via the artifacts bucket into
# .private/runs/<run-id>/.
#
#   run-probe.sh --stage <0..3> --run-id s<stage>-<UTC>
#
# This is an in-environment orchestration script (needs SSM and running EC2); it is not part of make
# test. When APPMOD_DRY_RUN is set, the Run Command and S3 calls are printed instead of run.
#
set -euo pipefail

DRY_RUN="${APPMOD_DRY_RUN:-}"
STAGE=""
RUN_ID=""

usage() { echo "usage: run-probe.sh --stage <0..3> --run-id <s..>" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --stage) STAGE="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "run-probe: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$STAGE" in 0|1|2|3) ;; *) echo "run-probe: --stage must be 0..3" >&2; usage; exit 2 ;; esac
if [ -z "$RUN_ID" ]; then echo "run-probe: --run-id is required" >&2; usage; exit 2; fi

say() {
  if [ -n "$DRY_RUN" ]; then echo "DRY-RUN: $*"; else echo "$*"; fi
}

OUT_DIR=".private/runs/$RUN_ID"
say "collect probe results into $OUT_DIR"

say "ssm send-command Windows: DocIntake.Probe --store smb --root \\\\APPMODSVM01\\appdata --stage $STAGE --role writer --run-id $RUN_ID"
say "ssm send-command Linux: probe_peer.py --store smb --root /mnt/appdata-smb --stage $STAGE --role reader --run-id $RUN_ID"
if [ "$STAGE" -ge 1 ]; then
  say "ssm send-command Linux (NFS): probe_peer.py --store nfs --root /mnt/appdata --stage $STAGE --role reader --run-id $RUN_ID"
fi
say "role pairs: holder/contender for file-locking, writer/reader for write-visibility"
say "record each host NTP offset; differences smaller than the offset are reported as 'no difference'"
say "copy both JSON outputs from the artifacts bucket to $OUT_DIR"
echo "run-probe: coordination plan printed for stage $STAGE run $RUN_ID"
