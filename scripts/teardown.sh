#!/usr/bin/env bash
#
# Tear down the environment. This is the exit from billing. Default is report-only: it prints the
# deletion order with the IDs it was given and makes NO AWS call. --apply runs the deletion in the
# design's fixed order. Run it from the operator workstation under the SSO session; the ONTAP-side
# steps run on the Linux EC2 host through SSM Run Command.
#
#   teardown.sh [--apply] --file-system-id fs-... --linux-instance i-... --bucket <artifacts-bucket>
#               --windows-role <role-name> [--volume-id fsvol-...] [--directory-id d-...]
#               [--vpc-id vpc-...] [--svm appmodsvm] [--region ap-northeast-1]
#   teardown.sh --after-failed-create [--apply] --file-system-id fs-... [--bucket b] [--windows-role r]
#
# Every live ID comes from an argument or the matching APPMOD_* environment variable; none is
# hardcoded. --volume-id is optional: appdata is resolved by enumerating describe-volumes for the
# file system, and a given --volume-id must match it.
#
# Deletion order (design "削除の順序"):
#   0  start the stopped Linux EC2 (the Run Command target) and wait until SSM reports it Online,
#      then stage the tracked files of scripts/ontap/ (git ls-files) to the artifacts bucket
#   1  check-no-locking.sh over all volumes (stop with exit 3 on any lock or a failed scan)
#   2  disable the Scheduler schedule and delete appmod-stage3 (detaches the S3 Access Point)
#   3  confirm 0 S3 Access Point attachments on appdata and on the file system
#   4  integration-clone.sh sweep: delete leftover FlexClone appdata_it_* and snapshots it_*
#   5  integration-clone.sh verify-clean: recovery queue, FlexClone list and it_* snapshots empty
#   6  delete leftover appdata_it_* FSx for ONTAP volume records, then appdata with SkipFinalBackup=true; confirm gone
#   7  empty the artifacts bucket
#   7b delete the two out-of-band Windows-role inline policies (app-users secret read and
#      artifacts-bucket access) so the base stack can delete the role
#   8  delete appmod-base and wait for DELETE_COMPLETE
#   9  delete the four secrets with --force-delete-without-recovery
#   10 confirm absence by API enumeration (not the stack resource list), 10 tries 60 s apart;
#      backups by volume id use --volume-id or the appdata id enumerated in steps 3 and 6
#   11 next day: confirm in Cost Explorer that billing stopped
#
# When APPMOD_DRY_RUN is set together with --apply, every AWS call is printed with its real
# arguments and none is made, so the order can be exercised without credentials. Read calls under
# dry-run report the object as present, so the delete calls are shown.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN="${APPMOD_DRY_RUN:-}"
REGION="${APPMOD_REGION:-ap-northeast-1}"
FS_ID="${APPMOD_FS_ID:-}"
VOLUME_ID="${APPMOD_VOLUME_ID:-}"
LNX_INSTANCE="${APPMOD_LNX_INSTANCE:-}"
BUCKET="${APPMOD_ARTIFACTS_BUCKET:-}"
WINDOWS_ROLE="${APPMOD_WINDOWS_ROLE:-}"
DIRECTORY_ID="${APPMOD_DIRECTORY_ID:-}"
VPC_ID="${APPMOD_VPC_ID:-}"
SVM="${APPMOD_SVM:-appmodsvm}"
DOMAIN_NAME="${APPMOD_DOMAIN_NAME:-appmod.example.com}"
APPLY=""
AFTER_FAILED_CREATE=""
BASE_STACK="appmod-base"
STAGE3_STACK="appmod-stage3"
SCHEDULE_NAME="$STAGE3_STACK-worker-poll"
TARGET_VOLUME="appdata"
SECRETS="${APPMOD_SECRETS:-appmod/ad-admin appmod/fsxadmin appmod/app-users appmod/ontap-itclone}"
STAGE_PREFIX="teardown/scripts/ontap"
# Polling budgets (tries x seconds). Overridable for tests; dry-run never sleeps.
SSM_ONLINE_TRIES=30; SSM_ONLINE_SLEEP=20
ABSENCE_TRIES=10; ABSENCE_SLEEP=60

