#!/bin/sh
# The bubblewrap the isolated tests run: the first bwrap on PATH must be
# bubblewrap 0.12 or later (GHSA-pxhw-h44j-8pfx is fixed there) and neither
# setuid nor setgid. Prints its version, path and sha256, and adds them to
# the job summary when there is one.
set -eu
umask 077
[ "$#" = 0 ] || { echo 'usage: bubblewrap_floor.sh' >&2; exit 2; }
path=$(command -v bwrap) || { echo '::error::bubblewrap (bwrap) is not on PATH' >&2; exit 1; }
if [ -u "$path" ] || [ -g "$path" ]; then
  echo "::error::$path is setuid or setgid; the tests run bubblewrap unprivileged" >&2
  exit 1
fi
version=$("$path" --version) || { echo "::error::$path --version failed" >&2; exit 1; }
number=${version#bubblewrap }
major=${number%%.*}
rest=${number#*.}
minor=${rest%%.*}
case "$version:$major:$minor" in
  "bubblewrap "*:[0-9]*:[0-9]*) ;;
  *) echo "::error::$path does not report a bubblewrap version: $version" >&2; exit 1 ;;
esac
case "$major$minor" in
  *[!0-9]*) echo "::error::$path does not report a bubblewrap version: $version" >&2; exit 1 ;;
esac
if [ "$major" -eq 0 ] && [ "$minor" -lt 12 ]; then
  echo "::error::$version at $path: the isolated tests need bubblewrap 0.12 or later" >&2
  exit 1
fi
digest=$(sha256sum "$path")
line="bubblewrap: $version ($path, sha256 ${digest%% *})"
echo "$line"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf '%s\n' "$line" >>"$GITHUB_STEP_SUMMARY"
fi
