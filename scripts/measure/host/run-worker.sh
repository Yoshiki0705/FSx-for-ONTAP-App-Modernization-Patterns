#!/bin/bash
#
# Worker golden-path run on the Linux client: one pass of the published Worker over NFS, as the NFS
# uid, writing only out/<stage>/. Runs on the host (sent through ssm_run.py). Deletes and moves
# nothing, on the volume or anywhere else.
#
#   APPMOD_WORKER_BIN    absolute path to the published Worker executable (required, must be +x)
#   APPMOD_WORKER_STAGE  stage directory name under out/, e.g. s2m (required, NO default)
#   APPMOD_WORKER_STORE  nfs (default; the only value accepted)
#   APPMOD_NFS_ROOT      NFS mount point (default /mnt/appdata)
#   APPMOD_NFS_UID       uid and gid of the NFS side (default 10001; 0 is refused)
#   APPMOD_REF_STAGES    comma-separated stages to print as reference, read only (default none)
#
# Guards, checked before the Worker starts:
#   exit 2  an argument is malformed, the Worker is not an executable file, or the uid is 0
#   exit 3  APPMOD_WORKER_STAGE is unset, empty or not s<digit>[a-z]{0,2}. The Worker's own default
#           stage is s0, so an unset variable would write into out/s0, the legacy baseline; the
#           stage is therefore always passed explicitly. Names such as seed, probe and out cannot
#           match the pattern
#   exit 4  out/<stage> already exists on the volume, seen as the NFS uid. Nothing is run
#   exit 5  the NFS root is not a mount point, or out/ is not a directory for the NFS uid
# After the Worker has run the script exits 0 whatever the Worker's exit code ("worker-exit=N" is
# printed), so the output is kept for diagnosis.
#
# Every access to the volume goes through setpriv with the NFS uid and no supplementary groups: over
# NFS with sec=sys the principal is the local uid, and the Run Command shell is root, which the
# export maps to the anonymous user.
#

# The single-quoted snippets are meant to expand in the inner sh (they use its $1, $2, $3).
# shellcheck disable=SC2016
set -uo pipefail
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  awk 'NR > 1 { if (/^#/) { sub(/^# ?/, ""); print } else exit }' "$0"
  exit 0
