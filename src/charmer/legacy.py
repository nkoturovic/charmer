"""The names this program had before it was charmer.

charmer was released as ``claude-multi`` up to 1.1.0. Normal operation uses
only the current names and never verifies a release under the earlier name.
Compatibility paths recognise old records and session markers, and reverse
migration writes the earlier names back. This module is the one place in
the package that spells them (the repository's name census holds every other
file to the new names), and each constant says who reads it and how long it stays.

Standard library only, and nothing from charmer: a leaf any module (and the
test runner, which reads ``ENVIRONMENT`` without importing it) can use.

Most of it serves ``charmer migrate from-legacy`` (:mod:`charmer.legacy_migration`),
which moves an installation of the old name onto charmer's layout once, and
doctor, which reports what such an installation left behind. Those
constants stay while a machine can still hold an installation of the old
name: until no supported upgrade path starts from one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# The earlier product name and the prefix of its environment variables: the
# name its commands, roots, units, shims and markers were spelled with. The
# tests translate the historical reference gateway unit with them; the name
# census allows the old spellings nowhere else in the package. Stay while
# anything recorded under the old name can reach charmer.
NAME = "claude-multi"
ENVIRONMENT_PREFIX = "CLAUDE_MULTI_"
# Every spelling of the earlier name: the command and path form, the package
# form, the environment form and the class-name form (the census proves
# NAME_PATTERN finds each).
SPELLINGS = ("claude-multi", "claude_multi", "CLAUDE_MULTI", "ClaudeMulti")
# Every form of the earlier name the repository's name census and the
# documentation rule against the former name search for, in any case: its
# two words, and the two half-renamed pairs a careless replace leaves
# (charmer and multi, claude and charmer), each joined by one bounded
# separator: nothing, up to three spaces or tabs, a hyphen or underscore
# with up to two on each side, or one line break (the next line may start
# with a comment or quote marker); never a longer run of white space, which
# would join words of unrelated sentences. A regular expression for text or
# (encoded) bytes.
_SEPARATOR = (r"(?:[ \t]{0,3}|[ \t]{0,2}[_-][ \t]{0,2}"
              r"|[ \t]{0,2}[_-]?[ \t]{0,2}\r?\n[ \t]*(?:(?:#|//|\*|>|--|;)[ \t]*)?)")
NAME_PATTERN = rf"(?i)claude{_SEPARATOR}multi|charmer{_SEPARATOR}multi|claude{_SEPARATOR}charmer"

# The prefix of the gateway selectors up to catalog 38; catalog 39 serves
# each of them as ``charmer-<rest>``. The catalog checks every renamed
# selector against it (``retired.json`` ``renamed``), the fixture catalog
# recipe rewrites its frozen catalog-32 input with it, and doctor still
# recognises a saved or served value under it. Stays while a shipped catalog
# carries renamed selectors for recorded sessions.
LEGACY_SELECTOR_PREFIX = "claude-multi-"

# The variables the old commands exported into a session or set in their
# wrappers. Normal routing does not use them; compatibility consent guards
# recognise the old managed-session variable, and reverse migration translates
# session environments. The test runner clears them so an old session's shell
# never leaks them into a run. Stay while old installations can start sessions.
ENVIRONMENT = (
    "CLAUDE_MULTI_ASSETS",
    "CLAUDE_MULTI_CHANNEL",
    "CLAUDE_MULTI_GATEWAY",
    "CLAUDE_MULTI_HOOK_COMMAND",
    "CLAUDE_MULTI_INHIBITION_TOKEN",
    "CLAUDE_MULTI_LAUNCH_EPOCH",
    "CLAUDE_MULTI_MANAGED_ID",
    "CLAUDE_MULTI_PROXY_BIN",
    "CLAUDE_MULTI_PROXY_PATCHES",
    "CLAUDE_MULTI_REGISTRY_DIR",
    "CLAUDE_MULTI_SECRET_ENV",
    "CLAUDE_MULTI_SESSION_ID",
)

# The /cm skill files the compiler wrote before the rename, oldest first;
# the last is the one 1.1.0 compiled. A live scope that still carries one
# differs only by the skill text, which doctor reports as lazy state (it
# applies at the next resume or repair), and a rollback repair restores it
# (``charmer.scope.PREVIOUS_SKILL_MD_BYTES``). Stays while scopes compiled
# under the old name can still be resumed.
SKILL_MD_BYTES = (
    (
        "---\n"
        "name: cm\n"
        "description: Show or change this claude-multi session's agent lineup "
        "(/cm, /cm set AGENT=MODEL[:EFFORT], /cm unset AGENT, /cm profile NAME, "
        "/cm direct, /cm pin, /cm follow)\n"
        "disable-model-invocation: true\n"
        "allowed-tools: Bash(claude-multi lineup:*)\n"
        "---\n"
        "!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`\n"
    ).encode("utf-8"),
    (
        "---\n"
        "name: cm\n"
        "description: Show or change this claude-multi session's agent lineup "
        "(/cm, /cm profiles, /cm profile NAME, /cm set AGENT=MODEL[:EFFORT], /cm unset AGENT, "
        "/cm direct, /cm pin, /cm follow, /cm fallback PROVIDER, /cm review, /cm quota)\n"
        'argument-hint: "profile NAME · set AGENT=MODEL[:EFFORT] · unset AGENT · direct · pin · '
        'follow · profiles · fallback PROVIDER · review [high-stakes] [RANGE] · quota"\n'
        "disable-model-invocation: true\n"
        "allowed-tools: Bash(claude-multi lineup:*)\n"
        "---\n"
        "!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`\n"
    ).encode("utf-8"),
    (
        "---\n"
        "name: cm\n"
        "description: Show or change this claude-multi session's agent lineup "
        "(/cm, /cm profiles, /cm profile NAME, /cm set AGENT=MODEL[:EFFORT], /cm unset AGENT, "
        "/cm direct [MODEL[:EFFORT]], /cm pin, /cm follow, /cm fallback PROVIDER [--preview], "
        "/cm review [high-stakes] [RANGE], /cm quota)\n"
        'argument-hint: "profiles · profile NAME · set AGENT=MODEL[:EFFORT] · unset AGENT · '
        'direct [MODEL[:EFFORT]] · pin · follow · fallback PROVIDER [--preview] · '
        'review [high-stakes] [RANGE] · quota"\n'
        "disable-model-invocation: true\n"
        "allowed-tools: Bash(claude-multi lineup:*)\n"
        "---\n"
        "!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`\n"
    ).encode("utf-8"),
)


# ------------------------------------------------------------ the old layout
#
# Everything below names what an installation of the old name wrote, for
# ``charmer migrate from-legacy`` (which moves it onto charmer's layout and
# can move it back) and for doctor (which reports what is left of it). They
# stay while a machine can still hold such an installation.

# The directory name of every old root: the state and configuration roots
# (XDG-aware), the gateway configuration directory and the data root
# (HOME-relative), resolved exactly as charmer.paths resolves charmer's own.
LEGACY_DIR = NAME


def _home(environ: Mapping[str, str]) -> Path:
    return Path(environ["HOME"]) if environ.get("HOME") else Path.home()


def _xdg(environ: Mapping[str, str], variable: str, fallback: str) -> Path:
    value = environ.get(variable)
    return Path(value) if value and os.path.isabs(value) else _home(environ) / fallback


def state_root(environ: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME/claude-multi`` or ``~/.local/state/claude-multi``
    (a relative ``XDG_STATE_HOME`` is ignored, as charmer.paths does)."""

    return _xdg(environ, "XDG_STATE_HOME", ".local/state") / LEGACY_DIR


