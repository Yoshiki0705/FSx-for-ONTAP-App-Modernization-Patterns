#!/usr/bin/env bash
#
# Take a file inventory of seed/ over NFS on the Linux EC2 host (stage 1 and later). Emits the same
# JSON shape as inventory.ps1: for every file under <mount>/seed, the relative path (/-separated),
# size in bytes and SHA-256. Read-only.
#
#   inventory.sh --mount /mnt/appdata --out /tmp/linux-inventory.json
#
# Over NFS (sec=sys) the principal is the local uid. Run it as the UNIX user appsvc, e.g.
# `setpriv --reuid=10001 --regid=10001 --clear-groups inventory.sh --mount /mnt/appdata`: root is
# squashed to the anonymous user by the export (superuser none), which has no Windows mapping, so
# the NTFS ACL denies it.
#
set -euo pipefail

MOUNT=""
OUT="-"

usage() { echo "usage: inventory.sh --mount <dir> [--out <file>|-]" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --mount) MOUNT="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "inventory: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [ -z "$MOUNT" ] || [ ! -d "$MOUNT/seed" ]; then
  echo "inventory: --mount must point at a directory containing seed/" >&2
  exit 2
fi

sha_cmd() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

seed_root="$MOUNT/seed"
# List first and check the status: a process substitution's exit status is never checked, so a
# find that failed part-way (an unreadable directory, an NFS error) would yield a short inventory
# that reads as complete. Deterministic order so two inventories compare cleanly.
if ! FILES="$(find "$seed_root" -type f | LC_ALL=C sort)"; then
  echo "inventory: listing $seed_root failed; no inventory written" >&2
  exit 1
fi
emit() {
  echo '{'
  echo '  "store": {"kind": "nfs", "root": "'"$MOUNT"'"},'
  echo '  "files": ['
  first=1
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    rel="seed/${file#"$seed_root"/}"
    size="$(wc -c <"$file" | tr -d ' ')"
    sha="$(sha_cmd "$file")"
    if [ "$first" -eq 1 ]; then first=0; else echo '    ,'; fi
    printf '    {"path": "%s", "size": %s, "sha256": "%s"}\n' "$rel" "$size" "$sha"
  done <<EOF
$FILES
EOF
  echo '  ]'
  echo '}'
}
# Writing to stdout without reopening /dev/stdout: reopening it fails with EACCES when this runs as
# an unprivileged NFS principal (setpriv --reuid=10001) and stdout is a file root opened (live
# 2026-10-07).
if [ "$OUT" = "-" ]; then
  emit
else
  emit >"$OUT"
fi
echo "inventory: wrote seed inventory to $OUT" >&2
