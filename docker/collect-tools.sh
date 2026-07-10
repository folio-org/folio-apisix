#!/usr/bin/env bash
#
# Collect the given dynamically-linked tools and every shared library they need
# into a staging root, so the result can be COPY'd into the hardened runtime
# image (which ships no package manager and cannot install them itself).
#
# Handles two Debian details that would otherwise break a plain copy:
#   * usr-merge: /lib, /lib64, /bin, /sbin are symlinks to /usr/* in the runtime
#     image, so destinations are normalized under /usr to avoid clobbering them.
#   * SONAME symlinks: the loader looks up e.g. libcurl.so.4 (a symlink to
#     libcurl.so.4.x), so the whole symlink chain plus the real file is staged.
#
# Usage: collect-tools.sh <tool> [<tool> ...]   (STAGING overrides /staging)
set -euo pipefail

STAGING="${STAGING:-/staging}"

# Map a source path to its staging destination, normalizing usr-merge symlinks.
dest() {
  case "$1" in
    /lib/*)   printf '%s/usr/lib/%s'   "${STAGING}" "${1#/lib/}"   ;;
    /lib64/*) printf '%s/usr/lib64/%s' "${STAGING}" "${1#/lib64/}" ;;
    /bin/*)   printf '%s/usr/bin/%s'   "${STAGING}" "${1#/bin/}"   ;;
    /sbin/*)  printf '%s/usr/sbin/%s'  "${STAGING}" "${1#/sbin/}"  ;;
    *)        printf '%s%s'            "${STAGING}" "$1"           ;;
  esac
}

# Stage a file, following and preserving its symlink chain down to the real file.
stage() {
  local f="$1" d t
  while [ -L "$f" ]; do
    d="$(dest "$f")"; mkdir -p "$(dirname "$d")"; cp -Pv "$f" "$d"
    t="$(readlink "$f")"
    case "$t" in /*) f="$t" ;; *) f="$(dirname "$f")/$t" ;; esac
  done
  d="$(dest "$f")"; mkdir -p "$(dirname "$d")"; cp -v "$f" "$d"
}

for tool in "$@"; do
  bin="$(command -v "$tool")" || { echo "tool not found: ${tool}" >&2; exit 1; }
  stage "${bin}"
  ldd "${bin}" | awk '/=>/ {print $3}' | while read -r lib; do
    [ -n "${lib}" ] && [ -e "${lib}" ] && stage "${lib}" || true
  done
done