# Out-of-band inline policies added to the Windows role on an environment created before
# templates/base.yaml carried the grants. Their names differ from the template's policies, so on a
# fresh environment they do not exist and step 7b deletes nothing.
OUT_OF_BAND_WINDOWS_POLICIES="appmod-read-app-users-secret appmod-artifacts-bucket-access"

usage() {
  echo "usage: teardown.sh [--apply] --file-system-id fs-... --linux-instance i-... --bucket b \\" >&2
  echo "                   --windows-role r [--volume-id v] [--directory-id d] [--vpc-id v] [--svm s] [--region r]" >&2
  echo "       teardown.sh --after-failed-create [--apply] --file-system-id fs-... [--bucket b] [--windows-role r]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --after-failed-create) AFTER_FAILED_CREATE=1; shift ;;
    --file-system-id) FS_ID="${2:-}"; shift 2 ;;
    --volume-id) VOLUME_ID="${2:-}"; shift 2 ;;
    --linux-instance) LNX_INSTANCE="${2:-}"; shift 2 ;;
    --bucket) BUCKET="${2:-}"; shift 2 ;;
    --windows-role) WINDOWS_ROLE="${2:-}"; shift 2 ;;
    --directory-id) DIRECTORY_ID="${2:-}"; shift 2 ;;
    --vpc-id) VPC_ID="${2:-}"; shift 2 ;;
    --svm) SVM="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "teardown: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# Required inputs are checked before any call, so a partial invocation cannot start deleting.
if [ -n "$APPLY" ]; then
  missing=""
  [ -n "$FS_ID" ] || missing="$missing --file-system-id"
  if [ -z "$AFTER_FAILED_CREATE" ]; then
    [ -n "$LNX_INSTANCE" ] || missing="$missing --linux-instance"
    [ -n "$BUCKET" ] || missing="$missing --bucket"
    [ -n "$WINDOWS_ROLE" ] || missing="$missing --windows-role"
  fi
  if [ -n "$missing" ]; then
    echo "teardown: --apply needs:$missing" >&2; usage; exit 2
  fi
fi

die() { local code="$1"; shift; echo "teardown: $*" >&2; exit "$code"; }

# Print a step. Without --apply nothing else happens (report only, no AWS call).
step() {
  local description="$1"; shift
  echo "- $description"
  if [ -z "$APPLY" ]; then
    echo "    (report only; pass --apply to run)"
    return 0
  fi
  "$@"
}

# Every AWS call goes through here. Under dry-run it is printed (to stderr, so a call inside $(...)
# does not leak the line into its result) and not made.
aws_r() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION $*" >&2
    return 0
  fi
  aws --region "$REGION" "$@"
}

nap() { if [ -z "$DRY_RUN" ]; then sleep "$1"; fi; }

# in_list <item> <whitespace-separated list>: membership without a pipeline.
in_list() {
  local item="$1" x
  for x in $2; do
    if [ "$x" = "$item" ]; then return 0; fi
  done
  return 1
}

# ---------------------------------------------------------------- SSM Run Command on the Linux host

