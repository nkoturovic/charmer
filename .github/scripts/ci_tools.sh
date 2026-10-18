#!/bin/sh
# CI-only Linux x86_64 tools; independent of the gateway's compiler pin.
# Verify the publisher's archive digest before extracting anything. Install
# into a new runner-temporary directory, never the runner's shared tools.
set -eu
umask 077
[ "$#" = 2 ] || { echo 'usage: ci_tools.sh go|shellcheck DESTINATION' >&2; exit 2; }
tool=$1
destination=$2
case "$tool" in
  go)
    version=1.26.9
    url="https://go.dev/dl/go${version}.linux-amd64.tar.gz"
    sha256=42d158b4d8f7b61ac0a830567c940a86098fb7aac52e467a5ebec03ef5cc2f8d
    ;;
  shellcheck)
    version=0.11.0
    url="https://github.com/koalaman/shellcheck/releases/download/v${version}/shellcheck-v${version}.linux.x86_64.tar.xz"
    sha256=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198
    ;;
  *) echo 'unknown CI tool' >&2; exit 2 ;;
esac
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || { echo 'CI tools require Linux x86_64' >&2; exit 1; }
mkdir "$destination"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$url" -o "$work/archive"
printf '%s  %s\n' "$sha256" "$work/archive" | sha256sum --check --status
tar -xf "$work/archive" --strip-components=1 --no-same-owner -C "$destination"
case "$tool" in
  go) GOTOOLCHAIN=local "$destination/bin/go" version ;;
  shellcheck) "$destination/shellcheck" --version ;;
esac
