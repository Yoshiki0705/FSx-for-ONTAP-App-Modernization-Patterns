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
# i-/account/bucket values is hardcoded here. The two-client behaviors (file-locking,
# write-visibility) run as coordinated pairs with a barrier on the artifacts bucket, and
# probe_merge.py records observed.topology=cross-host only for a pair whose timelines prove it; each
# behavior carries an
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

run_aws() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION $*"
    return 0
  fi
  aws --region "$REGION" "$@"
}

# Start one probe over SSM Run Command. $1 instance, $2 document, $3 the probe command line. The
# probe prints its appmod-probe/1 JSON to stdout; the Run Command also uploads stdout to the
# artifacts bucket (OutputS3BucketName/OutputS3KeyPrefix) for the record. Prints the command id.
send_probe() {
  local instance="$1" document="$2" command_line="$3" key_prefix="$4"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION ssm send-command --instance-ids $instance" \
      "--document-name $document --parameters commands=[\"$command_line\"]" \
      "--output-s3-bucket-name $BUCKET --output-s3-key-prefix probe/$RUN_ID/$key_prefix" >&2
    echo "dry-run-command-id"
    return 0
  fi
  aws --region "$REGION" ssm send-command \
    --instance-ids "$instance" \
    --document-name "$document" \
    --comment "appmod probe $RUN_ID stage $STAGE" \
    --parameters "commands=[\"$command_line\"]" \
    --output-s3-bucket-name "$BUCKET" \
    --output-s3-key-prefix "probe/$RUN_ID/$key_prefix" \
    --query 'Command.CommandId' --output text
}

# Wait for an SSM command to finish on an instance and write its stdout (the probe's appmod-probe/1
# JSON) to $OUT_DIR/<side>.json. The SSM output object in S3 is literally named "stdout" under a
# nested key, so rather than glob for *.json this reads StandardOutputContent after the command
# reaches a terminal state. A nonzero ResponseCode or a non-Success status is surfaced.
# collect_probe <command-id> <instance> <file-stem>: wait for the command and write its stdout (the
# probe's appmod-probe/1 JSON) to $OUT_DIR/<file-stem>.json. The SSM output object in S3 is
# literally named "stdout" under a nested key, so rather than glob for *.json this reads
# StandardOutputContent after the command reaches a terminal state. A non-Success status is
# recorded in FAILED_SIDES and checked before the merge, after every side has been collected.
collect_probe() {
  local command_id="$1" instance="$2" stem="$3"
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION ssm wait command-executed --command-id $command_id --instance-id $instance" >&2
    return 0
  fi
  # ssm wait gives up after 100 polls x 5 s and returns nonzero on a failed command; poll on until
  # the invocation is terminal, then read the status either way.
  aws --region "$REGION" ssm wait command-executed \
    --command-id "$command_id" --instance-id "$instance" 2>/dev/null || true
  local status rc
  while :; do
    status="$(aws --region "$REGION" ssm get-command-invocation \
      --command-id "$command_id" --instance-id "$instance" --query 'Status' --output text)"
    case "$status" in Pending|InProgress|Delayed) sleep 5 ;; *) break ;; esac
  done
  rc="$(aws --region "$REGION" ssm get-command-invocation \
    --command-id "$command_id" --instance-id "$instance" --query 'ResponseCode' --output text)"
  aws --region "$REGION" ssm get-command-invocation \
    --command-id "$command_id" --instance-id "$instance" \
    --query 'StandardOutputContent' --output text | sed 's/\r$//' >"$OUT_DIR/$stem.json"
  echo "run-probe: $stem status=$status rc=$rc -> $OUT_DIR/$stem.json"
  if [ "$status" != "Success" ]; then FAILED_SIDES="$FAILED_SIDES $stem"; fi
}
FAILED_SIDES=""

# side_command <side> <role> [pair-args]: the launcher command line for one side.
#   windows    DocIntake.Probe over SMB (the launcher opens the appsvc SMB session first, because
#              SSM runs as SYSTEM, which otherwise cannot reach the share)
#   linux-smb  probe_peer.py over the SMB mount (sec=ntlmssp, falling back to krb5)
#   linux-nfs  probe_peer.py over the NFS mount, as the UNIX user appsvc (stage 1 and later)
side_command() {
  local side="$1" role="$2" behavior="${3:-}" sync_id="${4:-}"
  case "$side" in
    windows)
      local extra=""
      if [ -n "$behavior" ]; then extra=" -PairBehavior $behavior -SyncId $sync_id -Bucket $BUCKET"; fi
      printf '%s\n' "powershell -ExecutionPolicy Bypass -File C:\\appmod\\probe-launch.ps1 -Stage $STAGE -RunId $RUN_ID -Role $role -SvmNetbios $SVM_NETBIOS -Region $REGION$extra" ;;
    linux-smb|linux-nfs)
      local extra=""
      if [ -n "$behavior" ]; then extra=" --pair-behavior $behavior --sync-id $sync_id --bucket $BUCKET"; fi
      echo "bash /opt/appmod/probe-launch.sh --store ${side#linux-} --stage $STAGE --role $role --run-id $RUN_ID --region $REGION --svm-netbios $SVM_NETBIOS$extra" ;;
  esac
}
side_instance() { if [ "$1" = "windows" ]; then echo "$WIN_INSTANCE"; else echo "$LNX_INSTANCE"; fi; }
side_document() { if [ "$1" = "windows" ]; then echo "AWS-RunPowerShellScript"; else echo "AWS-RunShellScript"; fi; }

