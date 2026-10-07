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
