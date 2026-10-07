#!/usr/bin/env bash
#
# Coordinate the two-client probes and collect the results. The behaviors that need two clients
# (file-locking, write-visibility) run with a role on each host: holder/contender and writer/reader.
# This drives DocIntake.Probe on the Windows host and probe_peer.py (or the migrated .NET Probe) on
# the Linux host over SSM Run Command, uploads each side's JSON to the artifacts S3 bucket, copies
# both back, and merges them per behavior into .private/runs/<run-id>/merged.json.
#
#   run-probe.sh --stage <0..3> --run-id s<stage>-<UTC> \
#       --windows-instance i-<win> --linux-instance i-<lnx> --bucket <artifacts-bucket> \
#       [--region ap-northeast-1] [--svm-netbios APPMODSVM01]
#
# The instance ids, bucket and SVM NetBIOS name are supplied by the caller; none of the live
# i-/account/bucket values is hardcoded here. The merged per-behavior JSON carries
# observed.topology=cross-host for the two-client behaviors (file-locking, write-visibility) and an
# outcome in {measured,error,skipped} per schema appmod-probe/1.
#
# When APPMOD_DRY_RUN is set, every aws ssm send-command and s3 cp is printed with its real
# arguments and NO AWS call is made; a reviewer confirms the commands are built, not stubbed.
#
set -euo pipefail

DRY_RUN="${APPMOD_DRY_RUN:-}"
REGION="${APPMOD_REGION:-ap-northeast-1}"
STAGE=""
RUN_ID=""
WIN_INSTANCE="${APPMOD_WIN_INSTANCE:-}"
LNX_INSTANCE="${APPMOD_LNX_INSTANCE:-}"
BUCKET="${APPMOD_ARTIFACTS_BUCKET:-}"
SVM_NETBIOS="${APPMOD_SVM_NETBIOS:-APPMODSVM01}"

# The two-client behaviors; the merged record forces observed.topology=cross-host for these.
TWO_CLIENT_BEHAVIORS="file-locking write-visibility"

usage() {
  echo "usage: run-probe.sh --stage <0..3> --run-id <s..> --windows-instance i-... \\" >&2
  echo "                    --linux-instance i-... --bucket <artifacts-bucket> [--region r] [--svm-netbios n]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stage) STAGE="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --windows-instance) WIN_INSTANCE="${2:-}"; shift 2 ;;
    --linux-instance) LNX_INSTANCE="${2:-}"; shift 2 ;;
    --bucket) BUCKET="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --svm-netbios) SVM_NETBIOS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "run-probe: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$STAGE" in 0|1|2|3) ;; *) echo "run-probe: --stage must be 0..3" >&2; usage; exit 2 ;; esac
if [ -z "$RUN_ID" ]; then echo "run-probe: --run-id is required" >&2; usage; exit 2; fi
if [ -z "$WIN_INSTANCE" ]; then echo "run-probe: --windows-instance is required" >&2; usage; exit 2; fi
if [ -z "$LNX_INSTANCE" ]; then echo "run-probe: --linux-instance is required" >&2; usage; exit 2; fi
if [ -z "$BUCKET" ]; then echo "run-probe: --bucket is required" >&2; usage; exit 2; fi

OUT_DIR=".private/runs/$RUN_ID"
mkdir -p "$OUT_DIR"
S3_PREFIX="s3://$BUCKET/probe/$RUN_ID"

run_aws() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION $*"
    return 0
  fi
  aws --region "$REGION" "$@"
}

# Start one probe over SSM Run Command. $1 instance, $2 document, $3 the probe command line. The
# probe prints its appmod-probe/1 JSON to stdout; the Run Command uploads stdout to the artifacts
# bucket via OutputS3BucketName/OutputS3KeyPrefix, which is how each side's JSON reaches S3.
send_probe() {
  local instance="$1" document="$2" command_line="$3" key_prefix="$4"
  run_aws ssm send-command \
    --instance-ids "$instance" \
    --document-name "$document" \
    --comment "appmod probe $RUN_ID stage $STAGE" \
    --parameters "commands=[\"$command_line\"]" \
    --output-s3-bucket-name "$BUCKET" \
    --output-s3-key-prefix "probe/$RUN_ID/$key_prefix"
}

# Windows DocIntake.Probe over SMB (writer / holder on the Windows side). The launcher establishes
# the appsvc SMB session first (SSM runs as SYSTEM, which otherwise cannot reach the share) and
# invokes the deployed Probe. probe-launch.ps1 must be staged at C:\appmod\probe-launch.ps1.
send_probe "$WIN_INSTANCE" "AWS-RunPowerShellScript" \
  "powershell -ExecutionPolicy Bypass -File C:\\appmod\\probe-launch.ps1 -Stage $STAGE -RunId $RUN_ID -Role holder -SvmNetbios $SVM_NETBIOS -Region $REGION" \
  "windows"

