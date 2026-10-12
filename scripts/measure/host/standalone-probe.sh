#!/bin/bash
#
# Standalone Probe on the Linux client: NFS first (as the NFS uid), then cifs (as root).
# Runs on the host (sent through ssm_run.py). Lists the volume top and /mnt before and after, and
# looks for leftover .docintake-canwrite-* files afterwards. Deletes and moves nothing.
#
#   APPMOD_RUN_ID     run id, e.g. s2m-YYYYMMDDTHHMMSSZ (required)
#   APPMOD_PROBE_BIN  absolute path to the published Probe executable (required, must be +x)
#   APPMOD_STAGE      stage digit 0..9 (required)
#   APPMOD_NFS_ROOT   NFS mount point (default /mnt/appdata)
#   APPMOD_SMB_ROOT   cifs mount point (default /mnt/appdata-smb)
#   APPMOD_NFS_UID    uid and gid of the NFS side (default 10001; 0 is refused)
#
# Output markers: ===PRE-BEGIN/END===, ===NFS-BEGIN/END=== with "nfs-exit=N", ===SMB-BEGIN/END===
# with "smb-exit=N", ===POST-BEGIN/END===. The script exits 0 after the Probes ran, whatever their
# exit codes (they are printed). Exit 2: bad argument. Exit 3: a mount point is not mounted (a
# Probe on an unmounted path would write to the local disk).
#
# The NFS side runs through setpriv with the NFS uid and no supplementary groups: over NFS with
# sec=sys the principal is the local uid, and the Run Command shell is root, which the export maps
# to the anonymous user. Over cifs the principal is the mount credential whatever the local uid.
#
set -uo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

die() { local code="$1"; shift; echo "standalone-probe: $*" >&2; exit "$code"; }

RUN_ID="${APPMOD_RUN_ID:-}"
PROBE="${APPMOD_PROBE_BIN:-}"
STAGE="${APPMOD_STAGE:-}"
NFS_ROOT="${APPMOD_NFS_ROOT:-/mnt/appdata}"
SMB_ROOT="${APPMOD_SMB_ROOT:-/mnt/appdata-smb}"
NFS_UID="${APPMOD_NFS_UID:-10001}"

RUN_ID_RE='^s[0-9][a-z]{0,2}-[0-9]{8}T[0-9]{6}Z$'
PATH_RE='^/[A-Za-z0-9._/-]+$'

[[ $RUN_ID =~ $RUN_ID_RE ]] || die 2 "APPMOD_RUN_ID is missing or malformed"
[[ $STAGE =~ ^[0-9]$ ]] || die 2 "APPMOD_STAGE must be one digit"
[[ $PROBE =~ $PATH_RE ]] || die 2 "APPMOD_PROBE_BIN must be an absolute path"
[[ $NFS_ROOT =~ $PATH_RE ]] || die 2 "APPMOD_NFS_ROOT must be an absolute path"
[[ $SMB_ROOT =~ $PATH_RE ]] || die 2 "APPMOD_SMB_ROOT must be an absolute path"
[[ $NFS_UID =~ ^[0-9]+$ ]] || die 2 "APPMOD_NFS_UID must be a number"
[ "$NFS_UID" -ne 0 ] || die 2 "APPMOD_NFS_UID must not be 0 (the NFS side must not run as root)"
for p in "$PROBE" "$NFS_ROOT" "$SMB_ROOT"; do
  case "/$p/" in */../*) die 2 "'..' is not allowed in a path: $p" ;; esac
done
[ -f "$PROBE" ] && [ -x "$PROBE" ] || die 2 "APPMOD_PROBE_BIN is not an executable file: $PROBE"
command -v setpriv >/dev/null 2>&1 || die 2 "setpriv is required"
mountpoint -q "$NFS_ROOT" || die 3 "not mounted: $NFS_ROOT"
mountpoint -q "$SMB_ROOT" || die 3 "not mounted: $SMB_ROOT"

as_nfs_uid() { setpriv --reuid="$NFS_UID" --regid="$NFS_UID" --clear-groups "$@"; }

echo "===PRE-BEGIN==="
echo "--- vol-top (uid $NFS_UID)"
as_nfs_uid ls -1 "$NFS_ROOT"
echo "--- mnt-top"
ls -1 /mnt
echo "===PRE-END==="

echo "===NFS-BEGIN==="
as_nfs_uid "$PROBE" --store nfs --root "$NFS_ROOT" --stage "$STAGE" --role standalone --run-id "$RUN_ID"
echo "nfs-exit=$?"
echo "===NFS-END==="

echo "===SMB-BEGIN==="
"$PROBE" --store smb --root "$SMB_ROOT" --stage "$STAGE" --role standalone --run-id "$RUN_ID"
echo "smb-exit=$?"
echo "===SMB-END==="

echo "===POST-BEGIN==="
echo "--- vol-top (uid $NFS_UID)"
as_nfs_uid ls -1 "$NFS_ROOT"
echo "--- mnt-top"
ls -1 /mnt
echo "--- canwrite leftovers"
as_nfs_uid find "$NFS_ROOT" -maxdepth 3 -name '.docintake-canwrite-*'
echo "===POST-END==="
exit 0