# 1. Standalone: every side measures all five behaviors on its own. The three single-client
#    behaviors are compared from these records. The two-client behaviors recorded here are a
#    single host's view and are never labeled cross-host by the merge.
SIDES="windows linux-smb"
if [ "$STAGE" -ge 1 ]; then SIDES="$SIDES linux-nfs"; fi
# Indexed arrays only: this runs on the operator's machine, where /bin/bash may be 3.2.
SIDE_CMDS=()
for side in $SIDES; do
  SIDE_CMDS+=("$(send_probe "$(side_instance "$side")" "$(side_document "$side")" \
    "$(side_command "$side" standalone)" "$side")")
done
i=0
for side in $SIDES; do
  collect_probe "${SIDE_CMDS[$i]}" "$(side_instance "$side")" "$side"
  i=$((i + 1))
done

# 2. Coordinated pairs for the two-client behaviors (design: the stage-0 pairs Windows(SMB)/
#    Linux(SMB) both ways, plus from stage 1 Windows(SMB)/Linux(NFS) and Linux(NFS)/Windows(SMB)).
#    Each pair shares a sync id and a barrier on the artifacts bucket; both sides run concurrently.
#    The manifest records which file carries which side and role, for the merge to verify.
PAIRS=(
  "file-locking windows holder linux-smb contender"
  "file-locking linux-smb holder windows contender"
  "write-visibility windows writer linux-smb reader"
  "write-visibility linux-smb writer windows reader"
)
if [ "$STAGE" -ge 1 ]; then
  PAIRS+=(
    "file-locking windows holder linux-nfs contender"
    "file-locking linux-nfs holder windows contender"
    "write-visibility windows writer linux-nfs reader"
    "write-visibility linux-nfs writer windows reader"
  )
fi
MANIFEST="$OUT_DIR/pairs-manifest.tsv"
: >"$MANIFEST"
echo "run-probe: role pairs holder/contender (file-locking), writer/reader (write-visibility)"
n=0
for spec in "${PAIRS[@]}"; do
  n=$((n + 1))
  read -r behavior s1 r1 s2 r2 <<<"$spec"
  sync_id="$RUN_ID-p$n"
  f1="pair-$n-$s1-$r1"
  f2="pair-$n-$s2-$r2"
  c1="$(send_probe "$(side_instance "$s1")" "$(side_document "$s1")" \
    "$(side_command "$s1" "$r1" "$behavior" "$sync_id")" "$f1")"
  c2="$(send_probe "$(side_instance "$s2")" "$(side_document "$s2")" \
    "$(side_command "$s2" "$r2" "$behavior" "$sync_id")" "$f2")"
  collect_probe "$c1" "$(side_instance "$s1")" "$f1"
  collect_probe "$c2" "$(side_instance "$s2")" "$f2"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$n" "$behavior" "$sync_id" \
    "$s1" "$r1" "$f1.json" "$s2" "$r2" "$f2.json" >>"$MANIFEST"
done

if [ -n "$FAILED_SIDES" ]; then
  echo "run-probe: the probe command did not succeed on:$FAILED_SIDES; not merging" >&2
  echo "run-probe: each side's output is kept under $OUT_DIR/ for diagnosis" >&2
  exit 1
fi

# 3. Merge. Under dry-run no command ran, so a fixture with standalone sides only is merged, which
#    proves the merge does NOT label the two-client behaviors cross-host without a proven pair.
if [ -n "$DRY_RUN" ]; then
  for side in $SIDES; do
    cat >"$OUT_DIR/$side.json" <<EOF
{"schema":"appmod-probe/1","run_id":"$RUN_ID","stage":$STAGE,"role":"standalone","behaviors":[
  {"id":"file-locking","outcome":"measured","observed":{"topology":"cross-host","exclusive_open":true}},
  {"id":"write-visibility","outcome":"measured","observed":{"topology":"cross-host","marker":"m"}}]}
EOF
  done
  : >"$MANIFEST"
fi
python3 "$(dirname "${BASH_SOURCE[0]}")/probe_merge.py" --out-dir "$OUT_DIR" --run-id "$RUN_ID" \
  --stage "$STAGE" --sides "$SIDES" --pairs "$MANIFEST"
echo "run-probe: coordination complete for stage $STAGE run $RUN_ID"