# Run one script from the staged scripts/ontap/ on the Linux host and print its output. Returns the
# script's exit code (the SSM ResponseCode). The output object SSM writes is `stdout`, not *.json,
# so the result is read from StandardOutputContent after the command reaches a terminal state.
ssm_run_ontap() {
  local script="$1"; shift
  local remote
  # The remote shell is sh-compatible (no pipefail needed: the command has no pipeline).
  remote="set -eu; d=\$(mktemp -d); trap 'rm -rf \"\$d\"' EXIT; aws --region $REGION s3 cp --recursive --quiet s3://$BUCKET/$STAGE_PREFIX/ \"\$d/\"; cd \"\$d\"; sha256sum $script; bash ./$script $*"
  local params
  params="$(APPMOD_REMOTE="$remote" python3 -c 'import json,os; print(json.dumps({"commands": [os.environ["APPMOD_REMOTE"]]}))')"
  if [ -n "$DRY_RUN" ]; then
    aws_r ssm send-command --instance-ids "$LNX_INSTANCE" --document-name AWS-RunShellScript \
      --comment "appmod teardown $script" --parameters "$params" \
      --query Command.CommandId --output text
    aws_r ssm wait command-executed --command-id "<command-id>" --instance-id "$LNX_INSTANCE"
    aws_r ssm get-command-invocation --command-id "<command-id>" --instance-id "$LNX_INSTANCE" \
      --query '[Status,ResponseCode,StandardOutputContent,StandardErrorContent]'
    return 0
  fi
  local command_id status i
  command_id="$(aws_r ssm send-command --instance-ids "$LNX_INSTANCE" --document-name AWS-RunShellScript \
    --comment "appmod teardown $script" --parameters "$params" \
    --query Command.CommandId --output text)"
  # The waiter gives up after a fixed number of polls; repeat it until the status is terminal.
  for i in $(seq 1 60); do
    aws_r ssm wait command-executed --command-id "$command_id" --instance-id "$LNX_INSTANCE" 2>/dev/null || true
    status="$(aws_r ssm get-command-invocation --command-id "$command_id" --instance-id "$LNX_INSTANCE" \
      --query Status --output text)"
    case "$status" in Pending|InProgress|Delayed) continue ;; *) break ;; esac
  done
  aws_r ssm get-command-invocation --command-id "$command_id" --instance-id "$LNX_INSTANCE" \
    --query StandardOutputContent --output text
  aws_r ssm get-command-invocation --command-id "$command_id" --instance-id "$LNX_INSTANCE" \
    --query StandardErrorContent --output text >&2
  local rc
  rc="$(aws_r ssm get-command-invocation --command-id "$command_id" --instance-id "$LNX_INSTANCE" \
    --query ResponseCode --output text)"
  echo "    $script: status=$status rc=$rc (after $i wait round(s))"
  if [ "$status" != "Success" ]; then
    if [ "$rc" -gt 0 ] 2>/dev/null; then return "$rc"; fi
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- steps

start_linux_and_wait_ssm() {
  aws_r ec2 start-instances --instance-ids "$LNX_INSTANCE" --query 'StartingInstances[].CurrentState.Name' --output text
  aws_r ec2 wait instance-running --instance-ids "$LNX_INSTANCE"
  local i ping=""
  for i in $(seq 1 "$SSM_ONLINE_TRIES"); do
    ping="$(aws_r ssm describe-instance-information --filters "Key=InstanceIds,Values=$LNX_INSTANCE" \
      --query 'InstanceInformationList[0].PingStatus' --output text)"
    if [ -n "$DRY_RUN" ] || [ "$ping" = "Online" ]; then
      echo "    $LNX_INSTANCE is running and SSM PingStatus is ${ping:-Online (dry-run)}"
      break
    fi
    nap "$SSM_ONLINE_SLEEP"
  done
  if [ -z "$DRY_RUN" ] && [ "$ping" != "Online" ]; then
    die 1 "$LNX_INSTANCE did not reach SSM Online after $i checks (last: ${ping:-none}); nothing deleted"
  fi
  # Stage the tracked files of scripts/ontap/ for the host, one by one, so an untracked or scratch
  # file in that directory is never uploaded. Read back from the bucket by the instance role.
  local tracked f
  local root tracked f
  root="$(cd "$HERE/.." && pwd)"
  tracked="$(git -C "$root" ls-files -- scripts/ontap)"
  if [ -z "$tracked" ]; then
    die 1 "git ls-files lists nothing under scripts/ontap; run teardown.sh from the repository checkout"
  fi
  for f in $tracked; do
    aws_r s3 cp --quiet "$root/$f" "s3://$BUCKET/$STAGE_PREFIX/${f#scripts/ontap/}"
  done
}

