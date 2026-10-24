#!/usr/bin/env python3
"""The pinned build inputs, read strictly, compared with upstream versions.

  python3 tools/_build/pin_watch.py pins [--repo DIR]
  python3 tools/_build/pin_watch.py compare --upstream FILE [--repo DIR]

``pins`` prints the pins as JSON: Claude Code (the packaged native
contract's verified version), the gateway (``gateway/UPSTREAM.json``
``upstream.version``), Go (its ``toolchain.version``), CPython and the
python-build-standalone release (``packaging/python-runtimes.json``). A pin
that is absent, empty or not a version refuses (exit 1): a watch that cannot
read its pins must not look like a quiet week.

``compare`` reads ``name=value`` lines of upstream versions (as the weekly
workflow collects them) and prints a Markdown table of the inputs whose
upstream is newer. Claude Code needs both ``claude_code_latest`` and
``claude_code_stable``: both are reported, and a pin behind latest while
latest differs from stable exits 1. Missing or malformed upstream values
also fail. No stdout means nothing newer; the tag report is on stderr.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

REPO = Path(__file__).resolve().parents[2]
NAMES = ("claude_code", "gateway", "go", "python", "python_build_standalone")
_VERSION = re.compile(r"[0-9]+(\.[0-9]+){1,2}")
_RELEASE = re.compile(r"[0-9]{8}")


class PinError(Exception):
    pass


def _load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PinError(f"{path} cannot be read ({exc})") from exc


def _field(document: object, keys: Sequence[str], where: str) -> object:
    node = document
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise PinError(f"{where}: {'.'.join(keys)} is missing")
        node = node[key]
    return node


def _version(value: object, what: str, pattern: re.Pattern[str] = _VERSION) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PinError(f"{what} is not a version: {value!r}")
    return value


def pins(repo: Path = REPO) -> dict[str, str]:
    contract_path = repo / "src/charmer/data/catalog/native-contract.json"
    contract = _load(contract_path)
    verified = _field(contract, ("verified",), str(contract_path))
    if not isinstance(verified, list) or not verified or not isinstance(verified[0], dict):
        raise PinError(f"{contract_path}: verified names no Claude Code version")
    upstream_path = repo / "gateway/UPSTREAM.json"
    upstream = _load(upstream_path)
    runtimes_path = repo / "packaging/python-runtimes.json"
    runtimes = _load(runtimes_path)
    return {
        "claude_code": _version(verified[0].get("version"), "the Claude Code pin"),
        "gateway": _version(_field(upstream, ("upstream", "version"), str(upstream_path)), "the gateway pin"),
        "go": _version(_field(upstream, ("toolchain", "version"), str(upstream_path)), "the Go pin"),
        "python": _version(_field(runtimes, ("python",), str(runtimes_path)), "the CPython pin"),
        "python_build_standalone": _version(_field(runtimes, ("release",), str(runtimes_path)),
                                            "the python-build-standalone release", _RELEASE),
    }


def _key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def normalize(name: str, raw: str) -> str:
    value = raw.strip()
    if name in ("gateway", "claude_code_latest", "claude_code_stable"):
        value = value.removeprefix("v")
    if name == "go":
        value = value.removeprefix("go")
    pattern = _RELEASE if name == "python_build_standalone" else _VERSION
    return _version(value, f"the upstream {name} version", pattern)


def upstream_versions(text: str) -> dict[str, str]:
    upstream = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    versions = {}
    for name in ("claude_code_latest", "claude_code_stable", "gateway", "go", "python_build_standalone"):
        if name not in upstream or not upstream[name].strip():
            raise PinError(f"the upstream {name} version is missing (the lookup failed)")
        versions[name] = normalize(name, upstream[name])
    return versions


def compare(pinned: dict[str, str], upstream: dict[str, str]) -> list[tuple[str, str, str]]:
    newer = []
    for name, latest in upstream.items():
        pin_name = "claude_code" if name in ("claude_code_latest", "claude_code_stable") else name
        if _key(latest) > _key(pinned[pin_name]):
            newer.append((name, pinned[pin_name], latest))
    return newer


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pin_watch.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("command", choices=("pins", "compare"))
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--upstream", type=Path, help="compare: the name=value lines")
    args = parser.parse_args(argv)
    try:
        if args.command == "pins":
            print(json.dumps(pins(args.repo), indent=2, sort_keys=True))
            return 0
        if args.upstream is None:
            parser.error("compare needs --upstream FILE")
        pinned = pins(args.repo)
        upstream = upstream_versions(args.upstream.read_text(encoding="utf-8"))
        rows = compare(pinned, upstream)
    except (PinError, OSError) as exc:
        print(f"pin-watch: {exc}", file=sys.stderr)
        return 1
    latest, stable = upstream["claude_code_latest"], upstream["claude_code_stable"]
    tags = f"Claude Code: pinned={pinned['claude_code']}; latest={latest}; stable={stable}."
    print(tags, file=sys.stderr)
    uncovered = _key(latest) > _key(pinned["claude_code"]) and latest != stable
    if rows:
        print("Newer upstream versions than the pins (re-pin through the documented release steps):\n")
        print(tags + "\n")
        print("| input | pinned | upstream |\n|---|---|---|")
        for name, version, newest in rows:
            print(f"| {name} | {version} | {newest} |")
    if uncovered:
        print("\nReview required: the Claude Code pin is behind latest, and latest is not stable.")
    return 1 if uncovered else 0


if __name__ == "__main__":
    raise SystemExit(main())