fi
die() { local code="$1"; shift; echo "run-worker: $*" >&2; exit "$code"; }
STAGE="${APPMOD_WORKER_STAGE:-}"
WORKER="${APPMOD_WORKER_BIN:-}"
STORE="${APPMOD_WORKER_STORE:-nfs}"
NFS_ROOT="${APPMOD_NFS_ROOT:-/mnt/appdata}"
NFS_UID="${APPMOD_NFS_UID:-10001}"
REF_STAGES="${APPMOD_REF_STAGES:-}"
STAGE_RE='^s[0-9][a-z]{0,2}$'
PATH_RE='^/[A-Za-z0-9._/-]+$'
[ -n "$STAGE" ] || die 3 "APPMOD_WORKER_STAGE is not set (the Worker would default to s0)"
[[ $STAGE =~ $STAGE_RE ]] || die 3 "APPMOD_WORKER_STAGE is not a stage name: $STAGE"
[ "$STORE" = "nfs" ] || die 2 "APPMOD_WORKER_STORE must be nfs"
[[ $WORKER =~ $PATH_RE ]] || die 2 "APPMOD_WORKER_BIN must be an absolute path"
[[ $NFS_ROOT =~ $PATH_RE ]] || die 2 "APPMOD_NFS_ROOT must be an absolute path"
[[ $NFS_UID =~ ^[0-9]+$ ]] || die 2 "APPMOD_NFS_UID must be a number"
[ "$NFS_UID" -ne 0 ] || die 2 "APPMOD_NFS_UID must not be 0 (the volume is accessed as the NFS uid)"
for p in "$WORKER" "$NFS_ROOT"; do
  case "/$p/" in */../*) die 2 "'..' is not allowed in a path: $p" ;; esac
done
if ! { [ -f "$WORKER" ] && [ -x "$WORKER" ]; }; then die 2 "APPMOD_WORKER_BIN is not an executable file: $WORKER"; fi
REFS=()
if [ -n "$REF_STAGES" ]; then
  IFS=',' read -r -a REFS <<<"$REF_STAGES"
  for r in "${REFS[@]}"; do
    [[ $r =~ $STAGE_RE ]] || die 2 "APPMOD_REF_STAGES has a malformed stage: $r"
    [ "$r" != "$STAGE" ] || die 2 "APPMOD_REF_STAGES must not contain the stage being written: $r"
  done
fi
command -v setpriv >/dev/null 2>&1 || die 2 "setpriv is required"
mountpoint -q "$NFS_ROOT" || die 5 "not mounted: $NFS_ROOT"
as_nfs_uid() { setpriv --reuid="$NFS_UID" --regid="$NFS_UID" --clear-groups "$@"; }
# vol <script>: run an sh snippet as the NFS uid; $1 is the NFS root, $2 the stage, $3 a reference.
vol() { as_nfs_uid sh -c "$1" _ "$NFS_ROOT" "$STAGE" "${2:-}"; }
as_nfs_uid test -d "$NFS_ROOT/out" || die 5 "out/ is not a directory for uid $NFS_UID under $NFS_ROOT"
if as_nfs_uid test -e "$NFS_ROOT/out/$STAGE" || as_nfs_uid test -L "$NFS_ROOT/out/$STAGE"; then
  die 4 "guard: out/$STAGE already exists; not running"
fi
seedhash() {
  vol 'cd "$1" && find seed -type f -exec sha256sum {} + | sort | sha256sum | cut -c1-16'
}
seedlist() {
  vol 'cd "$1" && find seed -type f -exec sha256sum {} + | sort'
}
echo "===PRE==="
echo "out-top-before: $(as_nfs_uid ls -1 "$NFS_ROOT/out" | tr '\n' ' ')"
echo "vol-top-before: $(as_nfs_uid ls -1 "$NFS_ROOT" | tr '\n' ' ')"
SEED_BEFORE="$(seedhash)"
echo "seed-before=$SEED_BEFORE"
seedlist
echo "===WORKER-BEGIN==="
as_nfs_uid env DOCINTAKE_STORE=nfs DOCINTAKE_ROOT="$NFS_ROOT" DOCINTAKE_STAGE="$STAGE" "$WORKER"
echo "worker-exit=$?"
echo "===WORKER-END==="
echo "===POST==="
SEED_AFTER="$(seedhash)"
echo "seed-after=$SEED_AFTER"
if [ "$SEED_BEFORE" = "$SEED_AFTER" ]; then echo "seed-unchanged=yes"; else echo "seed-unchanged=NO"; fi
seedlist
echo "out-top-after: $(as_nfs_uid ls -1 "$NFS_ROOT/out" | tr '\n' ' ')"
echo "vol-top-after: $(as_nfs_uid ls -1 "$NFS_ROOT" | tr '\n' ' ')"
echo "--- out/$STAGE files"
as_nfs_uid find "$NFS_ROOT/out/$STAGE" -type f
echo "--- out/$STAGE/index.json (raw)"
as_nfs_uid cat "$NFS_ROOT/out/$STAGE/index.json"
echo
echo "--- out/$STAGE/index.json sha256 and size"
vol 'sha256sum "$1/out/$2/index.json"; wc -c < "$1/out/$2/index.json"'
echo "--- out/$STAGE/reports sha256"
vol 'cd "$1/out/$2/reports" && sha256sum *'
for ref in ${REFS[@]+"${REFS[@]}"}; do
  echo "--- out/$ref reports sha256 (reference)"
  vol 'cd "$1/out/$3/reports" && sha256sum *' "$ref"
  echo "--- out/$ref index.json sha256 and size (reference)"
  vol 'sha256sum "$1/out/$3/index.json"; wc -c < "$1/out/$3/index.json"' "$ref"
done
echo "--- out/$STAGE/reports content"
vol 'for f in "$1/out/$2/reports"/*; do echo "## $f"; cat "$f"; done'
echo "--- canwrite leftovers / mnt stray"
as_nfs_uid find "$NFS_ROOT" -maxdepth 3 -name '.docintake-canwrite-*'
ls -1 /mnt
echo "===END==="
exit 0
