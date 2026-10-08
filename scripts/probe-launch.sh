#!/bin/bash
#
# Linux-side probe launcher: ensure the SMB (and, from stage 1, NFS) mount exists, then run
# probe_peer.py. Invoked by run-probe.sh. Credentials are read from appmod/app-users by the
# instance role into a tmpfs 0600 file, never on a command line; the SMB mount uses sec=ntlmssp
# (U24), falling back to sec=krb5.
#
#   probe-launch.sh --store smb|nfs --stage <0..3> --role <role> --run-id <id> [--region r]
#                   [--svm-netbios APPMODSVM01]
#
set -euo pipefail

STORE="" ; STAGE="" ; ROLE="" ; RUN_ID=""
PAIR_BEHAVIOR="" ; SYNC_ID="" ; BUCKET=""
REGION="${APPMOD_REGION:-ap-northeast-1}"
SVM_NETBIOS="${APPMOD_SVM_NETBIOS:-APPMODSVM01}"
PROBE="/opt/appmod/probe_peer.py"
NFS_UID="${APPMOD_NFS_UID:-10001}"

while [ $# -gt 0 ]; do
  case "$1" in
    --store) STORE="${2:-}"; shift 2 ;;
    --stage) STAGE="${2:-}"; shift 2 ;;
    --role) ROLE="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --svm-netbios) SVM_NETBIOS="${2:-}"; shift 2 ;;
    --pair-behavior) PAIR_BEHAVIOR="${2:-}"; shift 2 ;;
    --sync-id) SYNC_ID="${2:-}"; shift 2 ;;
    --bucket) BUCKET="${2:-}"; shift 2 ;;
    *) echo "probe-launch: unknown argument: $1" >&2; exit 2 ;;
  esac
done

for v in STORE STAGE ROLE RUN_ID; do
  if [ -z "${!v}" ]; then echo "probe-launch: --${v,,} is required" >&2; exit 2; fi
done

if [ "$STORE" = "smb" ]; then
  MNT=/mnt/appdata-smb
  mkdir -p "$MNT"
  if ! mountpoint -q "$MNT"; then
    CREDS="$(mktemp /dev/shm/smbcred.XXXXXX)"; chmod 600 "$CREDS"
    PW="$(aws --region "$REGION" secretsmanager get-secret-value --secret-id appmod/app-users \
          --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["appsvc"])')"
    { echo "username=appsvc"; echo "password=$PW"; echo "domain=APPMOD"; } > "$CREDS"; unset PW
    mounted=""
    for sec in ntlmssp krb5; do
      if mount -t cifs "//$SVM_NETBIOS/appdata" "$MNT" -o "credentials=$CREDS,sec=$sec,vers=3.0,uid=0,gid=0" 2>/dev/null; then
        mounted="$sec"; break
      fi
    done
    shred -u "$CREDS" 2>/dev/null || rm -f "$CREDS"
    if [ -z "$mounted" ]; then echo "probe-launch: SMB mount failed (ntlmssp and krb5)" >&2; exit 1; fi
    echo "probe-launch: SMB mounted sec=$mounted" >&2
  fi
  ROOT="$MNT"
elif [ "$STORE" = "nfs" ]; then
  ROOT="/mnt/appdata"
  if ! mountpoint -q "$ROOT"; then echo "probe-launch: NFS mount $ROOT not present (stage 1+ sets it up)" >&2; exit 1; fi
else
  echo "probe-launch: --store must be smb or nfs" >&2; exit 2
fi

# Clock offset against the Amazon Time Sync Service (chrony), in ms, positive when the local clock
# is fast. Recorded with the result; the merge treats a smaller difference as no difference.
OFFSET_MS="$(chronyc tracking 2>/dev/null | awk '/^System time/ {
  v = $4 * 1000; if ($6 == "slow") v = -v; printf "%.6f", v }')" || OFFSET_MS=""

RUN_AS=()
if [ "$STORE" = "nfs" ]; then
  RUN_AS=(setpriv --reuid="$NFS_UID" --regid="$NFS_UID" --clear-groups)