def config_root(environ: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME/claude-multi`` or ``~/.config/claude-multi``."""

    return _xdg(environ, "XDG_CONFIG_HOME", ".config") / LEGACY_DIR


def gateway_config_dir(environ: Mapping[str, str]) -> Path:
    """``~/.config/claude-multi`` whatever ``XDG_CONFIG_HOME`` says: the
    gateway's configuration, keys and continuity set, HOME-relative."""

    return _home(environ) / ".config" / LEGACY_DIR


def data_root(environ: Mapping[str, str]) -> Path:
    """``~/.local/share/claude-multi``: installs, sign-in records, client copies."""

    return _home(environ) / ".local" / "share" / LEGACY_DIR


# The data root's install tree (the retained 1.x bundles, their ``current``
# link and the receipt of the files the old installer wrote): migration
# leaves it for ``--back`` and ``--purge`` removes it.
LEGACY_INSTALL_DIR = "install"

# The hook and token shims the old launcher wrote into ``<state root>/bin``
# (regenerable; migration replaces them with charmer's).
LEGACY_SHIMS = ("claude-multi-hook", "claude-multi-hook-3", "claude-multi-gateway-token")

# The supervised gateway's user units, the exact first line of every unit
# file the old ``gateway service install`` wrote (its ownership mark), and
# the texts inside them. Migration stops and removes a unit only with that
# header; ``--back`` restores the bytes it saved.
LEGACY_UNIT = "claude-multi-gateway"
LEGACY_FAILURE_UNIT = "claude-multi-gateway-failure"
LEGACY_UNIT_HEADER = "# Written by claude-multi gateway service install; run it again to refresh this file."
LEGACY_UNIT_DESCRIPTION = "claude-multi local model gateway"
LEGACY_FAILURE_DESCRIPTION = "claude-multi gateway failure notice"
LEGACY_APP_NAME = "claude-multi"

# The launcher files the old installer wrote into ``~/.local/bin``, the
# second line that marks them as its own, and the marker of the PATH line
# it appended to a shell profile. Migration removes a launcher only with
# that mark.
LEGACY_LAUNCHERS = ("claude-multi", "claude-multi-proxy")
LEGACY_LAUNCHER_MARK = "# claude-multi installer launcher"
LEGACY_PATH_MARK = "# added by the claude-multi installer"

# Set in every session an old launcher started. charmer treats it as "inside
# a managed session" wherever a person must be at a plain terminal.
LEGACY_MANAGED_ID_ENV = "CLAUDE_MULTI_MANAGED_ID"

# An old Nix installation's exec links: the data root's ``nix/current``
# (``gateway service install`` on the Nix channel) and the release link some
# Nix setups kept outside it (HOME-relative). Migration supports the bundle
# channel only and refuses when either exists.
LEGACY_NIX_LINK = Path("nix") / "current"
LEGACY_NIX_RELEASE_LINK = Path(".local") / "share" / "claude-multi-release" / "current"

# The operator export's format identifier before the rename; ``charmer
# import`` still reads an export that carries it.
LEGACY_OPERATOR_EXPORT_FORMAT = "claude-multi-operator-export"

# The old build and test caches (HOME-relative), which ``--purge`` removes.
LEGACY_CACHE_DIRS = (Path(".cache") / "claude-multi-build", Path(".cache") / "claude-multi-test-tmp")

# Where the release of the earlier name kept its packaged resources in the
# source tree (``version.json``, ``catalog/``): ``tools/release.py`` reads a
# previous release's version there when that release predates the rename.
# Stays while a release of the earlier name can be the previous release.
LEGACY_RESOURCES = Path("src") / "claude_multi" / "data"

# The Nix flake attributes the first releases of the earlier name offered;
# the flake must never offer them again (the packaging tests assert it).
LEGACY_NIX_ATTRIBUTES = ("claudeMultiPlugin", "claudeMultiLauncher", "claudeMultiProxy")

# The first part of the earlier gateway's start line in its unit journal
# (``<prefix><instance> starting``); charmer's is ``charmer-proxy: ...``.
# The earlier gateway's persistence hold is evaluated with it before
# ``migrate from-legacy`` stops that gateway.
LEGACY_START_MARKER_PREFIX = "claude-multi-proxy: gateway instance "


def legacy_start_marker(instance: str) -> str:
    """The earlier gateway's start line for ``instance`` (its first part)."""

    return f"{LEGACY_START_MARKER_PREFIX}{instance} starting"
