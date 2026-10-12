#!/bin/bash
#
# Read-only dump of recorded Worker output and the seed inventory, for collect_out.py. Runs on the
# Linux client (sent through ssm_run.py). Writes, deletes and moves nothing; every access to the
# volume is as the NFS uid through setpriv.
#
#   APPMOD_STAGES    comma-separated stage names under out/ to dump, e.g. s0,s2m (required)
#   APPMOD_NFS_ROOT  NFS mount point (default /mnt/appdata)
#   APPMOD_NFS_UID   uid and gid of the NFS side (default 10001; 0 is refused)
#
# For each stage: "== ls out/<stage>", then "== B64 <stage>/<relative path>" followed by the file
# content as one base64 line, for index.json and every file under reports/. Then the seed inventory:
# "== seed sha256" (sorted sha256sum lines), "== seed aggregate" (first 16 hex of the sha256 of that
# listing), "== seed dir order (unsorted, ls -f)" and "== top-level". Exit 2: bad argument. Exit 3:
# the NFS root is not mounted.
#

# The single-quoted snippets are meant to expand in the inner sh (they use its $1, $2, $3).
# shellcheck disable=SC2016
set -uo pipefail
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  awk 'NR > 1 { if (/^#/) { sub(/^# ?/, ""); print } else exit }' "$0"
  exit 0
fi
die() { local code="$1"; shift; echo "read-out: $*" >&2; exit "$code"; }
STAGES_ARG="${APPMOD_STAGES:-}"
NFS_ROOT="${APPMOD_NFS_ROOT:-/mnt/appdata}"
NFS_UID="${APPMOD_NFS_UID:-10001}"
STAGE_RE='^s[0-9][a-z]{0,2}$'
PATH_RE='^/[A-Za-z0-9._/-]+$'
[ -n "$STAGES_ARG" ] || die 2 "APPMOD_STAGES is required"
[[ $NFS_ROOT =~ $PATH_RE ]] || die 2 "APPMOD_NFS_ROOT must be an absolute path"
[[ $NFS_UID =~ ^[0-9]+$ ]] || die 2 "APPMOD_NFS_UID must be a number"
[ "$NFS_UID" -ne 0 ] || die 2 "APPMOD_NFS_UID must not be 0"
case "/$NFS_ROOT/" in */../*) die 2 "'..' is not allowed in a path: $NFS_ROOT" ;; esac
IFS=',' read -r -a STAGES <<<"$STAGES_ARG"
for s in "${STAGES[@]}"; do
  [[ $s =~ $STAGE_RE ]] || die 2 "APPMOD_STAGES has a malformed stage: $s"
done
command -v setpriv >/dev/null 2>&1 || die 2 "setpriv is required"
mountpoint -q "$NFS_ROOT" || die 3 "not mounted: $NFS_ROOT"
as_nfs_uid() { setpriv --reuid="$NFS_UID" --regid="$NFS_UID" --clear-groups "$@"; }
for s in "${STAGES[@]}"; do
  echo "== ls out/$s"
  as_nfs_uid ls -la "$NFS_ROOT/out/$s" "$NFS_ROOT/out/$s/reports"
  files="index.json"
  # Names are listed with find; a name with whitespace would break the dump format, so it is
  # reported instead of dumped.
  while IFS= read -r f; do
    case "$f" in
      *[[:space:]]*) echo "== SKIP $s/reports/$f (whitespace in name)" ;;
      *) files="$files reports/$f" ;;
    esac
  done < <(as_nfs_uid sh -c 'cd "$1/out/$2/reports" 2>/dev/null && find . -type f | sed "s|^\./||" | sort' _ "$NFS_ROOT" "$s")
  for f in $files; do
    echo "== B64 $s/$f"
    as_nfs_uid sh -c 'base64 < "$1/out/$2/$3" | tr -d "\n"' _ "$NFS_ROOT" "$s" "$f"
    echo
  done
done
echo "== seed sha256"
as_nfs_uid sh -c 'cd "$1" && find seed -type f -exec sha256sum {} + | sort' _ "$NFS_ROOT"
echo "== seed aggregate"
as_nfs_uid sh -c 'cd "$1" && find seed -type f -exec sha256sum {} + | sort | sha256sum | cut -c1-16' _ "$NFS_ROOT"
echo "== seed dir order (unsorted, ls -f)"
as_nfs_uid ls -f "$NFS_ROOT/seed/inbox"
echo "== top-level"
as_nfs_uid ls -la "$NFS_ROOT"
exit 0
