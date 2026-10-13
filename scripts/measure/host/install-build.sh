#!/bin/bash
#
# Assemble a shipped build under a NEW directory on the Linux client and verify it.
# Runs on the host (sent through ship_build.py -> ssm_run.py). It never touches an existing
# directory: the destination is created with mkdir (not mkdir -p) and an existing one is refused.
#
#   APPMOD_RUN_ID          run id; names the payload file /tmp/appmod-ship-<run-id>-payload.b64
#   APPMOD_PAYLOAD_SHA256  expected sha256 of the decoded tar.gz payload
#   APPMOD_DEST_DIR        NEW directory under /opt/appmod/ (its parent must exist)
#   APPMOD_COMPONENTS      comma-separated component names; each becomes <dest>/<name>
#   APPMOD_BASES           optional name=hostdir,... ; hostdir is copied to <dest>/<name> first
#   APPMOD_EXECUTABLES     optional comma-separated paths under <dest> that get chmod a+x
#   APPMOD_PRUNE_EXTRA     optional; 1 removes files inside <dest> that are not in the list
#
# The payload holds <name>/<changed files> and <name>.sha256 (full list, "hash  ./path").
# After assembly every component is checked with sha256sum -c, and the files found are compared
# with the files listed (extra and missing), and ._* (AppleDouble) files are counted.
#
# Exit codes: 0 verified; 20 payload sha256 mismatch; 21 destination exists; 22 bad argument or
# missing input; 23 verification failed.
#
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  echo "usage: install-build.sh [-h|--help]   (input comes from APPMOD_* variables; no arguments)"
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

die() { local code="$1"; shift; echo "install-build: $*" >&2; exit "$code"; }
[ "$#" -eq 0 ] || die 2 "unknown argument: $1"

RUN_ID="${APPMOD_RUN_ID:-}"
EXPECT="${APPMOD_PAYLOAD_SHA256:-}"
DEST="${APPMOD_DEST_DIR:-}"
COMPONENTS="${APPMOD_COMPONENTS:-}"
BASES="${APPMOD_BASES:-}"
EXECUTABLES="${APPMOD_EXECUTABLES:-}"
PRUNE="${APPMOD_PRUNE_EXTRA:-}"

RUN_ID_RE='^s[0-9][a-z]{0,2}-[0-9]{8}T[0-9]{6}Z$'
SHA_RE='^[0-9a-f]{64}$'
DEST_RE='^/opt/appmod/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$'
NAME_RE='^[a-z][a-z0-9-]{0,30}$'
REL_RE='^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$'