run_check_no_locking() {
  if ! ssm_run_ontap check-no-locking.sh --file-system-id "$FS_ID" --region "$REGION"; then
    die 3 "check-no-locking failed (a lock was found or the scan failed). Nothing deleted. If lock-fsxadmin.sh is on, run it off first; otherwise report to a human"
  fi
}

delete_stage3() {
  local present=""
  if [ -n "$DRY_RUN" ]; then
    aws_r cloudformation list-stacks --query "StackSummaries[?StackName=='$STAGE3_STACK' && StackStatus!='DELETE_COMPLETE'].StackStatus" --output text
    present="dry-run"
  else
    present="$(aws_r cloudformation list-stacks \
      --query "StackSummaries[?StackName=='$STAGE3_STACK' && StackStatus!='DELETE_COMPLETE'].StackStatus" --output text)"
    if [ "$present" = "None" ]; then present=""; fi
  fi
  if [ -z "$present" ]; then
    echo "    $STAGE3_STACK not deployed; nothing to delete"
    return 0
  fi
  # Disable the schedule first so no invocation runs while the stack is being deleted.
  local schedule input
  if [ -n "$DRY_RUN" ]; then
    aws_r scheduler get-schedule --name "$SCHEDULE_NAME" --output json
    aws_r scheduler update-schedule --cli-input-json "<get-schedule output with State=DISABLED>"
  elif schedule="$(aws_r scheduler get-schedule --name "$SCHEDULE_NAME" --output json 2>/dev/null)"; then
    input="$(printf '%s' "$schedule" | python3 -c 'import json,sys
s = json.load(sys.stdin)
keep = ("Name", "GroupName", "ScheduleExpression", "ScheduleExpressionTimezone", "StartDate",
        "EndDate", "Description", "KmsKeyArn", "Target", "FlexibleTimeWindow", "ActionAfterCompletion")
out = {k: s[k] for k in keep if k in s}
out["State"] = "DISABLED"
print(json.dumps(out))')"
    aws_r scheduler update-schedule --cli-input-json "$input" >/dev/null
    echo "    schedule $SCHEDULE_NAME disabled"
  else
    echo "    schedule $SCHEDULE_NAME not found; continuing to the stack delete"
  fi
  aws_r cloudformation delete-stack --stack-name "$STAGE3_STACK"
  if ! aws_r cloudformation wait stack-delete-complete --stack-name "$STAGE3_STACK"; then
    die 1 "$STAGE3_STACK did not reach DELETE_COMPLETE; check its stack events, then re-run"
  fi
}

confirm_no_access_point() {
  local volume="$1" i n_vol="0" n_fs
  for i in $(seq 1 "$ABSENCE_TRIES"); do
    if [ -n "$volume" ]; then
      n_vol="$(aws_r fsx describe-s3-access-point-attachments --filters "Name=volume-id,Values=$volume" \
        --query 'length(S3AccessPointAttachments)' --output text)"
    fi
    n_fs="$(aws_r fsx describe-s3-access-point-attachments --filters "Name=file-system-id,Values=$FS_ID" \
      --query 'length(S3AccessPointAttachments)' --output text)"
    if [ -n "$DRY_RUN" ] || { [ "$n_vol" = "0" ] && [ "$n_fs" = "0" ]; }; then
      echo "    0 S3 Access Point attachments on $TARGET_VOLUME and on the file system"
      return 0
    fi
    nap "$ABSENCE_SLEEP"
  done
  die 1 "S3 Access Point attachments remain (volume: $n_vol, file system: $n_fs); appdata not deleted"
}

