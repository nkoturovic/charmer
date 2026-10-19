#!/bin/sh
# CI-only Linux x86_64 tools. Go is the gateway's own compiler pin (the
# linux-amd64 archive and sha256 in gateway/UPSTREAM.json), so a toolchain
# update reaches the scanners built with it; ShellCheck and bubblewrap are
# pinned here. Verify the publisher's archive digest before extracting
# anything. Install into a new runner-temporary directory, never the
# runner's shared tools.
set -eu
umask 077
[ "$#" = 2 ] || { echo 'usage: ci_tools.sh go|shellcheck|bubblewrap DESTINATION' >&2; exit 2; }
tool=$1
destination=$2
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
case "$tool" in
  go)
    pin=$(python3 -I -c '
import json, re, sys
toolchain = json.load(open(sys.argv[1], encoding="utf-8"))["toolchain"]
version, archive = toolchain["version"], toolchain["archives"]["linux-amd64"]
if not (re.fullmatch(r"1\.[0-9]+\.[0-9]+", version) and archive["file"] == f"go{version}.linux-amd64.tar.gz"
        and re.fullmatch(r"[0-9a-f]{64}", archive["sha256"]) and toolchain["url_prefix"] == "https://go.dev/dl/"):
    sys.exit("gateway/UPSTREAM.json names no pinned linux-amd64 Go toolchain")
print(version, toolchain["url_prefix"] + archive["file"], archive["sha256"])
' "$here/../../gateway/UPSTREAM.json")
    version=${pin%% *}
    rest=${pin#* }
    url=${rest%% *}
    sha256=${rest#* }
    ;;
  shellcheck)
    version=0.11.0
    url="https://github.com/koalaman/shellcheck/releases/download/v${version}/shellcheck-v${version}.linux.x86_64.tar.xz"
    sha256=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198
    ;;
  bubblewrap)
    version=0.13.1
    url="https://github.com/containers/bubblewrap/releases/download/v${version}/bubblewrap-${version}.tar.xz"
    sha256=0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d
    ;;
  *) echo 'unknown CI tool' >&2; exit 2 ;;
esac
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || { echo 'CI tools require Linux x86_64' >&2; exit 1; }
mkdir "$destination"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$url" -o "$work/archive"
printf '%s  %s\n' "$sha256" "$work/archive" | sha256sum --check --status
case "$tool" in
  go) tar -xf "$work/archive" --strip-components=1 --no-same-owner -C "$destination"
    GOTOOLCHAIN=local "$destination/bin/go" version ;;
  shellcheck) tar -xf "$work/archive" --strip-components=1 --no-same-owner -C "$destination"
    "$destination/shellcheck" --version ;;
  bubblewrap)
    # The source release, built unprivileged (0.12 removed setuid builds)
    # without SELinux, man pages, completions or its own tests; only the
    # bwrap executable is kept.
    mkdir "$work/source"
    tar -xf "$work/archive" --strip-components=1 --no-same-owner -C "$work/source"
    meson setup --buildtype=release -Dman=disabled -Dtests=false -Dselinux=disabled \
      -Dbash_completion=disabled -Dzsh_completion=disabled "$work/build" "$work/source" >"$work/setup.log" ||
      { cat "$work/setup.log" >&2; exit 1; }
    ninja -C "$work/build" bwrap >"$work/build.log" || { cat "$work/build.log" >&2; exit 1; }
    cp "$work/build/bwrap" "$destination/bwrap"
    chmod 0755 "$destination/bwrap"
    "$destination/bwrap" --version ;;
esac
