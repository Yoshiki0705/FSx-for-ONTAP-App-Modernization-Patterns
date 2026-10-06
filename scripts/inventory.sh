#!/usr/bin/env bash
#
# Take a file inventory of seed/ over NFS on the Linux EC2 host (stage 1 and later). Emits the same
# JSON shape as inventory.ps1: for every file under <mount>/seed, the relative path (/-separated),
# size in bytes and SHA-256. Read-only.
#
#   inventory.sh --mount /mnt/appdata --out /tmp/linux-inventory.json
#
set -euo pipefail

MOUNT=""
OUT="/dev/stdout"

usage() { echo "usage: inventory.sh --mount <dir> [--out <file>]" >&2; }

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
{
  echo '{'
  echo '  "store": {"kind": "nfs", "root": "'"$MOUNT"'"},'
  echo '  "files": ['
  first=1
  # Deterministic order so two inventories compare cleanly.
  while IFS= read -r file; do
    rel="seed/${file#"$seed_root"/}"
    size="$(wc -c <"$file" | tr -d ' ')"
    sha="$(sha_cmd "$file")"
    if [ "$first" -eq 1 ]; then first=0; else echo '    ,'; fi
    printf '    {"path": "%s", "size": %s, "sha256": "%s"}\n' "$rel" "$size" "$sha"
  done < <(find "$seed_root" -type f | LC_ALL=C sort)
  echo '  ]'
  echo '}'
} >"$OUT"
echo "inventory: wrote seed inventory to $OUT" >&2