run_sweep() {
  if ! ssm_run_ontap integration-clone.sh sweep --credential fsxadmin \
      --file-system-id "$FS_ID" --svm "$SVM" --region "$REGION"; then
    die 1 "integration-clone.sh sweep failed; appdata not deleted"
  fi
}

run_verify_clean() {
  if ! ssm_run_ontap integration-clone.sh verify-clean --credential fsxadmin \
      --file-system-id "$FS_ID" --svm "$SVM" --region "$REGION"; then
    die 1 "recovery queue / FlexClone / it_* check failed; appdata not deleted"
  fi
}

# Resolve appdata by enumerating the file system's volumes. Prints the volume id, or "" when it is
# already gone. A given --volume-id must match.
resolve_appdata_volume() {
  if [ -n "$DRY_RUN" ]; then
    aws_r fsx describe-volumes --filters "Name=file-system-id,Values=$FS_ID" \
      --query "Volumes[?Name=='$TARGET_VOLUME'].VolumeId" --output text
    printf '%s' "${VOLUME_ID:-<appdata-volume-id>}"
    return 0
  fi
  local found
  found="$(aws_r fsx describe-volumes --filters "Name=file-system-id,Values=$FS_ID" \
    --query "Volumes[?Name=='$TARGET_VOLUME'].VolumeId" --output text)"
  if [ "$found" = "None" ]; then found=""; fi
  case "$found" in *[[:space:]]*) die 2 "more than one volume named $TARGET_VOLUME on $FS_ID: $found" ;; esac
  if [ -n "$VOLUME_ID" ] && [ -n "$found" ] && [ "$found" != "$VOLUME_ID" ]; then
    die 2 "--volume-id $VOLUME_ID is not the $TARGET_VOLUME volume of $FS_ID ($found); refusing"
  fi
  printf '%s' "$found"
}

wait_volume_gone() {
  local id="$1" i left
  for i in $(seq 1 20); do
    left="$(aws_r fsx describe-volumes --filters "Name=file-system-id,Values=$FS_ID" \
      --query "Volumes[?VolumeId=='$id'].VolumeId" --output text)"
    if [ -n "$DRY_RUN" ] || [ -z "$left" ] || [ "$left" = "None" ]; then
      echo "    $id no longer listed by describe-volumes"
      return 0
    fi
    nap 30
  done
  die 1 "$id is still listed after deletion; check describe-volumes, then re-run"
}

# delete-volume with SkipFinalBackup=true. A record whose ONTAP volume step 4 already removed may
# still be listed until the FSx for ONTAP API catches up (unverified); VolumeNotFound from the
# delete is treated as already gone, and wait_volume_gone confirms it. Any other error stops.
delete_fsx_volume() {
  local id="$1" err
  if [ -n "$DRY_RUN" ]; then
    aws_r fsx delete-volume --volume-id "$id" --ontap-configuration SkipFinalBackup=true
    return 0
  fi
  err="$(mktemp)"
  if aws_r fsx delete-volume --volume-id "$id" --ontap-configuration SkipFinalBackup=true 2>"$err"; then
    rm -f "$err"
    return 0
  fi
  if grep -q "VolumeNotFound" "$err"; then
    echo "    $id: delete-volume answered VolumeNotFound; treating it as already gone"
    rm -f "$err"
    return 0
  fi
  cat "$err" >&2
  rm -f "$err"
  die 1 "delete-volume $id failed; check describe-volumes, then re-run"
}

# Set by steps 3 and 6 from the enumerated appdata volume id, so step 10 can check backups by
# volume id even when --volume-id was not passed.
APPDATA_ID=""