fi

if [ -n "$PAIR_BEHAVIOR" ]; then
  # Coordinated pair. probe_peer.py signals through local directories only; this loop bridges them
  # to the artifacts bucket (s3://<bucket>/probe/<run-id>/sync/<sync-id>/<role>/<name>), so the two
  # hosts never synchronize through the volume under test.
  if [ -z "$SYNC_ID" ] || [ -z "$BUCKET" ]; then
    echo "probe-launch: --pair-behavior needs --sync-id and --bucket" >&2; exit 2
  fi
  case "$ROLE" in
    holder) PEER=contender ;; contender) PEER=holder ;;
    writer) PEER=reader ;; reader) PEER=writer ;;
    *) echo "probe-launch: unknown pair role $ROLE" >&2; exit 2 ;;
  esac
  SYNC="/run/appmod-sync/$SYNC_ID"
  mkdir -p "$SYNC/out" "$SYNC/in"
  if [ "$STORE" = "nfs" ]; then chown -R "$NFS_UID:$NFS_UID" "$SYNC"; fi
  PREFIX="s3://$BUCKET/probe/$RUN_ID/sync/$SYNC_ID"
  "${RUN_AS[@]}" python3 "$PROBE" --store "$STORE" --root "$ROOT" --stage "$STAGE" --role "$ROLE" \
    --run-id "$RUN_ID" --pair-behavior "$PAIR_BEHAVIOR" --sync-id "$SYNC_ID" --sync-dir "$SYNC" \
    ${OFFSET_MS:+--ntp-offset-ms "$OFFSET_MS"} >"$SYNC/stdout.json" 2>"$SYNC/stderr.txt" &
  pid=$!
  declare -A sent=() got=()
  bridge() {
    local f n
    for f in "$SYNC"/out/*; do
      [ -f "$f" ] || continue
      n="$(basename "$f")"
      case "$n" in *.tmp) continue ;; esac
      [ -n "${sent[$n]:-}" ] && continue
      aws --region "$REGION" s3 cp "$f" "$PREFIX/$ROLE/$n" --quiet && sent[$n]=1
    done
    for n in $(aws --region "$REGION" s3 ls "$PREFIX/$PEER/" 2>/dev/null | awk '{print $4}'); do
      [ -n "${got[$n]:-}" ] && continue
      if aws --region "$REGION" s3 cp "$PREFIX/$PEER/$n" "$SYNC/in/$n.tmp" --quiet; then
        chmod 644 "$SYNC/in/$n.tmp"; mv "$SYNC/in/$n.tmp" "$SYNC/in/$n"; got[$n]=1
      fi
    done
  }
  deadline=$((SECONDS + 300))
  while kill -0 "$pid" 2>/dev/null; do
    bridge
    if [ "$SECONDS" -gt "$deadline" ]; then kill "$pid"; break; fi
    sleep 0.2
  done
  wait "$pid" || true
  bridge
  cat "$SYNC/stdout.json"
  cat "$SYNC/stderr.txt" >&2
  exit 0
fi

if [ "$STORE" = "nfs" ]; then
  # Over SMB the principal is the mount credential (APPMOD\appsvc) whatever the local uid. Over
  # NFS with sec=sys it is the local uid, and Run Command runs as root, which the export squashes
  # (superuser none) to the anonymous user and which has no Windows mapping, so every NTFS check
  # denies it (live 2026-10-07). Run as the UNIX user appsvc (uid 10001) that stage1-nfs.sh maps to
  # APPMOD\appsvc, so both protocols measure the same principal.
  exec setpriv --reuid="$NFS_UID" --regid="$NFS_UID" --clear-groups \
    python3 "$PROBE" --store "$STORE" --root "$ROOT" --stage "$STAGE" --role "$ROLE" --run-id "$RUN_ID"
fi
exec python3 "$PROBE" --store "$STORE" --root "$ROOT" --stage "$STAGE" --role "$ROLE" --run-id "$RUN_ID"