[[ $RUN_ID =~ $RUN_ID_RE ]] || die 22 "APPMOD_RUN_ID is missing or malformed"
[[ $EXPECT =~ $SHA_RE ]] || die 22 "APPMOD_PAYLOAD_SHA256 is missing or malformed"
[[ $DEST =~ $DEST_RE ]] || die 22 "APPMOD_DEST_DIR must match /opt/appmod/<name>[/<name>...]"
case "/$DEST/" in */../* | */./*) die 22 "APPMOD_DEST_DIR must not contain . or .. segments" ;; esac
[ -n "$COMPONENTS" ] || die 22 "APPMOD_COMPONENTS is required"

IFS=',' read -r -a COMP_LIST <<<"$COMPONENTS"
for c in "${COMP_LIST[@]}"; do
  [[ $c =~ $NAME_RE ]] || die 22 "bad component name: $c"
done

# base_for NAME prints the host base directory for NAME, or nothing.
base_for() {
  local pair
  [ -n "$BASES" ] || return 0
  IFS=',' read -r -a pairs <<<"$BASES"
  for pair in "${pairs[@]}"; do
    if [ "${pair%%=*}" = "$1" ]; then printf '%s' "${pair#*=}"; return 0; fi
  done
}

if [ -n "$BASES" ]; then
  IFS=',' read -r -a PAIR_LIST <<<"$BASES"
  for pair in "${PAIR_LIST[@]}"; do
    b="${pair#*=}"
    [[ $b =~ $DEST_RE ]] || die 22 "bad base directory: $b"
    case "/$b/" in */../* | */./*) die 22 "base directory must not contain . or ..: $b" ;; esac
  done
fi

if [ -n "$EXECUTABLES" ]; then
  IFS=',' read -r -a EXE_LIST <<<"$EXECUTABLES"
  for e in "${EXE_LIST[@]}"; do
    [[ $e =~ $REL_RE ]] || die 22 "bad executable path: $e"
    case "/$e/" in */../* | */./*) die 22 "executable path must not contain . or ..: $e" ;; esac
  done
fi

B64="/tmp/appmod-ship-$RUN_ID-payload.b64"
TGZ="/tmp/appmod-ship-$RUN_ID-payload.tgz"
XDIR="/tmp/appmod-ship-$RUN_ID-x"
WORK="/tmp/appmod-ship-$RUN_ID-work"
# The payload files are kept on failure so a corrected retry does not re-upload them.
trap 'rm -rf "$XDIR" "$WORK"' EXIT

[ -f "$B64" ] || die 22 "payload not found: $B64"
base64 -d "$B64" >"$TGZ"
GOT="$(sha256sum "$TGZ" | cut -d' ' -f1)"
echo "payload-sha256=$GOT"
[ "$GOT" = "$EXPECT" ] || die 20 "payload sha256 mismatch"

if [ -e "$DEST" ]; then die 21 "$DEST already exists; refusing to touch it"; fi

mkdir "$XDIR" "$WORK"
tar -xzf "$TGZ" -C "$XDIR" --no-same-owner

mkdir "$DEST"
for c in "${COMP_LIST[@]}"; do
  base="$(base_for "$c")"
  if [ -n "$base" ]; then
    [ -d "$base" ] || die 22 "base directory not found: $base"
    cp -a -- "$base" "$DEST/$c"
  else
    mkdir "$DEST/$c"
  fi
  [ -d "$XDIR/$c" ] || die 22 "payload has no directory for component $c"
  cp -a -- "$XDIR/$c/." "$DEST/$c/"
  [ -f "$XDIR/$c.sha256" ] || die 22 "payload has no $c.sha256"
  cp -p -- "$XDIR/$c.sha256" "$DEST/$c.sha256"
done

chown -R root:root "$DEST"
chmod -R a+rX "$DEST"
if [ -n "$EXECUTABLES" ]; then
  for e in "${EXE_LIST[@]}"; do
    [ -f "$DEST/$e" ] || die 22 "executable not found in the new directory: $e"
    chmod a+x "$DEST/$e"
  done
fi

export LC_ALL=C
# list_found NAME / list_listed NAME write sorted "./path" lines for the comparison.
list_found() { (cd "$DEST/$1" && find . -type f | sort); }
list_listed() { sed 's/^[0-9a-f]*  //' "$DEST/$1.sha256" | sort; }

if [ "$PRUNE" = "1" ]; then
  for c in "${COMP_LIST[@]}"; do
    list_found "$c" >"$WORK/found-$c.txt"
    list_listed "$c" >"$WORK/listed-$c.txt"
    comm -13 "$WORK/listed-$c.txt" "$WORK/found-$c.txt" >"$WORK/extra-$c.txt"
    while IFS= read -r path; do
      case "$path" in ./*) ;; *) continue ;; esac
      case "/$path/" in */../*) continue ;; esac
      # Only regular files inside the directory this run created.
      if [ -f "$DEST/$c/$path" ] && [ ! -L "$DEST/$c/$path" ]; then
        rm -f -- "$DEST/$c/$path"
        echo "pruned $c/$path"
      fi
    done <"$WORK/extra-$c.txt"
  done
fi

echo "---verify"
FAILED=0
for c in "${COMP_LIST[@]}"; do
  rc=0
  (cd "$DEST/$c" && sha256sum -c --quiet "../$c.sha256" >"$WORK/verify-$c.txt" 2>&1) || rc=$?
  files="$(list_found "$c" | wc -l)"
  listed="$(wc -l <"$DEST/$c.sha256")"
  list_found "$c" >"$WORK/found-$c.txt"
  list_listed "$c" >"$WORK/listed-$c.txt"
  extra="$(comm -13 "$WORK/listed-$c.txt" "$WORK/found-$c.txt" | wc -l)"
  missing="$(comm -23 "$WORK/listed-$c.txt" "$WORK/found-$c.txt" | wc -l)"
  echo "$c sha256-c rc=$rc files=$files listed=$listed extra-files: $extra missing-files: $missing"
  if [ "$rc" -ne 0 ]; then head -n 5 "$WORK/verify-$c.txt" >&2; FAILED=1; fi
  if [ "$extra" -ne 0 ] || [ "$missing" -ne 0 ]; then FAILED=1; fi
done
apple="$(find "$DEST" -name '._*' | wc -l)"
echo "appledouble: $apple"
if [ "$apple" -ne 0 ]; then FAILED=1; fi
if [ -n "$EXECUTABLES" ]; then
  for e in "${EXE_LIST[@]}"; do ls -l "$DEST/$e"; done
fi
[ "$FAILED" -eq 0 ] || die 23 "verification failed (see the lines above)"
rm -f "$B64" "$TGZ"
echo "install-build: ok $DEST"