delete_appdata() {
  # FSx for ONTAP API records of FlexClones (appdata_it_*) go first; the ONTAP side was swept in step 4.
  local leftovers id
  leftovers="$(aws_r fsx describe-volumes --filters "Name=file-system-id,Values=$FS_ID" \
    --query "Volumes[?starts_with(Name, 'appdata_it_')].VolumeId" --output text)"
  if [ "$leftovers" = "None" ]; then leftovers=""; fi
  for id in $leftovers; do
    delete_fsx_volume "$id"
    wait_volume_gone "$id"
  done
  local appdata
  appdata="$(resolve_appdata_volume)"
  if [ -z "$appdata" ]; then
    echo "    $TARGET_VOLUME is already gone from $FS_ID"
    return 0
  fi
  APPDATA_ID="$appdata"
  # ONTAP refuses to delete a volume with S3 Access Points; the AWS API is the only path, and
  # SkipFinalBackup=true keeps the delete from leaving a billed final backup.
  delete_fsx_volume "$appdata"
  wait_volume_gone "$appdata"
}

empty_bucket() {
  if [ -z "$BUCKET" ]; then
    echo "    no --bucket given; if the artifacts bucket exists the base stack delete will fail on it"
    return 0
  fi
  local exists
  exists="$(aws_r s3api list-buckets --query "Buckets[?Name=='$BUCKET'].Name" --output text)"
  if [ -z "$DRY_RUN" ] && { [ -z "$exists" ] || [ "$exists" = "None" ]; }; then
    echo "    bucket $BUCKET does not exist"
    return 0
  fi
  aws_r s3 rm "s3://$BUCKET" --recursive --quiet
}

remove_out_of_band_policies() {
  if [ -z "$WINDOWS_ROLE" ]; then
    echo "    no --windows-role given; skipping (a fresh environment has no out-of-band policy)"
    return 0
  fi
  local present policy
  present="$(aws_r iam list-role-policies --role-name "$WINDOWS_ROLE" --query PolicyNames --output text)"
  for policy in $OUT_OF_BAND_WINDOWS_POLICIES; do
    if [ -n "$DRY_RUN" ] || in_list "$policy" "$present"; then
      aws_r iam delete-role-policy --role-name "$WINDOWS_ROLE" --policy-name "$policy"
      echo "    removed inline policy $policy from $WINDOWS_ROLE"
    else
      echo "    inline policy $policy absent on $WINDOWS_ROLE (fresh environment); nothing to delete"
    fi
  done
}

delete_base() {
  aws_r cloudformation delete-stack --stack-name "$BASE_STACK"
  if ! aws_r cloudformation wait stack-delete-complete --stack-name "$BASE_STACK"; then
    die 1 "$BASE_STACK did not reach DELETE_COMPLETE; save its stack events, fix the dependency, re-run"
  fi
}

delete_secrets() {
  local listed secret
  listed="$(aws_r secretsmanager list-secrets --include-planned-deletion \
    --filters Key=name,Values=appmod/ --query 'SecretList[].Name' --output text)"
  for secret in $SECRETS; do
    if [ -n "$DRY_RUN" ] || in_list "$secret" "$listed"; then
      aws_r secretsmanager delete-secret --secret-id "$secret" --force-delete-without-recovery
    else
      echo "    secret $secret already absent"
    fi
  done
}