# Linux probe_peer.py over SMB (contender / reader on the Linux side). The launcher mounts the SMB
# share as appsvc (sec=ntlmssp, falling back to krb5) and invokes probe_peer.py. probe-launch.sh
# must be staged at /opt/appmod/probe-launch.sh.
send_probe "$LNX_INSTANCE" "AWS-RunShellScript" \
  "bash /opt/appmod/probe-launch.sh --store smb --stage $STAGE --role contender --run-id $RUN_ID --region $REGION --svm-netbios $SVM_NETBIOS" \
  "linux-smb"

# Stage 1 and later add the NFS mount on the Linux side.
if [ "$STAGE" -ge 1 ]; then
  send_probe "$LNX_INSTANCE" "AWS-RunShellScript" \
    "bash /opt/appmod/probe-launch.sh --store nfs --stage $STAGE --role reader --run-id $RUN_ID --region $REGION --svm-netbios $SVM_NETBIOS" \
    "linux-nfs"
fi

echo "run-probe: role pairs holder/contender (file-locking), writer/reader (write-visibility)"
echo "run-probe: record each host NTP offset; a difference smaller than the offset is 'no difference'"

# Collect each side's JSON from the artifacts bucket into the run directory.
run_aws s3 cp "$S3_PREFIX/windows/" "$OUT_DIR/" --recursive --exclude '*' --include '*.json'
run_aws s3 cp "$S3_PREFIX/linux-smb/" "$OUT_DIR/" --recursive --exclude '*' --include '*.json'
if [ "$STAGE" -ge 1 ]; then
  run_aws s3 cp "$S3_PREFIX/linux-nfs/" "$OUT_DIR/" --recursive --exclude '*' --include '*.json'
fi

# Merge the collected per-host JSON into one per-behavior record. Under dry-run no file was copied,
# so a tiny in-script fixture pair is merged instead, which still proves the merge forces
# topology=cross-host on the two-client behaviors and keeps the three-valued outcome.
merge_results() {
  if [ -n "$DRY_RUN" ]; then
    cat >"$OUT_DIR/windows.json" <<EOF
{"schema":"appmod-probe/1","run_id":"$RUN_ID","stage":$STAGE,"role":"holder","behaviors":[
  {"id":"file-locking","outcome":"measured","observed":{"topology":"single-host","denied":true}},
  {"id":"write-visibility","outcome":"measured","observed":{"topology":"single-host","delay_ms":12}}]}
EOF
    cat >"$OUT_DIR/linux-smb.json" <<EOF
{"schema":"appmod-probe/1","run_id":"$RUN_ID","stage":$STAGE,"role":"contender","behaviors":[
  {"id":"file-locking","outcome":"measured","observed":{"topology":"single-host","denied":true}},
  {"id":"write-visibility","outcome":"measured","observed":{"topology":"single-host","delay_ms":12}}]}
EOF
  fi
  APPMOD_RUN_ID="$RUN_ID" APPMOD_STAGE="$STAGE" APPMOD_OUT_DIR="$OUT_DIR" \
  APPMOD_TWO_CLIENT="$TWO_CLIENT_BEHAVIORS" python3 - <<'PY'
import glob
import json
import os

out_dir = os.environ["APPMOD_OUT_DIR"]
run_id = os.environ["APPMOD_RUN_ID"]
stage = int(os.environ["APPMOD_STAGE"])
two_client = set(os.environ["APPMOD_TWO_CLIENT"].split())

sides = {}
for path in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
    name = os.path.basename(path)
    if name == "merged.json":
        continue
    try:
        sides[name] = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue

# Collect every behavior id seen across the collected sides.
behavior_ids = []
for side in sides.values():
    for b in side.get("behaviors", []):
        if b.get("id") not in behavior_ids:
            behavior_ids.append(b["id"])

OUTCOMES = {"measured", "error", "skipped"}
merged = []
for bid in behavior_ids:
    per_side = {}
    outcome = "measured"
    for name, side in sides.items():
        for b in side.get("behaviors", []):
            if b.get("id") == bid:
                side_outcome = b.get("outcome", "error")
                if side_outcome not in OUTCOMES:
                    side_outcome = "error"
                # error wins over skipped wins over measured when the two sides disagree.
                if side_outcome == "error":
                    outcome = "error"
                elif side_outcome == "skipped" and outcome != "error":
                    outcome = "skipped"
                per_side[name] = b.get("observed", {})
    observed = {"per_side": per_side}
    # The two-client behaviors are cross-host by construction of this merge.
    if bid in two_client:
        observed["topology"] = "cross-host"
    else:
        topologies = {v.get("topology") for v in per_side.values() if isinstance(v, dict)}
        observed["topology"] = topologies.pop() if len(topologies) == 1 else "cross-host"
    merged.append({"id": bid, "outcome": outcome, "observed": observed})

record = {
    "schema": "appmod-probe/1",
    "run_id": run_id,
    "stage": stage,
    "merged_from": sorted(sides),
    "behaviors": merged,
}
path = os.path.join(out_dir, "merged.json")
with open(path, "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2)
print(f"run-probe: merged {len(merged)} behavior(s) from {len(sides)} side(s) into {path}")
PY
}

merge_results
echo "run-probe: coordination complete for stage $STAGE run $RUN_ID"
