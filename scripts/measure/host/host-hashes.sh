#!/bin/bash
#
# Read-only sha256 listing of host directories, for ship_build.py's delta.
# Runs on the Linux client (sent through ssm_run.py). Changes nothing.
#
#   APPMOD_HASH_DIRS   comma-separated absolute directories under /opt/appmod/ (required)
#
# Output, per directory: a line "===<dir>", then one "<first 16 hex of sha256>  ./<path>" line per
# regular file, sorted. Nothing else is printed. Exit 2 on a missing variable or a directory that
# is not under /opt/appmod/ or does not exist.
#
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

DIRS="${APPMOD_HASH_DIRS:-}"
if [ -z "$DIRS" ]; then
  echo "host-hashes: APPMOD_HASH_DIRS is required" >&2
  exit 2
fi

IFS=',' read -r -a LIST <<<"$DIRS"
for d in "${LIST[@]}"; do
  case "$d" in
    /opt/appmod/*) ;;
    *) echo "host-hashes: not under /opt/appmod/: $d" >&2; exit 2 ;;
  esac
  case "/$d/" in
    */../*) echo "host-hashes: '..' is not allowed: $d" >&2; exit 2 ;;
  esac
  if [ ! -d "$d" ]; then
    echo "host-hashes: not a directory: $d" >&2
    exit 2
  fi
done

for d in "${LIST[@]}"; do
  echo "===$d"
  (cd "$d" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum | cut -c1-16,65-)
done