# Step 10: enumerate through the APIs; the stack resource list is gone by now and would not show a
# retained or out-of-band resource anyway. Each check prints what it found.
residuals() {
  local out="" v
  v="$(aws_r fsx describe-file-systems --query "FileSystems[?FileSystemId=='$FS_ID'].FileSystemId" --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out file-system:$v"
  v="$(aws_r fsx describe-storage-virtual-machines --filters "Name=file-system-id,Values=$FS_ID" \
    --query 'StorageVirtualMachines[].StorageVirtualMachineId' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out svm:$v"
  v="$(aws_r fsx describe-volumes --filters "Name=file-system-id,Values=$FS_ID" --query 'Volumes[].VolumeId' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out volume:$v"
  v="$(aws_r fsx describe-backups --filters "Name=file-system-id,Values=$FS_ID" --query 'Backups[].BackupId' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out backup(fs):$v"
  # H4: backups by volume id as well. confirm_absence reports when no appdata id is known.
  local vid="${VOLUME_ID:-$APPDATA_ID}"
  if [ -n "$vid" ]; then
    v="$(aws_r fsx describe-backups --filters "Name=volume-id,Values=$vid" --query 'Backups[].BackupId' --output text)"
    [ -z "$v" ] || [ "$v" = "None" ] || out="$out backup(volume):$v"
  fi
  v="$(aws_r fsx describe-s3-access-point-attachments --filters "Name=file-system-id,Values=$FS_ID" \
    --query 'S3AccessPointAttachments[].Name' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out access-point:$v"
  v="$(aws_r ds describe-directories \
    --query "DirectoryDescriptions[?DirectoryId=='${DIRECTORY_ID:-none}' || Name=='$DOMAIN_NAME'].DirectoryId" --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out directory:$v"
  v="$(aws_r ec2 describe-instances --filters Name=tag-key,Values=appmod \
    "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down" \
    --query 'Reservations[].Instances[].InstanceId' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out ec2:$v"
  # ENIs and endpoints: by tag, and also by the dedicated VPC when --vpc-id is given (the file
  # system and directory ENIs carry no appmod tag). Pass --vpc-id only for a VPC this stack created.
  local net_filter="Name=tag-key,Values=appmod"
  if [ -n "$VPC_ID" ]; then net_filter="Name=vpc-id,Values=$VPC_ID"; fi
  v="$(aws_r ec2 describe-vpcs --filters Name=tag-key,Values=appmod --query 'Vpcs[].VpcId' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out vpc:$v"
  v="$(aws_r ec2 describe-network-interfaces --filters "$net_filter" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out eni:$v"
  # shellcheck disable=SC2016  # backticks are JMESPath literals, not shell expansion
  v="$(aws_r ec2 describe-vpc-endpoints --filters "$net_filter" \
    --query 'VpcEndpoints[?State!=`deleted` && State!=`Deleted`].VpcEndpointId' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out endpoint:$v"
  v="$(aws_r secretsmanager list-secrets --include-planned-deletion --filters Key=name,Values=appmod/ \
    --query 'SecretList[].Name' --output text)"
  [ -z "$v" ] || [ "$v" = "None" ] || out="$out secret:$v"
  printf '%s' "$out"
}

confirm_absence() {
  local i left=""
  if [ -n "${VOLUME_ID:-$APPDATA_ID}" ]; then
    echo "    backup(volume): checking describe-backups by volume id ${VOLUME_ID:-$APPDATA_ID}"
  else
    echo "    backup(volume): skipped, appdata id unknown (appdata was already gone when resolved and"
    echo "    no --volume-id was given); only the file-system-id backup filter is checked"
  fi
  for i in $(seq 1 "$ABSENCE_TRIES"); do
    left="$(residuals)"
    if [ -z "$left" ]; then
      echo "    nothing left (file system, SVM, volumes, backups, access points, directory, tagged"
      echo "    EC2/ENI/endpoints, secrets) after $i check(s)"
      return 0
    fi
    echo "    still present (check $i/$ABSENCE_TRIES):$left"
    nap "$ABSENCE_SLEEP"
  done
  die 1 "resources remain after $ABSENCE_TRIES checks:$left"
}

# After a failed create there is no Linux EC2 and no ONTAP operation has run, so SnapLock is checked
# through the FSx for ONTAP API. Snapshot locking has no AWS API field; it can only have been set by
# an ONTAP call, and none was made (.private/runs/ has no ONTAP execution record).
check_no_snaplock_via_api() {
  local locked
  # shellcheck disable=SC2016  # backticks are JMESPath literals, not shell expansion
  locked="$(aws_r fsx describe-volumes --filters "Name=file-system-id,Values=$FS_ID" \
    --query 'Volumes[?OntapConfiguration.SnaplockConfiguration!=`null`].Name' --output text)"
  if [ -n "$locked" ] && [ "$locked" != "None" ]; then
    die 3 "SnapLock volume(s) found: $locked. Nothing deleted; report to a human"
  fi
  echo "    no SnapLock configuration on any volume of the file system"
}

teardown_after_failed_create() {
  echo "teardown: after-failed-create path in $REGION (file system exists, no Linux EC2; apply=${APPLY:-no})"
  echo "- 0' no Linux EC2, so no instance start and no Run Command. Confirm .private/runs/ has no"
  echo "     ONTAP execution record (no ONTAP call ran, so no snapshot locking can have been set)"
  step "1' describe-volumes shows no SnaplockConfiguration on any volume of ${FS_ID:-<fs-id>}" \
    check_no_snaplock_via_api
  step "6 delete $TARGET_VOLUME (resolved by describe-volumes) with SkipFinalBackup=true; confirm gone" \
    delete_appdata
  step "7 empty the artifacts bucket ${BUCKET:-<not given>}" empty_bucket
  step "7b delete the out-of-band Windows-role inline policies if present" remove_out_of_band_policies
  step "8 delete $BASE_STACK and wait for DELETE_COMPLETE" delete_base
  step "9 delete the four secrets with --force-delete-without-recovery" delete_secrets
  step "10 confirm absence by API enumeration ($ABSENCE_TRIES tries, ${ABSENCE_SLEEP}s apart)" confirm_absence
  echo "- 11 next day: confirm in Cost Explorer that billing stopped."
}

teardown_full() {
  echo "teardown: full deletion order in $REGION (apply=${APPLY:-no})"
  echo "  file system ${FS_ID:-<fs-id>}, Linux EC2 ${LNX_INSTANCE:-<instance-id>}, bucket ${BUCKET:-<bucket>}, Windows role ${WINDOWS_ROLE:-<role>}"
  step "0 start the Linux EC2 ${LNX_INSTANCE:-<instance-id>} (Run Command target), wait for SSM PingStatus Online, stage scripts/ontap/ to the bucket" \
    start_linux_and_wait_ssm
  step "1 check-no-locking.sh over all volumes on the Linux host (stop on any lock)" run_check_no_locking
  step "2 disable the Scheduler schedule and delete $STAGE3_STACK (detaches the S3 Access Point)" delete_stage3
  step "3 confirm 0 S3 Access Point attachments on $TARGET_VOLUME and the file system" confirm_no_access_point_resolved
  step "4 integration-clone.sh sweep: delete leftover FlexClone appdata_it_* and snapshots it_*" run_sweep
  step "5 integration-clone.sh verify-clean: recovery queue, FlexClone list, it_* snapshots all empty" run_verify_clean
  step "6 delete leftover appdata_it_* records, then $TARGET_VOLUME with SkipFinalBackup=true; confirm gone" delete_appdata
  step "7 empty the artifacts bucket ${BUCKET:-<bucket>}" empty_bucket
  step "7b delete the out-of-band Windows-role inline policies ($OUT_OF_BAND_WINDOWS_POLICIES) if present" \
    remove_out_of_band_policies
  step "8 delete $BASE_STACK and wait for DELETE_COMPLETE" delete_base
  step "9 delete the four secrets with --force-delete-without-recovery" delete_secrets
  step "10 confirm absence by API enumeration ($ABSENCE_TRIES tries, ${ABSENCE_SLEEP}s apart)" confirm_absence
  echo "- 11 next day: confirm in Cost Explorer that billing stopped."
}

# Step 3 needs the appdata volume id; resolve it (by enumeration) right before the check.
confirm_no_access_point_resolved() {
  local resolved
  resolved="$(resolve_appdata_volume)"
  if [ -z "$resolved" ]; then
    echo "    $TARGET_VOLUME already gone; checking the file system only"
  else
    APPDATA_ID="$resolved"
  fi
  confirm_no_access_point "$resolved"
}

if [ -n "$AFTER_FAILED_CREATE" ]; then
  teardown_after_failed_create
else
  teardown_full
fi
