"""``charmer migrate from-legacy``: move an installation of the earlier name onto charmer.

charmer always runs on its own layout. An account that ran the release of the
earlier name (``charmer.legacy``) keeps everything it had — session records,
compiled scopes, profiles, provider keys, sign-in records, Claude Code copies,
the supervised gateway — through one operator-run transaction while nothing
is live, and it can be moved back until the retained earlier installation is
purged:

    charmer migrate from-legacy [--dry-run | --status | --back | --purge]

The transaction is journaled in ``<state root>/migration-from-legacy/``
(``journal.json`` plus the bytes it saved for ``--back``). The journal starts
in the earlier state root and moves with it, so it is always beside the state
it describes. Its steps, each idempotent (a rerun converges, and ``--back``
reverses whatever was done, from any step):

1. ``units``: the earlier gateway's persistence hold is evaluated with the
   earlier unit and start line (never recorded, never cleared); the earlier
   gateway unit and its failure unit (only with the exact earlier header)
   are saved byte for byte, then disabled and stopped, and the stop's result
   and the earlier exec stamp are kept in the journal;
2. ``roots``: ``rename(2)`` of the configuration root, the gateway
   configuration directory (when it is another directory), every data-root
   child but ``install/`` and ``nix/`` and, last, the state root (an empty
   skeleton charmer's installer left at a destination is removed first;
   anything more is a second root, never merged; a move across file systems
   is refused, never copied). Every file moves as it is: keys, sign-in
   records, rotation and lifecycle files are never opened;
3. ``authorities``: the few values that name a moved root or the earlier
   unit are rewritten in place, each file otherwise byte for byte
   (``continuity.json`` ``state_root``, ``secret-file.json`` ``path`` when
   its target moved, ``endpoint.json`` ``unit`` when it is the earlier
   default), and charmer's installer receipt takes over the earlier
   installer's PATH lines that are still present (the earlier receipt stays
   with the earlier install tree);
4. ``shims``: the earlier hook and token shims give way to charmer's
   (compiled scopes are recompiled at their next resume);
5. ``gateway``: the supervised gateway is installed as ``charmer-gateway``,
   enabled and started through its own preparation (``init
   --prepare-start`` renders the configuration and its receipt again), and
   proven ready on the recorded endpoint;
6. ``remnants``: the earlier launchers in ``~/.local/bin`` (only with the
   earlier installer's mark) and unit files (only with the earlier header)
   are removed; the earlier install tree stays for ``--back`` and
   ``--purge``;
7. ``verify``: ``gateway ensure`` on loopback and a doctor pass without a
   BLOCK; the journal is then marked done and kept as evidence.

``--back`` undoes the steps in the other order, then hands the sessions
back (:func:`_restore_sessions`): every session record charmer wrote names
the earlier selectors again (``retired.json`` ``renamed`` read backwards,
its ``applied_hash`` with them), and the launch-time items of every scope
charmer compiled (which the record's launch fence proves, so the earlier
repair keeps them) are written in the earlier release's own form, the fence
following them. Then, when a scope still carries a charmer name, the
retained earlier launcher's own ``doctor --repair-all --include-live`` runs
once and recompiles the rest in its own format (:func:`_repair_earlier`).
A session first launched under charmer stays, and the earlier release
resumes it like its own.

Guards. Both install locks are the one transaction lock of the move,
``--back`` and ``--purge``: each takes it first, reads the journal under it
and holds it through its last write (another of them refuses meanwhile).
The journal is written before anything else, the inhibition included. Steps 1-2
run under the guards of the roots where they are now: the gateway
inhibition (owner ``cutover``), the session-store migration lock, the
served-change barrier and every owned Claude Code copy's use lock (a rename
keeps the inode, so each lock stays on the file it took). Before step 3
those guards are released and fresh ones are taken on charmer's paths (the
inhibition handed over under the gateway's start lock, so no start slips
in between); an earlier root's token never reaches a writer of charmer's.
While a journal is unfinished, every other charmer command refuses
(:func:`pending`), but for the gateway commands that name and clear a
persistence hold, from when the move's roots are in place until ``--back``
changes any of them (:func:`runs_while_pending`).

Effects outside the filesystem pass through :class:`Seams`, which the command
fills with the live implementations and tests with fakes.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from charmer import (assets, catalog, continuity, endpoint, errors, gateway_inhibition, install_receipt, installs,
                     legacy, operator, paths, scope, secret_store, self_update, service, sessions, state,
                     strict_json)
from charmer.platform import linux_service, posix_fs, systemd_unit

COMMAND = "charmer migrate from-legacy"
DRY_RUN = f"{COMMAND} --dry-run"
STATUS = f"{COMMAND} --status"
BACK = f"{COMMAND} --back"
PURGE = f"{COMMAND} --purge"

JOURNAL_DIR = "migration-from-legacy"
JOURNAL_NAME = "journal.json"
SAVED_DIR = "saved"
VERSION = 1
OWNER = "cutover"

RUNNING = "running"
DONE = "done"
REVERTING = "reverting"
REVERTED = "reverted"
STATUSES = (RUNNING, DONE, REVERTING, REVERTED)
UNFINISHED = (RUNNING, REVERTING)
# The journal's mark that --back began to change charmer's units, values or
# roots (until then a REVERTING journal still has charmer's installation in
# place, its gateway stopped or about to be).
UNWOUND = "unwound"

UNITS, ROOTS, AUTHORITIES, SHIMS, GATEWAY, REMNANTS, VERIFY = (
    "units", "roots", "authorities", "shims", "gateway", "remnants", "verify")
STEPS = (UNITS, ROOTS, AUTHORITIES, SHIMS, GATEWAY, REMNANTS, VERIFY)
STEP_TEXT = {
    UNITS: "stopped and disabled the earlier gateway units",
    ROOTS: "moved the roots",
    AUTHORITIES: "rewrote the values that name a moved root or the earlier unit",
    SHIMS: "replaced the hook and token shims",
    GATEWAY: "the gateway: charmer-gateway, or on demand as before",
    REMNANTS: "removed the earlier launchers and unit files",
    VERIFY: "verified (gateway ensure, doctor)",
}
# The gateway step as its service decision made it (``journal["service"]``).
GATEWAY_TEXT = {True: "started the gateway as charmer-gateway",
                False: "kept the gateway starting on demand, as before (no service installed)"}

CHARMER_SHIMS = tuple(Path(relative).name for relative in (
    scope.HOOK_SHIM_RELATIVE, scope.HOOK_SHIM_V3_RELATIVE, scope.GATEWAY_TOKEN_SHIM_RELATIVE))
CHARMER_UNIT = endpoint.DEFAULT_UNIT
SECRET_POINTER = "secret-file.json"
# The gateway configuration files step 3 may rewrite one value of.
AUTHORITY_FILES = (continuity.FILE_NAME, SECRET_POINTER, endpoint.ENDPOINT_FILE)
RECEIPT_SAVED = "installer.json"
# Data-root children that stay with the earlier installation: its install
# tree (kept for --back, removed by --purge) and its Nix selection (history).
KEPT_DATA = (legacy.LEGACY_INSTALL_DIR, legacy.LEGACY_NIX_LINK.parts[0])
GATEWAY_ENV = ".env"
DRAFTS = "drafts"
# The retained earlier launcher (in its install tree) and the one command
# ``--back`` runs with it: the earlier release recompiles, in its own
# format, the session scopes charmer compiled. Nothing runs then (``--back``
# checked), so the sessions without a recorded end are included.
EARLIER_LAUNCHER = Path("current") / "bin" / legacy.NAME
REPAIR_ARGS = ("doctor", "--repair-all", "--include-live")
REPAIR = f"{legacy.NAME} {' '.join(REPAIR_ARGS)}"
# Where charmer's scope compile spells its own name in a launch-time item
# (``scope.compile_lineup_scope``): the lifecycle ``env`` keys and the model
# picker's descriptions (``charmer · <line key>``); the earlier release
# spelled its name there (``--back`` writes them in its form).
CHARMER_ENV_PREFIX = "CHARMER_"
PICKER_PREFIX = "charmer · "
EARLIER_PICKER_PREFIX = f"{legacy.NAME} · "
LOCK_WAIT = 10.0
ACTIVE_STATES = ("active", "activating", "reloading", "deactivating")
_TEMPORARY = re.compile(r"^\..+\.[^.]+\.tmp$")
# A JSON object member whose value is a string: ``"key": "value"``.
_STRING = r'"(?:[^"\\\x00-\x1f]|\\.)*"'
_MEMBER = re.compile(rf'(?P<key>{_STRING})\s*:\s*(?P<value>{_STRING})')
# A credential name under charmer's reserved prefix, bare (``custom.json`` ``secret_env``)
# or as a secret reference (``env:NAME``: ``providers.d`` and the operator
# ledger's ``secret_ref``); group 1 is the name.
_RESERVED_NAME = re.compile(r"^(?:env:)?(CHARMER_[A-Z0-9_]*)$")


class MigrationError(errors.CharmerError, RuntimeError):
    """A refusal or a failed step; ``lines`` are every reason, each with its fix."""

    def __init__(self, message: str, *, remedy: str | None = None, lines: Sequence[str] = ()) -> None:
        super().__init__(message, remedy=remedy)
        self.lines = tuple(lines)


class LivenessUnknown(errors.CharmerError, RuntimeError):
    """Whether a session runs cannot be told (an inventory is unreadable)."""


@dataclass
class Seams:
    """The effects outside the filesystem (the live ones from the command,
    fakes in tests):

    - ``installed(environ)``: why this charmer is not running from an
      installed release bundle (None: it is);
    - ``liveness(state_root)``: the managed sessions under ``state_root``
      that run now; raises :class:`LivenessUnknown` when that is unknown;
    - ``hold(layout)``: why the earlier gateway must not be stopped (its
      persistence hold, evaluated over its own unit's journal or instance
      logs with its own start line, never recorded); empty when it may;
    - ``shims(environ)``: write charmer's hook and token shims;
    - ``gateway(environ)``: install, enable and start ``charmer-gateway`` and
      wait until it is ready on the recorded endpoint; ``(ok, message)``;
    - ``verify(environ, since)``: ``gateway ensure``, the proof that the
      gateway's configuration and preparation receipt were rendered again
      since ``since``, and a doctor pass; the BLOCK lines (empty: verified);
    - ``repair(environ)``: the retained earlier launcher's own
      ``doctor --repair-all --include-live`` (:data:`REPAIR`), the one call
      ``--back`` makes to it; ``(ok, its output lines)``;
    - ``charmer_hold(layout)``: why charmer's own gateway must not be
      stopped (its persistence hold over its state root, backend, unit and
      start line; evidence that cannot be read holds), never recorded;
      empty when it may;
    - ``stop_gateway(environ)``: stop charmer's gateway through its own stop
      (the hold again, a stop proven for its backend); ``(ok, message)``;
    - ``runner``: the user service manager's commands (``subprocess.run``'s signature).

    ``environ`` carries the charmer root's inhibition token
    (``CHARMER_INHIBITION_TOKEN``) for ``shims`` and ``gateway``.
    """

    installed: Callable[[Mapping[str, str]], str | None]
    liveness: Callable[[Path], Sequence[str]]
    hold: Callable[["Layout"], Sequence[str]]
    shims: Callable[[Mapping[str, str]], None]
    gateway: Callable[[Mapping[str, str]], tuple[bool, str]]
    verify: Callable[[Mapping[str, str], datetime.datetime], Sequence[str]]
    repair: Callable[[Mapping[str, str]], tuple[bool, Sequence[str]]]
    charmer_hold: Callable[["Layout"], Sequence[str]]
    stop_gateway: Callable[[Mapping[str, str]], tuple[bool, str]]
    runner: Callable[..., Any] = subprocess.run
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc)


# ------------------------------------------------------------ layout

@dataclass(frozen=True)
class Layout:
    """Every path the migration touches, resolved once from ``environ``."""

    environ: Mapping[str, str]
    home: Path
    legacy_state: Path
    legacy_config: Path
    legacy_gateway: Path
    legacy_data: Path
    state: Path
    config: Path
    gateway: Path
    data: Path
    unit_dir: Path
    bin_dir: Path

    @classmethod
    def of(cls, environ: Mapping[str, str]) -> "Layout":
        env = dict(environ)
        home = paths.home(env)
        return cls(
            environ=env, home=home,
            legacy_state=legacy.state_root(env), legacy_config=legacy.config_root(env),
            legacy_gateway=legacy.gateway_config_dir(env), legacy_data=legacy.data_root(env),
            state=paths.state_root(env), config=paths.config_root(env),
            gateway=paths.gateway_config_dir(env), data=paths.data_root(env),
            unit_dir=Path(linux_service.unit_dir(env)), bin_dir=home / ".local" / "bin",
        )

    @property
    def legacy_install(self) -> Path:
        return self.legacy_data / legacy.LEGACY_INSTALL_DIR

    @property
    def install(self) -> Path:
        return self.data / installs.BUNDLE_DIR

    def legacy_units(self) -> tuple[tuple[str, Path], ...]:
        return tuple((name, self.unit_dir / f"{name}.service")
                     for name in (legacy.LEGACY_UNIT, legacy.LEGACY_FAILURE_UNIT))

    def charmer_units(self) -> tuple[tuple[str, Path], ...]:
        return tuple((name, self.unit_dir / f"{name}.service") for name in (CHARMER_UNIT, f"{CHARMER_UNIT}-failure"))

    def legacy_nix_links(self) -> tuple[Path, ...]:
        return (self.legacy_data / legacy.LEGACY_NIX_LINK, self.home / legacy.LEGACY_NIX_RELEASE_LINK)

    def legacy_cache_dirs(self) -> tuple[Path, ...]:
        return tuple(self.home / relative for relative in legacy.LEGACY_CACHE_DIRS)

    def legacy_roots(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((self.legacy_state, self.legacy_config, self.legacy_gateway)))

    def legacy_present(self) -> bool:
        """An earlier installation's state or configuration exists (its data
        root alone, which ``--purge`` removes, is not an installation)."""

        return any(os.path.lexists(root) for root in self.legacy_roots())

    def show(self, path: Path | str) -> str:
        return paths.display(path, self.environ)

    def relocation(self) -> tuple[tuple[Path, Path], ...]:
        """``(earlier root, charmer root)`` pairs, the gateway directory's
        before the configuration root's when it lies inside it."""

        pairs = [(self.legacy_state, self.state), (self.legacy_gateway, self.gateway),
                 (self.legacy_config, self.config)]
        return tuple(dict.fromkeys(pairs))

    def mapped(self, path: Path) -> Path | None:
        """Where ``path`` (under an earlier root) lives once the roots moved;
        None for a path outside them, or one that stays (``install/``, ``nix/``)."""

        for old, new in self.relocation():
            try:
                return new / path.relative_to(old)
            except ValueError:
                continue
        try:
            relative = path.relative_to(self.legacy_data)
        except ValueError:
            return None
        if relative.parts and relative.parts[0] not in KEPT_DATA:
            return self.data / relative
        return None


@dataclass(frozen=True)
class Move:
    source: Path
    target: Path

    def document(self) -> dict[str, str]:
        return {"from": str(self.source), "to": str(self.target)}


def plan_moves(layout: Layout) -> list[Move]:
    """The renames of the ``roots`` step, the state root last (the journal
    moves with it). The two configuration authorities move once each: the
    gateway directory only when it is another directory than the
    configuration root."""

    moves: list[Move] = []
    pairs = [(layout.legacy_config, layout.config)]
    if layout.legacy_gateway != layout.legacy_config:
        pairs.append((layout.legacy_gateway, layout.gateway))
    for source, target in pairs:
        if os.path.lexists(source):
            moves.append(Move(source, target))
    if os.path.isdir(layout.legacy_data) and not os.path.islink(layout.legacy_data):
        for name in sorted(os.listdir(layout.legacy_data)):
            if name not in KEPT_DATA:
                moves.append(Move(layout.legacy_data / name, layout.data / name))
    if os.path.lexists(layout.legacy_state):
        moves.append(Move(layout.legacy_state, layout.state))
    return moves


def _kind(layout: Layout, source: Path) -> str:
    if source == layout.legacy_state:
        return "state"
    if source in (layout.legacy_config, layout.legacy_gateway):
        return "config"
    return "data"


def _nested(one: Path, other: Path) -> bool:
    return one != other and (one in other.parents or other in one.parents)


# ------------------------------------------------------------ a destination's skeleton

def _skeleton_file(relative: Path, info: os.stat_result, kind: str, root: Path) -> bool:
    """A file charmer's installer (or a first command) leaves in a root
    before it holds anything of the user's: an empty lock file, the channel
    and state-version markers, charmer's own shims."""

    if not stat.S_ISREG(info.st_mode):
        return False
    if relative.name.endswith(".lock") and info.st_size == 0:
        return True
    if kind != "state":
        return False
    if relative == Path(installs.MARKER):
        try:
            return state.read_private(root / relative) == b"bundle\n"
        except (OSError, errors.CharmerError):
            return False
    if relative == Path(sessions.STATE_MARKER):
        try:
            return state.read_private(root / relative) == f"{sessions.SUPPORTED_STATE_VERSION}\n".encode("ascii")
        except (OSError, errors.CharmerError):
            return False
    return relative.parent == Path("bin") and relative.name in CHARMER_SHIMS


def skeleton(root: Path, kind: str, *, first: bool = False) -> tuple[list[Path], list[Path]]:
    """``(removable, foreign)`` under ``root``: the skeleton's files, and
    anything that is not skeleton (a second root's content). An absent root
    has neither. ``first`` stops at the first foreign entry (a yes/no check
    that never walks a populated root)."""

    removable: list[Path] = []
    foreign: list[Path] = []
    if not os.path.lexists(root):
        return removable, foreign
    if os.path.islink(root) or not os.path.isdir(root):
        return removable, [root]
    for directory, dirnames, filenames in os.walk(root):
        links = [name for name in dirnames if os.path.islink(os.path.join(directory, name))]
        foreign.extend(Path(directory) / name for name in sorted(links))
        if foreign and first:
            return removable, foreign
        dirnames[:] = sorted(name for name in dirnames if name not in links)
        for name in sorted(filenames):
            path = Path(directory) / name
            info = os.lstat(path)
            if _skeleton_file(path.relative_to(root), info, kind, root):
                removable.append(path)
            else:
                foreign.append(path)
                if first:
                    return removable, foreign
    return removable, foreign


def _clear_skeleton(root: Path, kind: str, layout: Layout) -> None:
    """Remove a destination's skeleton (refusing anything else) and the root itself."""

    removable, foreign = skeleton(root, kind)
    if foreign:
        raise MigrationError(f"{layout.show(foreign[0])} is more than charmer's empty skeleton; two roots are never "
                             "merged", remedy=f"move {layout.show(root)} aside, then: {COMMAND} (or: {BACK})")
    for path in removable:
        os.unlink(path)
    for directory in sorted((entry[0] for entry in os.walk(root)), key=len, reverse=True):
        os.rmdir(directory)


def _empty_dir(path: Path) -> bool:
    try:
        return os.path.isdir(path) and not os.path.islink(path) and not os.listdir(path)
    except OSError:
        return False


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        current = current.parent
    return current


# ------------------------------------------------------------ the journal

def journal_dir(state_root: Path) -> Path:
    return Path(state_root) / JOURNAL_DIR


def journal_path(state_root: Path) -> Path:
    return journal_dir(state_root) / JOURNAL_NAME


@dataclass
class Journal:
    root: Path  # the state root it lives in now
    document: dict[str, Any]

    @property
    def status(self) -> str:
        return str(self.document.get("status"))

    @property
    def done(self) -> list[str]:
        return list(self.document.get("done", []))

    def saved(self, *parts: str) -> Path:
        return journal_dir(self.root).joinpath(SAVED_DIR, *parts)

    def moves(self) -> list[Move]:
        return [Move(Path(item["from"]), Path(item["to"])) for item in self.document.get("moves", [])]


def _now_text(seams: Seams) -> str:
    return seams.now().astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def _check_document(document: Any, where: Path) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("version") != VERSION or document.get("status") not in STATUSES:
        raise MigrationError(f"{where} is not a migration journal this release reads; nothing was changed",
                             remedy="restore your cold backup")
    done = document.get("done")
    if not isinstance(done, list) or not all(step in STEPS for step in done):
        raise MigrationError(f"{where} names steps this release does not know; nothing was changed",
                             remedy="restore your cold backup")
    moves = document.get("moves", [])
    if not isinstance(moves, list) or not all(
            isinstance(item, dict) and set(item) == {"from", "to"}
            and all(isinstance(value, str) and os.path.isabs(value) for value in item.values()) for item in moves):
        raise MigrationError(f"{where} records moves this release cannot read; nothing was changed",
                             remedy="restore your cold backup")
    return document


def read_journal(state_root: Path) -> Journal | None:
    target = journal_path(state_root)
    if not os.path.lexists(target):
        return None
    try:
        document = strict_json.loads(state.read_private(target))
    except (OSError, ValueError, RecursionError, errors.CharmerError) as exc:
        raise MigrationError(f"the migration journal {target} cannot be read ({exc}); nothing was changed",
                             remedy="restore your cold backup") from exc
    return Journal(Path(state_root), _check_document(document, target))


def locate(layout: Layout) -> Journal | None:
    """The journal wherever the state root is now: the earlier root before
    the state moved (and after ``--back``), charmer's once it moved."""

    found = [journal for journal in (read_journal(layout.legacy_state), read_journal(layout.state))
             if journal is not None]
    unfinished = [journal for journal in found if journal.status in UNFINISHED]
    if len(unfinished) > 1:
        raise MigrationError("two unfinished migration journals exist; nothing was changed",
                             remedy=f"restore your cold backup (both {layout.show(layout.legacy_state)} and "
                                    f"{layout.show(layout.state)} hold one)")
    if unfinished:
        return unfinished[0]
    for journal in found:
        if journal.root == layout.state:
            return journal
    return found[0] if found else None


def pending(environ: Mapping[str, str]) -> Journal | None:
    """The unfinished journal every other command refuses on, or None. A
    journal that cannot be read counts as unfinished (its state is unknown)."""

    layout = Layout.of(environ)
    try:
        journal = locate(layout)
    except MigrationError:
        return Journal(layout.state, {"status": RUNNING, "done": []})
    return journal if journal is not None and journal.status in UNFINISHED else None


# The gateway commands that name and resolve charmer's own persistence
# hold, which --back refuses on: read-only status and logs, and the typed
# clear-hold. They run while a move is unfinished once it put charmer's
# roots, values and helpers in place (charmer's gateway may run on them),
# until --back begins to change any of them.
HOLD_COMMANDS = ("status", "logs", "clear-hold")


def runs_while_pending(journal: Journal, command: str | None, verb: str | None) -> bool:
    """Whether ``charmer <command> <verb>`` may run while ``journal`` is
    unfinished: a hold command of charmer's gateway, once the move finished
    its roots, authorities and shims, and while the move runs or ``--back``
    has not yet changed anything of charmer's (no :data:`UNWOUND` mark: it
    stopped at the hold check of its own gateway stop, or before)."""

    if command != "gateway" or verb not in HOLD_COMMANDS or not {ROOTS, AUTHORITIES, SHIMS} <= set(journal.done):
        return False
    return journal.status == RUNNING or (journal.status == REVERTING and not journal.document.get(UNWOUND))


def pending_refusal(journal: Journal) -> tuple[str, str]:
    """The one-line refusal (and its fix) of a command run while ``journal`` is unfinished."""

    if journal.status == REVERTING:
        return ("moving the earlier installation back did not finish; nothing was run",
                f"finish it: {BACK} (progress: {STATUS})")
    return ("the move from the earlier installation did not finish; nothing was run",
            f"finish it: {COMMAND}, or undo it: {BACK} (progress: {STATUS})")


def _write_journal(journal: Journal, seams: Seams) -> None:
    journal.document["updated"] = _now_text(seams)
    state.ensure_private_dir(journal_dir(journal.root))
    state.atomic_write(journal_path(journal.root), strict_json.pretty_file_bytes(journal.document))


def _relocate(journal: Journal, layout: Layout) -> None:
    """Follow the journal to wherever the state root is now."""

    for root in (layout.state, layout.legacy_state):
        if os.path.lexists(journal_path(root)):
            journal.root = root
            return


def _save(journal: Journal, name: tuple[str, ...], data: bytes) -> None:
    """Keep ``data`` for ``--back``; the first copy wins (a rerun never
    overwrites what the first run found)."""

    target = journal.saved(*name)
    if os.path.lexists(target):
        return
    state.ensure_private_dir(target.parent.parent)
    state.ensure_private_dir(target.parent)
    state.atomic_write(target, data)


def _read_bytes(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _first_line(path: Path) -> str | None:
    data = _read_bytes(path)
    return None if data is None else data.decode("utf-8", "replace").split("\n", 1)[0]


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _put_bytes(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` exactly, with ``mode`` (atomic rename)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.back.{os.getpid()}")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def launcher_owned(path: Path) -> bool:
    """A launcher the earlier installer wrote: a regular file whose second line is its mark."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        return False
    lines = (_read_bytes(path) or b"").decode("utf-8", "replace").split("\n")
    return len(lines) > 1 and lines[1] == legacy.LEGACY_LAUNCHER_MARK


def _rewrite_value(path: Path, key: str, old: str, new: str, layout: Layout) -> bool:
    """Replace the top-level ``key``'s string ``old`` with ``new`` in the JSON
    file ``path``; every other byte stays. False when ``key`` is not ``old``."""

    raw = state.read_private(path)
    document = strict_json.loads(raw)
    if not isinstance(document, dict) or document.get(key) != old:
        return False
    # The token is found by what it decodes to, so a value spelled as UTF-8
    # (the product's own writers) and one spelled with \u escapes both
    # match; the new value keeps the spelling the old one had.
    text = raw.decode("utf-8")
    found = [match for match in _MEMBER.finditer(text)
             if json.loads(match.group("key")) == key and json.loads(match.group("value")) == old]
    expected = {**document, key: new}
    updated = None
    if len(found) == 1:
        match = found[0]
        spelled = json.dumps(new, ensure_ascii="\\u" in match.group("value"))
        updated = (text[:match.start("value")] + spelled + text[match.end("value"):]).encode("utf-8")
    if updated is None or strict_json.loads(updated) != expected:
        raise MigrationError(f"{layout.show(path)}: its {key} cannot be rewritten in place",
                             remedy=f"set {key} to {new} in it yourself, then: {COMMAND} (or: {BACK})")
    state.atomic_write(path, updated)
    return True


# ------------------------------------------------------------ report-only findings

def _walk_files(root: Path) -> Iterator[Path]:
    if not os.path.isdir(root) or os.path.islink(root):
        return
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not os.path.islink(os.path.join(directory, name)))
        for name in sorted(filenames):
            yield Path(directory) / name


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _json_file(path: Path) -> Any:
    try:
        return strict_json.loads(state.read_private(path))
    except (OSError, ValueError, RecursionError, errors.CharmerError):
        return None


def report_only(layout: Layout, *, state_root: Path, config: Path, gateway: Path,
                data: Path) -> list[tuple[str, str]]:
    """What the move leaves as it is but the operator should know, each
    ``(finding, remedy)`` (names only, nothing is changed): crash remnants,
    a gateway ``.env``, names under charmer's reserved ``CHARMER_`` prefix,
    project directories inside the moved roots, developer drafts and reviews
    that fence the earlier paths, the earlier Nix selection and old exports.
    ``state_root``/``config``/``gateway``/``data`` are where those roots are now."""

    found: list[tuple[str, str]] = []
    remnants = [path for root in dict.fromkeys((state_root, config, gateway)) for path in _walk_files(root)
                if _TEMPORARY.match(path.name)]
    if remnants:
        found.append((f"{len(remnants)} interrupted write(s) left temporary files "
                      f"({', '.join(layout.show(path) for path in remnants[:3])}); they move as they are",
                      "remove them once nothing is running"))
    if os.path.lexists(gateway / GATEWAY_ENV):
        found.append((f"{layout.show(gateway / GATEWAY_ENV)} exists; it moves as it is and is never read",
                      "check whether the gateway still needs it"))
    reserved: list[str] = []
    documents = [(name, config / name) for name in ("custom.json", "settings.json")]
    documents.append((operator.LEDGER_NAME, gateway / operator.LEDGER_NAME))
    providers = [path for path in _walk_files(gateway / operator.PROVIDERS_DIRNAME) if path.suffix == ".json"]
    for shown, path in documents:
        for value in _strings(_json_file(path)):
            match = _RESERVED_NAME.match(value)
            if match:
                reserved.append(f"{shown}: {match.group(1)}")
    if reserved:
        found.append((f"charmer reserves the CHARMER_ prefix, which these credential names use: "
                      f"{', '.join(reserved[:5])}", "give each credential a name outside CHARMER_"))
    # A provider whose secret reference uses the reserved prefix makes the
    # operator layer invalid: every change that writes the gateway
    # configuration is refused until it is renamed or removed.
    for path in providers:
        names = [match.group(1) for value in _strings(_json_file(path))
                 if (match := _RESERVED_NAME.match(value))]
        if names:
            shown, provider = f"{operator.PROVIDERS_DIRNAME}/{path.name}", path.stem
            found.append((f"{shown}: its secret {names[0]} uses charmer's reserved CHARMER_ prefix: the provider is "
                          "ignored for routing, and it blocks gateway configuration changes until you rename its "
                          f"secret or remove it (charmer providers rm {provider})",
                          f"store its key under a name outside CHARMER_ (charmer providers edit {provider}), or "
                          f"remove it: charmer providers rm {provider}"))
    moved = [*layout.legacy_roots(), layout.legacy_data]
    inside = []
    for path in _walk_files(state_root / "sessions"):
        if path.suffix != ".json" or path.name.endswith(".v3.json"):
            continue
        record = _json_file(path)
        cwd = record.get("cwd") if isinstance(record, dict) else None
        if isinstance(cwd, str) and any(Path(cwd) == root or root in Path(cwd).parents for root in moved):
            inside.append(path.stem[:8])
    if inside:
        found.append((f"{len(inside)} session(s) work in a directory inside the moved roots ({', '.join(inside[:5])})",
                      "resume them from where their files are now, or start them again"))
    fences = [str(root) for root in moved] + [legacy.LEGACY_RESOURCES.parent.as_posix()]
    drafts = []
    for path in _walk_files(state_root / DRAFTS):
        data_bytes = _read_bytes(path) or b""
        if path.suffix == ".json" and any(fence.encode() in data_bytes for fence in fences):
            drafts.append(path.name)
    if drafts:
        found.append((f"{len(drafts)} developer draft(s) or review(s) fence the earlier paths and need "
                      f"revalidation ({', '.join(drafts[:3])})", "check and review them again: charmer-dev check"))
    nix = layout.legacy_data / legacy.LEGACY_NIX_LINK
    if os.path.lexists(nix):
        found.append((f"the earlier Nix selection {layout.show(nix)} stays as history; it is not moved",
                      PURGE))
    found.append((f"operator exports {legacy.NAME} wrote stay where you saved them; charmer import reads them",
                  "nothing to do"))
    return found


# ------------------------------------------------------------ preconditions

@dataclass
class Check:
    """One precondition: ``problem`` None when it holds."""

    name: str
    problem: str | None = None
    remedy: str | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def _endpoint_document(gateway_dir: Path) -> dict[str, Any] | None:
    document = _json_file(gateway_dir / endpoint.ENDPOINT_FILE)
    return document if isinstance(document, dict) else None


def check_preconditions(layout: Layout, seams: Seams) -> list[Check]:
    """Every precondition of a fresh run, each with its fix (names only;
    nothing is written, a persistence hold included)."""

    checks: list[Check] = []

    def add(name: str, problem: str | None = None, remedy: str | None = None) -> None:
        checks.append(Check(name, problem, remedy))

    add("charmer runs from an installed release", seams.installed(layout.environ),
        "install charmer first (sh install.sh --no-setup), then run this from the installed charmer")
    if not layout.legacy_present():
        add(f"an installation of {legacy.NAME}", f"no installation of {legacy.NAME} was found "
            f"({layout.show(layout.legacy_state)}, {layout.show(layout.legacy_config)})")
        return checks
    add(f"an installation of {legacy.NAME}")
    add("its state root exists",
        None if os.path.isdir(layout.legacy_state) and not os.path.islink(layout.legacy_state)
        else f"{layout.show(layout.legacy_state)} does not exist (that installation never ran a session)",
        f"nothing to move; {fresh_start(layout)}")
    try:
        channel = installs.read_marker(layout.legacy_state, layout.environ)
        problem = None
        if channel == "nix" or (channel is None and any(os.path.lexists(link) for link in layout.legacy_nix_links())):
            problem = "it is a Nix installation; only a release bundle installation moves"
        elif channel not in (None, "bundle"):
            problem = f"its state root belongs to its {channel} channel"
    except installs.ChannelError as exc:
        problem = f"its state root's channel marker cannot be read ({exc})"
    add("it is a release bundle installation", problem,
        f"remove the Nix installation through your Nix setup; then {fresh_start(layout)}")
    add("its release bundle is installed",
        None if os.path.lexists(layout.legacy_install / self_update.CURRENT)
        else f"{layout.show(layout.legacy_install)} holds no installed release (--back would have nothing to return to)",
        f"reinstall the earlier release bundle, then retry; or {fresh_start(layout)}")
    try:
        sessions.check_state_marker(layout.legacy_state)
        problem = None
    except sessions.StateMarkerError as exc:
        problem = f"its state cannot be read by this release ({exc})"
    add("its state format is one charmer reads", problem, "install the newest charmer release, then retry")
    nested = [f"{layout.show(one)} and {layout.show(other)}" for one, other in (
        (layout.legacy_config, layout.legacy_gateway), (layout.config, layout.gateway)) if _nested(one, other)]
    add("its two configuration directories are one or apart",
        f"{nested[0]} lie one inside the other" if nested else None,
        "point XDG_CONFIG_HOME elsewhere (or unset it) for this run, then retry")
    moves = plan_moves(layout)
    second: list[str] = []
    for move in moves:
        kind = _kind(layout, move.source)
        if kind == "data":
            if os.path.lexists(move.target) and not _empty_dir(move.target):
                second.append(layout.show(move.target))
        elif skeleton(move.target, kind, first=True)[1]:
            second.append(layout.show(move.target))
    add("charmer's own roots hold nothing yet",
        f"charmer already keeps its own state or configuration ({', '.join(second[:3])}); two roots are never "
        "merged" if second else None,
        "move charmer's own directories aside (or remove them) to take the earlier installation's, then retry; "
        "charmer doctor names both")
    crossing = []
    for move in moves:
        try:
            if os.lstat(move.source).st_dev != os.stat(_existing_ancestor(move.target)).st_dev:
                crossing.append(f"{layout.show(move.source)} -> {layout.show(move.target)}")
        except OSError as exc:
            crossing.append(f"{layout.show(move.source)} ({exc.strerror or exc})")
    add("every move stays on one file system",
        f"{crossing[0]} would cross file systems, and nothing is copied" if crossing else None,
        "move that directory onto its destination's file system yourself, then retry")
    foreign = [layout.show(path) for _name, path in layout.legacy_units()
               if os.path.lexists(path) and _first_line(path) != legacy.LEGACY_UNIT_HEADER]
    add("its gateway units are its own",
        f"{foreign[0]} was not written by {legacy.NAME}'s gateway service install" if foreign else None,
        "move that unit file away (it is not the earlier gateway's), then retry")
    recorded = _endpoint_document(layout.legacy_gateway) or {}
    custom = recorded.get("unit") if recorded.get("backend") == endpoint.SYSTEMD else None
    add("its gateway service has the earlier default name",
        f"its gateway service is installed as {custom}, and only {legacy.LEGACY_UNIT} moves"
        if isinstance(custom, str) and custom != legacy.LEGACY_UNIT else None,
        f"with the earlier release: {legacy.NAME} gateway service uninstall; then retry (and afterwards, if you "
        f"want that name: charmer gateway service install --name {custom})")
    foreign = [layout.show(path) for _name, path in layout.charmer_units()
               if os.path.lexists(path) and not systemd_unit.written_here(_first_line(path) or "")]
    add("no foreign charmer-gateway unit", f"{foreign[0]} exists and was not written by charmer" if foreign else None,
        "move that unit file away, then retry")
    document = _json_file(layout.legacy_gateway / continuity.FILE_NAME)
    root = document.get("state_root") if isinstance(document, dict) else None
    add("its gateway serves its own state root",
        f"{continuity.FILE_NAME} names another state root ({root}); charmer would not adopt it"
        if isinstance(root, str) and Path(root) != layout.legacy_state else None,
        f"settle which state root this gateway serves with the earlier release first, then retry")
    switch = layout.install / self_update.SWITCH_RECORD
    add("no unfinished install or update of charmer",
        f"an install or update of charmer did not finish ({layout.show(switch)})" if os.path.lexists(switch) else None,
        "finish it first: sh install.sh --repair")
    switch = layout.legacy_install / self_update.SWITCH_RECORD
    add(f"no unfinished install or update of {legacy.NAME}",
        f"an install or update of {legacy.NAME} did not finish ({layout.show(switch)})"
        if os.path.lexists(switch) else None,
        f"let the retained 1.1.0 launcher recover it first ({legacy.NAME} update), then retry")
    # An inhibition of this command whose run died before it wrote its
    # journal (only an earlier build began one first) is not a transaction
    # of the earlier release: this run takes it over.
    stranded = _stranded(layout.legacy_state) is not None
    records = [layout.show(path) for path in (gateway_inhibition.record_path(layout.legacy_state),
                                              gateway_inhibition.legacy_path(layout.legacy_state))
               if os.path.lexists(path)
               and not (stranded and path == gateway_inhibition.record_path(layout.legacy_state))]
    add("no unfinished gateway transaction of the earlier release",
        f"an unfinished gateway transaction is recorded ({records[0]})" if records else None,
        f"let the earlier release finish it ({legacy.NAME} gateway service status names it), then retry")
    try:
        live = list(seams.liveness(layout.legacy_state))
        problem = (f"{len(live)} managed session(s) run now: {', '.join(item[:8] for item in live[:5])}"
                   if live else None)
    except LivenessUnknown as exc:
        problem = f"whether a managed session runs cannot be told ({exc})"
    add("no managed session runs", problem,
        "close every managed session (the lead's too), then run this from a plain terminal")
    reasons = list(seams.hold(layout))
    add("its gateway may stop (no persistence hold)",
        f"its gateway may hold an unsaved credential: {reasons[0]}" if reasons else None,
        f"resolve the hold with the earlier release first ({legacy.NAME} gateway clear-hold), then retry")
    return checks


def _refuse(checks: list[Check]) -> None:
    failed = [check for check in checks if not check.ok]
    if failed:
        first = failed[0]
        raise MigrationError(f"{first.problem}; nothing was changed", remedy=first.remedy,
                             lines=[f"{check.problem} (fix: {check.remedy})" if check.remedy else str(check.problem)
                                    for check in failed])


def _check_live(seams: Seams, root: Path) -> None:
    try:
        live = list(seams.liveness(root))
    except LivenessUnknown as exc:
        raise MigrationError(f"whether a managed session runs cannot be told ({exc}); nothing was changed",
                             remedy="close every managed session, then retry") from exc
    if live:
        raise MigrationError(f"{len(live)} managed session(s) run now ({', '.join(item[:8] for item in live[:5])});"
                             " nothing was changed", remedy="close every managed session, then retry")


# ------------------------------------------------------------ guards

@contextlib.contextmanager
def _install_locks(layout: Layout) -> Iterator[None]:
    """Both install locks (charmer's and the earlier installation's), held
    for the whole transaction."""

    with contextlib.ExitStack() as stack:
        for root in (layout.install, layout.legacy_install):
            if os.path.isdir(root):
                try:
                    stack.enter_context(self_update.install_lock(root))
                except self_update.UpdateError as exc:
                    raise MigrationError(f"{exc}; nothing was changed", remedy=exc.remedy or "retry later") from exc
        yield


@contextlib.contextmanager
def _root_guards(layout: Layout, state_root: Path, gateway_dir: Path, data_root: Path) -> Iterator[None]:
    """The guards of one set of roots: the session-store migration lock
    (exclusive), the served-change barrier and every owned Claude Code
    copy's use lock, none waited on for long. They are objects on these
    paths only: a moved root's guards are released, never handed on."""

    from charmer import pin

    with contextlib.ExitStack() as stack:
        lock = sessions.migration_lock(state_root)
        if not lock.acquire(blocking=False):
            raise MigrationError("a session-record migration or restore is running; nothing was changed",
                                 remedy="wait until it has finished, then retry")
        stack.callback(lock.release)
        if os.path.isdir(gateway_dir):
            try:
                barrier = state.acquire_served_barrier(gateway_dir, timeout=LOCK_WAIT)
            except state.StateError as exc:
                raise MigrationError(f"the gateway's served-change barrier stays taken ({exc}); nothing was changed",
                                     remedy="retry in a moment") from exc
            stack.callback(barrier.release)
        owned = data_root / "claude"
        if os.path.isdir(owned) and not os.path.islink(owned):
            for version in sorted(os.listdir(owned)):
                if version.startswith(".") or not os.path.isdir(owned / version):
                    continue
                try:
                    use = pin.take_use_lock_in(owned, version, shared=False, blocking=False)
                except (OSError, errors.CharmerError) as exc:
                    raise MigrationError(f"the Claude Code copy {version} cannot be locked ({exc}); nothing was "
                                         "changed", remedy="check its directory, then retry") from exc
                if use is None:
                    raise MigrationError(f"Claude Code {version} is in use (a session or a probe runs it); nothing was "
                                         "changed", remedy="close every managed session, then retry")
                stack.callback(use.release)
        yield


def _stranded(root: Path) -> gateway_inhibition.Inhibition | None:
    """This command's own inhibition on ``root`` whose owner is gone (a run
    interrupted before it wrote its journal), or None."""

    try:
        record = gateway_inhibition.read(root) if os.path.isdir(root) else None
    except (OSError, errors.CharmerError):
        return None
    if record is None or not record.readable or record.owner != OWNER or not record.stale():
        return None
    return record


def _begin_inhibition(root: Path) -> str:
    try:
        return gateway_inhibition.begin(
            root, owner=OWNER, purpose=f"move the earlier {legacy.NAME} installation onto charmer",
            phase="migrate", remedy=f"{COMMAND} (or: {BACK})", take_over_stale=True, lock_wait=LOCK_WAIT)
    except gateway_inhibition.InhibitionError as exc:
        raise MigrationError(f"{exc}; nothing was changed", remedy=exc.remedy) from exc


def _handover(root: Path, token: str) -> str:
    """End the inhibition the moved root carries (an earlier root's token)
    and record a fresh one for ``root``, under the gateway's start lock, so
    no start slips in between. Returns the fresh token."""

    workdir = service.ensure_gateway_workdir(root)
    lock = state.FileLock(workdir / service.START_LOCK.removesuffix(".lock"))
    if not lock.acquire(blocking=False):
        raise MigrationError("a start or stop of the gateway holds its start lock",
                             remedy=f"retry in a moment: {COMMAND}")
    try:
        record = gateway_inhibition.read(root)
        if record is not None and record.readable and record.token == token:
            gateway_inhibition.end(root, token, held_lock=True)
        try:
            return gateway_inhibition.begin(
                root, owner=OWNER, purpose=f"move the earlier {legacy.NAME} installation onto charmer",
                phase="migrate", remedy=f"{COMMAND} (or: {BACK})", take_over_stale=True, held_lock=True,
                lock_wait=LOCK_WAIT)
        except gateway_inhibition.InhibitionError as exc:
            raise MigrationError(str(exc), remedy=exc.remedy or f"{COMMAND} again") from exc
    finally:
        lock.release()


def _end_inhibition(layout: Layout, token: str | None) -> None:
    if token is None:
        return
    for root in (layout.state, layout.legacy_state):
        if not os.path.isdir(root):
            continue
        record = gateway_inhibition.read(root)
        if record is not None and record.readable and record.token == token:
            gateway_inhibition.end(root, token)
            return


def _with_token(layout: Layout, token: str) -> dict[str, str]:
    return {**layout.environ, gateway_inhibition.TOKEN_ENV: token}


# ------------------------------------------------------------ the steps

def _unit_state(name: str, seams: Seams) -> dict[str, Any]:
    properties = linux_service.unit_properties(name, seams.runner) or {}
    return {"enabled": properties.get("UnitFileState") in ("enabled", "enabled-runtime"),
            "unit_file_state": properties.get("UnitFileState"),
            "active": properties.get("ActiveState") in ACTIVE_STATES}


def _step_units(journal: Journal, layout: Layout, seams: Seams) -> None:
    units = journal.document.setdefault("units", {})
    present: list[str] = []
    for name, path in layout.legacy_units():
        data = _read_bytes(path)
        if data is None:
            units.setdefault(name, {"present": False, "enabled": False, "active": False})
            continue
        if data.decode("utf-8", "replace").split("\n", 1)[0] != legacy.LEGACY_UNIT_HEADER:
            raise MigrationError(f"{layout.show(path)} was not written by {legacy.NAME}; it was left alone",
                                 remedy=f"move it away, then: {COMMAND}")
        _save(journal, ("units", path.name), data)
        if name not in units:
            units[name] = {"present": True, "mode": _mode(path), **_unit_state(name, seams)}
            _write_journal(journal, seams)
        present.append(name)
    # The hold is evaluated with the earlier unit and start line before the
    # gateway stops; it is never recorded or cleared here.
    reasons = list(seams.hold(layout))
    if reasons:
        raise MigrationError(f"the earlier gateway may hold an unsaved credential: {reasons[0]}",
                             remedy=f"{BACK}; resolve the hold with the earlier release ({legacy.NAME} gateway "
                                    f"clear-hold); then {COMMAND}", lines=reasons)
    shutdown = journal.document.setdefault("shutdown", {})
    for name in present:
        if not linux_service.disable_now(name, seams.runner):
            raise MigrationError(f"the service manager did not stop and disable {name}",
                                 remedy=f"check it: {linux_service.hint('status', unit=name)}; then: {COMMAND}")
        if name not in shutdown:
            shutdown[name] = {
                "at": _now_text(seams),
                "result": linux_service.manager_property("Result", f"{name}.service", seams.runner),
                "exit_status": linux_service.manager_property("ExecMainStatus", f"{name}.service", seams.runner),
                "active": (linux_service.unit_properties(name, seams.runner) or {}).get("ActiveState"),
            }
    stamp = _json_file(service.gateway_workdir(layout.legacy_state) / service.EXEC_STAMP)
    if isinstance(stamp, dict) and "exec_stamp" not in journal.document:
        journal.document["exec_stamp"] = stamp
    _write_journal(journal, seams)
    instance = service.gateway_workdir(layout.legacy_state) / service.INSTANCE_LOCK
    if posix_fs.lock_held(instance) is not False:
        raise MigrationError(f"the earlier gateway still runs (it holds {layout.show(instance)})",
                             remedy=f"stop it with the earlier release ({legacy.NAME} gateway stop), then: {COMMAND}")


def _step_roots(journal: Journal, layout: Layout, seams: Seams) -> None:
    for move in journal.moves():
        source, target = move.source, move.target
        if not os.path.lexists(source):
            if not os.path.lexists(target):
                raise MigrationError(f"{layout.show(source)} is gone, and {layout.show(target)} does not exist",
                                     remedy="restore your cold backup")
            continue  # moved by an earlier run
        kind = _kind(layout, source)
        if os.path.lexists(target):
            if kind != "data":
                _clear_skeleton(target, kind, layout)
            elif _empty_dir(target):
                os.rmdir(target)
            else:
                raise MigrationError(f"{layout.show(target)} exists; two roots are never merged",
                                     remedy=f"move it aside, then: {COMMAND} (or: {BACK})")
        if kind == "data":
            state.ensure_private_dir(target.parent)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, target)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise MigrationError(f"{layout.show(source)} and {layout.show(target)} are on different file "
                                     "systems, and nothing is copied", remedy=f"undo the moves so far: {BACK}") from exc
            raise MigrationError(f"{layout.show(source)} could not be moved ({exc.strerror or exc})",
                                 remedy=f"{COMMAND} again, or: {BACK}") from exc
        _relocate(journal, layout)


def _step_authorities(journal: Journal, layout: Layout, seams: Seams) -> None:
    for name in AUTHORITY_FILES:
        data = _read_bytes(layout.gateway / name)
        if data is not None:
            _save(journal, ("authorities", name), data)
    receipt = _read_bytes(install_receipt.receipt_path(layout.install))
    if receipt is not None:
        _save(journal, ("authorities", RECEIPT_SAVED), receipt)
    rewritten = journal.document.setdefault("rewritten", [])
    if os.path.isdir(layout.gateway):
        with state.FileLock(layout.gateway / "api-key"):
            for name, done in ((continuity.FILE_NAME, _rewrite_continuity(layout)),
                               (SECRET_POINTER, _rewrite_pointer(layout)),
                               (endpoint.ENDPOINT_FILE, _rewrite_endpoint(layout))):
                if done and name not in rewritten:
                    rewritten.append(name)
    if _rewrite_receipt(layout) and RECEIPT_SAVED not in rewritten:
        rewritten.append(RECEIPT_SAVED)


def _rewrite_continuity(layout: Layout) -> bool:
    """``state_root`` through the relocation map; the aliases, tombstones and
    watermark keep their bytes."""

    target = layout.gateway / continuity.FILE_NAME
    document = _json_file(target) if os.path.lexists(target) else None
    recorded = document.get("state_root") if isinstance(document, dict) else None
    if not isinstance(recorded, str):
        return False
    moved = layout.mapped(Path(recorded))
    if moved is None:
        return False
    return _rewrite_value(target, "state_root", recorded, str(moved), layout)


def _rewrite_pointer(layout: Layout) -> bool:
    """``path`` only when the key file it selects moved (absolute or ``~/``,
    kept in the spelling it had); a file elsewhere, or in the retained
    install tree, stays selected as it is."""

    target = layout.gateway / SECRET_POINTER
    document = _json_file(target) if os.path.lexists(target) else None
    text = document.get("path") if isinstance(document, dict) else None
    if not isinstance(text, str):
        return False
    selected = layout.home / text[2:] if text.startswith("~/") else Path(text)
    moved = layout.mapped(selected)
    if moved is None:
        return False
    spelled = str(moved)
    if text.startswith("~/"):
        with contextlib.suppress(ValueError):
            spelled = f"~/{moved.relative_to(layout.home).as_posix()}"
    return _rewrite_value(target, "path", text, spelled, layout)


def _rewrite_endpoint(layout: Layout) -> bool:
    """``unit`` when it is the earlier default; every other key keeps its bytes."""

    target = layout.gateway / endpoint.ENDPOINT_FILE
    document = _json_file(target) if os.path.lexists(target) else None
    if not isinstance(document, dict) or document.get("unit") != legacy.LEGACY_UNIT:
        return False
    return _rewrite_value(target, "unit", legacy.LEGACY_UNIT, CHARMER_UNIT, layout)


def _rewrite_receipt(layout: Layout) -> bool:
    """charmer's receipt keeps its own launchers and takes over the earlier
    installer's PATH lines that are still present (verbatim, marker
    included), so ``charmer uninstall`` removes them; the earlier receipt
    stays as it is with the earlier install tree."""

    try:
        own = install_receipt.read(layout.install)
    except install_receipt.ReceiptError:
        return False
    taken = _path_lines_to_take(layout)
    if not taken:
        return False
    merged = install_receipt.Receipt(own.launchers, (*own.path_lines, *taken))
    if merged.document() == own.document():
        return False
    install_receipt.write(layout.install, merged)
    return True


def _path_lines_to_take(layout: Layout) -> list[install_receipt.PathLine]:
    """The earlier installer's PATH lines still present in their shell
    profile that charmer's receipt does not list yet (none when either
    receipt is missing or unreadable)."""

    try:
        own = install_receipt.read(layout.install)
        earlier = install_receipt.read(layout.legacy_install)
    except install_receipt.ReceiptError:
        return []
    known = {(item.file, item.line) for item in own.path_lines}
    taken: dict[tuple[str, str], install_receipt.PathLine] = {}
    for item in earlier.path_lines:
        if (item.file, item.line) not in known and item.present():
            taken.setdefault((item.file, item.line), item)
    return list(taken.values())


def _step_shims(journal: Journal, layout: Layout, seams: Seams, token: str) -> None:
    bin_dir = layout.state / "bin"
    for name in legacy.LEGACY_SHIMS:
        data = _read_bytes(bin_dir / name)
        if data is not None:
            _save(journal, ("shims", name), data)
            os.unlink(bin_dir / name)
    seams.shims(_with_token(layout, token))
    missing = [name for name in CHARMER_SHIMS if not os.access(bin_dir / name, os.X_OK)]
    if missing:
        raise MigrationError(f"charmer's shim {layout.show(bin_dir / missing[0])} was not written",
                             remedy=f"{COMMAND} again")


def _service_wanted(journal: Journal, layout: Layout) -> bool:
    if journal.document.get("units", {}).get(legacy.LEGACY_UNIT, {}).get("present"):
        return True
    document = _endpoint_document(layout.gateway) or {}
    return document.get("backend") == endpoint.SYSTEMD


def _step_gateway(journal: Journal, layout: Layout, seams: Seams, token: str) -> None:
    wanted = _service_wanted(journal, layout)
    journal.document["service"] = wanted
    if not wanted:
        return
    ok, message = seams.gateway(_with_token(layout, token))
    if not ok:
        raise MigrationError(f"the gateway did not start as {CHARMER_UNIT}: {message}",
                             remedy=f"see {STATUS}; then {COMMAND} again (or: {BACK})")


def _step_remnants(journal: Journal, layout: Layout, seams: Seams) -> None:
    removed = journal.document.setdefault("launchers_removed", {})
    for name in legacy.LEGACY_LAUNCHERS:
        path = layout.bin_dir / name
        if launcher_owned(path):
            _save(journal, ("launchers", name), _read_bytes(path) or b"")
            removed.setdefault(name, _mode(path))
            _write_journal(journal, seams)
            os.unlink(path)
    reload = False
    for _name, path in layout.legacy_units():
        if os.path.lexists(path) and _first_line(path) == legacy.LEGACY_UNIT_HEADER:
            _save(journal, ("units", path.name), _read_bytes(path) or b"")
            os.unlink(path)
            reload = True
    if reload and not linux_service.daemon_reload(seams.runner):
        raise MigrationError("the service manager did not reload its unit files",
                             remedy=f"{linux_service.RELOAD_HINT}, then: {COMMAND}")


def _step_verify(journal: Journal, layout: Layout, seams: Seams) -> None:
    started = datetime.datetime.fromisoformat(str(journal.document.get("started")))
    problems = [str(line) for line in seams.verify(layout.environ, started)]
    journal.document["verify"] = problems
    if problems:
        raise MigrationError(f"the moved installation does not verify: {problems[0]}",
                             remedy=f"fix what doctor names, then: {COMMAND} (or: {BACK})", lines=problems)


# ------------------------------------------------------------ the run

@dataclass
class Report:
    """What a run did, line by line (for the command's output)."""

    lines: list[str] = field(default_factory=list)


def step_text(step: str, journal: Journal) -> str:
    """What ``step`` does, the gateway step as the journal's service decision made it."""

    if step == GATEWAY and journal.document.get("service") in GATEWAY_TEXT:
        return GATEWAY_TEXT[journal.document["service"]]
    return STEP_TEXT[step]


def _mark(journal: Journal, layout: Layout, seams: Seams, report: Report, step: str) -> None:
    _relocate(journal, layout)
    journal.document["done"].append(step)
    _write_journal(journal, seams)
    report.lines.append(step_text(step, journal))


def migrate(environ: Mapping[str, str], seams: Seams) -> Report:
    """Run (or finish) the migration. Raises :class:`MigrationError`."""

    layout = Layout.of(environ)
    report = Report()
    # The transaction lock (both install locks) comes first: the journal this
    # run acts on is read under it, so a --back or --purge never interleaves.
    with _install_locks(layout):
        journal = locate(layout)
        if journal is not None and journal.status == REVERTING:
            raise MigrationError("moving the earlier installation back did not finish; nothing was changed",
                                 remedy=f"finish it first: {BACK}")
        if journal is not None and journal.status == DONE:
            report.lines.append(f"the earlier installation was already moved "
                                f"({layout.show(journal_path(journal.root))})")
            return report
        fresh = journal is None or journal.status == REVERTED
        if fresh:
            _refuse(check_preconditions(layout, seams))
            journal = Journal(layout.legacy_state, {
                "version": VERSION, "status": RUNNING, "started": _now_text(seams), "updated": None, "done": [],
                "moves": [move.document() for move in plan_moves(layout)], "units": {}, "service": None,
                "launchers_removed": {}, "rewritten": [], "verify": [], "error": None, "purged": None})
            if os.path.lexists(journal_dir(journal.root)):
                shutil.rmtree(journal_dir(journal.root))  # a reverted run's evidence: this run starts over
            # The journal before anything else, the inhibition included: a
            # run that dies at any point after this is found (a rerun
            # finishes it, --back undoes it), and before it nothing changed.
            _write_journal(journal, seams)
        else:
            _check_live(seams, journal.root)
        assert journal is not None
        token: str | None = None
        try:
            # Steps 1-2 under the guards of the roots where they are now.
            if not (UNITS in journal.done and ROOTS in journal.done):
                token = _begin_inhibition(journal.root)
                gateway_dir = layout.legacy_gateway if os.path.isdir(layout.legacy_gateway) else layout.gateway
                data_root = layout.legacy_data if os.path.isdir(layout.legacy_data / "claude") else layout.data
                with _root_guards(layout, journal.root, gateway_dir, data_root):
                    # Under the migration lock no launch starts a session;
                    # one that started before it shows here.
                    _check_live(seams, journal.root)
                    for step, run in ((UNITS, _step_units), (ROOTS, _step_roots)):
                        if step not in journal.done:
                            run(journal, layout, seams)
                            _mark(journal, layout, seams, report, step)
            # The earlier roots' guards are released; charmer's are taken on
            # its own paths before anything is rewritten, rendered or started.
            token = _handover(layout.state, token) if token is not None else _begin_inhibition(layout.state)
            if AUTHORITIES not in journal.done:
                with _root_guards(layout, layout.state, layout.gateway, layout.data):
                    _check_live(seams, layout.state)
                    _step_authorities(journal, layout, seams)
                    _mark(journal, layout, seams, report, AUTHORITIES)
            for step in (SHIMS, GATEWAY, REMNANTS):
                if step in journal.done:
                    continue
                if step == SHIMS:
                    _step_shims(journal, layout, seams, token)
                elif step == GATEWAY:
                    _step_gateway(journal, layout, seams, token)
                else:
                    _step_remnants(journal, layout, seams)
                _mark(journal, layout, seams, report, step)
            _end_inhibition(layout, token)
            token = None
            if VERIFY not in journal.done:
                _step_verify(journal, layout, seams)
                _mark(journal, layout, seams, report, VERIFY)
            journal.document["status"] = DONE
            journal.document["error"] = None
            _write_journal(journal, seams)
        except (MigrationError, OSError, errors.CharmerError) as exc:
            _relocate(journal, layout)
            with contextlib.suppress(OSError, errors.CharmerError):
                _end_inhibition(layout, token)
            journal.document["error"] = str(exc)
            if os.path.lexists(journal_path(journal.root)):
                with contextlib.suppress(OSError, errors.CharmerError):
                    _write_journal(journal, seams)
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(f"the migration stopped: {exc}", remedy=f"{COMMAND} again, or: {BACK}") from exc
    return report


# ------------------------------------------------------------ back

def back(environ: Mapping[str, str], seams: Seams) -> Report:
    """Move everything back (from any step), so the retained earlier
    installation works as before: charmer's gateway is stopped (after its
    own persistence hold, proven stopped), the whole reverse plan is checked
    before anything is stopped or moved, the earlier unit files return byte
    for byte from the saved copies with their enablement and running state,
    the session records charmer wrote name the earlier selectors again, and
    the earlier release recompiles the session scopes charmer compiled
    (:func:`_repair_earlier`). Everything runs under the transaction lock
    (both install locks), the journal read under it. Raises
    :class:`MigrationError`."""

    layout = Layout.of(environ)
    report = Report()
    with _install_locks(layout):
        journal = locate(layout)
        if journal is None and _clear_stranded(layout):
            report.lines.append("cleared the inhibition a run left before it wrote its journal; nothing else had "
                                "changed")
            return report
        if journal is None or journal.status == REVERTED:
            raise MigrationError("no migration to undo was found; nothing was changed")
        if journal.document.get("purged"):
            raise MigrationError("the earlier installation was purged after this migration; it cannot be moved back",
                                 remedy="restore your cold backup")
        if not os.path.isdir(layout.legacy_install):
            raise MigrationError(f"the earlier install tree {layout.show(layout.legacy_install)} is gone; nothing "
                                 "was changed", remedy="restore your cold backup")
        _check_live(seams, journal.root)
        # Before anything stops or moves: every root can go back.
        _reverse_plan(journal, layout)
        # charmer's gateway may hold the only copy of a refreshed credential:
        # its own hold is evaluated while the journal still lets charmer's
        # gateway commands resolve it.
        ran = _charmer_gateway_may_run(journal, layout)
        if ran:
            reasons = list(seams.charmer_hold(layout))
            if reasons:
                raise MigrationError(f"charmer's gateway may hold an unsaved credential: {reasons[0]}; nothing was "
                                     "changed", lines=reasons,
                                     remedy="charmer gateway status names it; once the credential is saved again, "
                                            f"clear the hold (charmer gateway clear-hold), then: {BACK}")
        token: str | None = None
        try:
            token = _begin_inhibition(journal.root)
            journal.document["status"] = REVERTING
            _write_journal(journal, seams)
            if ran:
                _stop_charmer_gateway(layout, seams, token)
            _check_charmer_gateway_stopped(layout, unwound=False)
            # From here on charmer's units, values and roots change; until
            # this mark charmer's gateway hold commands still run.
            journal.document[UNWOUND] = True
            _write_journal(journal, seams)
            _undo_gateway(layout, seams)
            _check_charmer_gateway_stopped(layout, unwound=True)
            _undo_launchers(journal, layout)
            _undo_shims(journal, layout)
            moved = journal.root == layout.state
            gateway_dir = layout.gateway if moved and os.path.isdir(layout.gateway) else layout.legacy_gateway
            data_root = layout.data if os.path.isdir(layout.data / "claude") else layout.legacy_data
            with _root_guards(layout, journal.root, gateway_dir, data_root):
                _check_live(seams, journal.root)
                plan, notes = _reverse_plan(journal, layout)
                _undo_authorities(journal, layout, plan)
                _undo_roots(plan)
                _relocate(journal, layout)
                handed = _restore_sessions(layout)
                restored = journal.document.setdefault("sessions_restored", [])
                restored.extend(mid for mid in handed if mid not in restored)
                _write_journal(journal, seams)
            report.lines.append("moved the roots back")
            report.lines.extend(notes)
            if handed:
                report.lines.append(f"wrote {len(handed)} session(s) charmer resumed or started back in the earlier "
                                    "release's names (records and launch settings)")
            # The earlier gateway starts on the earlier roots, which keep no
            # inhibition of this run from here on.
            _end_inhibition(layout, token)
            token = None
            _undo_units(journal, layout, seams)
            report.lines.append("restored the earlier gateway units, launchers and shims")
        except (MigrationError, OSError, errors.CharmerError) as exc:
            _relocate(journal, layout)
            with contextlib.suppress(OSError, errors.CharmerError):
                _end_inhibition(layout, token)
            journal.document["error"] = str(exc)
            with contextlib.suppress(OSError, errors.CharmerError):
                _write_journal(journal, seams)
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(f"moving back stopped: {exc}", remedy=f"{BACK} again") from exc
        # Still under the transaction lock (the earlier launcher's repair
        # takes no install lock); every guard of this run is released.
        _repair_earlier(journal, layout, seams, report)
    return report


def _clear_stranded(layout: Layout) -> bool:
    """End this command's own stale inhibition where no journal exists (a
    run of an earlier build died between the two); True when one was."""

    for root in (layout.legacy_state, layout.state):
        record = _stranded(root)
        if record is not None and record.token:
            gateway_inhibition.end(root, record.token)
            return True
    return False


def _reverse_plan(journal: Journal, layout: Layout) -> tuple[list[Move], list[str]]:
    """The renames ``--back`` makes, the whole plan checked before any of
    them (or anything else) happens: each recorded move is already back
    (only its earlier path exists), still to go back (only charmer's path
    exists), or refused with a fix while the journal stays resumable. Two
    explicit rules: an earlier path something re-created as an empty
    directory is removed first (it holds nothing to merge), and a data-root
    child gone from both sides (removed after the move) is noted, not
    refused; a root gone from both sides is missing data and refuses.
    Returns the moves to make (in the order they are made) and the notes."""

    plan: list[Move] = []
    notes: list[str] = []
    problems: list[str] = []
    for move in reversed(journal.moves()):
        here, there = os.path.lexists(move.target), os.path.lexists(move.source)
        if there and (not here or _left_by_charmer(layout, move)):
            continue
        if here and (not there or _empty_dir(move.source)):
            plan.append(move)
            continue
        if here:
            problems.append(f"{layout.show(move.source)} exists again and is not empty, and "
                            f"{layout.show(move.target)} still holds what the move put there; two roots are never "
                            "merged")
        elif move.source.parent == layout.legacy_data:
            notes.append(f"{layout.show(move.target)} is gone (removed after the move); nothing to move back")
        else:
            problems.append(f"neither {layout.show(move.source)} nor {layout.show(move.target)} exists")
    if problems:
        raise MigrationError(f"{problems[0]}; nothing was moved back", lines=problems,
                             remedy=f"move the extra directory aside (or restore the missing one from your cold "
                                    f"backup), then: {BACK}")
    return plan, notes


def _left_by_charmer(layout: Layout, move: Move) -> bool:
    """What stands at a move's charmer path is charmer's own, not what the
    move put there: the empty skeleton its installer left (a root that has
    not moved yet), or an empty directory (a data-root child)."""

    kind = _kind(layout, move.source)
    if kind == "data":
        return _empty_dir(move.target)
    return os.path.isdir(move.target) and not skeleton(move.target, kind)[1]


def _charmer_gateway_may_run(journal: Journal, layout: Layout) -> bool:
    """Whether charmer's gateway may have run on charmer's state root: the
    state root moved and the gateway step began (or the move finished), a
    charmer unit file exists, or its instance lock is not known free."""

    if journal.root != layout.state:
        return False
    if journal.document.get("service") is not None or journal.status == DONE or VERIFY in journal.done:
        return True
    if any(os.path.lexists(path) for _name, path in layout.charmer_units()):
        return True
    return posix_fs.lock_held(service.gateway_workdir(layout.state) / service.INSTANCE_LOCK) is not False


def _stop_charmer_gateway(layout: Layout, seams: Seams, token: str) -> None:
    """charmer's gateway stopped by its own stop (its persistence hold
    evaluated again, the stop proven for its backend), before any unit file
    is removed or any root moves; a stop that fails or cannot be told
    refuses, nothing moved."""

    ok, message = seams.stop_gateway(_with_token(layout, token))
    if not ok:
        raise MigrationError(f"charmer's gateway was not stopped ({message}); nothing was moved back",
                             remedy="check it: charmer gateway status (it runs until --back changes anything; "
                                    "a persistence hold: once the credential is saved again, charmer gateway "
                                    f"clear-hold); then: {BACK}")


def _check_charmer_gateway_stopped(layout: Layout, *, unwound: bool) -> None:
    """No gateway holds charmer's instance lock (free, not unknown):
    checked before anything of charmer's changes (``unwound`` False, when
    charmer's gateway status still runs) and again once its units are gone."""

    instance = service.gateway_workdir(layout.state) / service.INSTANCE_LOCK
    if os.path.isdir(layout.state) and posix_fs.lock_held(instance) is not False:
        raise MigrationError(f"a gateway still runs on charmer's state root (it holds {layout.show(instance)}); "
                             "nothing was moved back",
                             remedy=("end that gateway's process" if unwound else
                                     "find it (charmer gateway status) and end its process") + f", then: {BACK}")


def earlier_selector(renamed: Mapping[str, Any], selector: Any) -> Any:
    """:func:`catalog.renamed_selector` backwards: a selector ``retired.json``
    ``renamed`` gave a new name, under the name it had before (``[1m]`` kept
    as given); anything else as it is."""

    if not isinstance(selector, str):
        return selector
    suffix = operator.SELECTOR_SUFFIX if selector.endswith(operator.SELECTOR_SUFFIX) else ""
    base = selector.removesuffix(operator.SELECTOR_SUFFIX)
    for old, spec in renamed.items():
        new = spec.get("selector") if isinstance(spec, Mapping) else None
        if isinstance(new, str) and new.removesuffix(operator.SELECTOR_SUFFIX) == base:
            return old.removesuffix(operator.SELECTOR_SUFFIX) + suffix
    return selector


def _earlier_names(value: Any, renamed: Mapping[str, Any]) -> Any:
    """``value`` with every string a renamed selector's new name replaced by
    its earlier one (:func:`earlier_selector`); keys stay."""

    if isinstance(value, str):
        return earlier_selector(renamed, value)
    if isinstance(value, dict):
        return {key: _earlier_names(item, renamed) for key, item in value.items()}
    if isinstance(value, list):
        return [_earlier_names(item, renamed) for item in value]
    return value


def _earlier_env_name(name: str) -> str:
    """A scope ``env`` key charmer compiles (``CHARMER_<suffix>``) under the
    name the earlier release compiled it with, when it had one."""

    if name.startswith(CHARMER_ENV_PREFIX):
        earlier = legacy.ENVIRONMENT_PREFIX + name.removeprefix(CHARMER_ENV_PREFIX)
        if earlier in legacy.ENVIRONMENT:
            return earlier
    return name


# Scope env keys charmer compiles that the earlier release never did (its
# pinned client knows none of them): a scope handed back leaves them out.
LATER_ENV_KEYS = frozenset({scope.WORKFLOW_MODEL_ENV, scope.AUTO_MODE_MODEL_ENV})


def _earlier_launch_items(settings: dict[str, Any], renamed: Mapping[str, Any]) -> dict[str, Any]:
    """``settings.json`` with its launch-time items (the keys the client reads
    once at start, which the record's ``launch_fence`` proves and a repair
    keeps) as the earlier release compiles them: ``availableModels`` (sorted)
    and ``model`` under the earlier selector names, each ``modelPicker``
    option's model under its earlier name and its description under the
    earlier product name, and the lifecycle ``env`` keys under their earlier
    names (values kept, a selector value under its earlier name), without
    the :data:`LATER_ENV_KEYS`. Every other key stays; the earlier release's
    repair recompiles those."""

    updated = dict(settings)
    available = settings.get("availableModels")
    if isinstance(available, list) and all(isinstance(item, str) for item in available):
        updated["availableModels"] = sorted(earlier_selector(renamed, item) for item in available)
    if "model" in settings:
        updated["model"] = earlier_selector(renamed, settings["model"])
    picker = settings.get("modelPicker")
    if isinstance(picker, dict) and isinstance(picker.get("options"), list):
        options = []
        for option in picker["options"]:
            if isinstance(option, dict):
                option = _earlier_names(option, renamed)
                description = option.get("description")
                if isinstance(description, str) and description.startswith(PICKER_PREFIX):
                    option["description"] = EARLIER_PICKER_PREFIX + description.removeprefix(PICKER_PREFIX)
            options.append(option)
        updated["modelPicker"] = {**picker, "options": options}
    env = settings.get("env")
    if isinstance(env, dict):
        updated["env"] = {_earlier_env_name(key): earlier_selector(renamed, value) for key, value in env.items()
                          if key not in LATER_ENV_KEYS}
    return updated


def _charmer_compiled(layout: Layout, settings: Any) -> bool:
    """A scope whose hooks name charmer's hook or token shims: charmer
    compiled it after the move."""

    return isinstance(settings, dict) and any(path.parent == layout.state / "bin" and path.name in CHARMER_SHIMS
                                              for path in scope.hook_targets(settings))


def _restore_sessions(layout: Layout) -> list[str]:
    """Every session charmer resumed or first started, in the earlier
    release's own names (from the earlier state root):

    - its record names each selector under the earlier name again (the
      earlier catalog serves no other), its ``applied_hash`` following its
      ``applied`` (only when it matched before: a record that did not is
      never vouched for);
    - when charmer compiled its scope, the launch-time items the record's
      ``launch_fence`` proves (:func:`_earlier_launch_items` and the
      ``lead-set.json`` rows) are the earlier release's form, and the fence
      proves them in turn (only when it proved charmer's): the earlier
      repair keeps proven launch items, so they must be its own.

    Each file is written as charmer writes it (canonical JSON), the record
    first (a rerun recognises the fence it wrote); a file that names none,
    or cannot be read, keeps its bytes. The session ids rewritten."""

    records_dir, scopes_dir = layout.legacy_state / "sessions", layout.legacy_state / "scopes"
    ids = set()
    for directory, suffix in ((records_dir, ".json"), (scopes_dir, "")):
        if os.path.isdir(directory) and not os.path.islink(directory):
            ids.update(name.removesuffix(suffix) for name in os.listdir(directory)
                       if name.endswith(suffix) and sessions.UUID4.fullmatch(name.removesuffix(suffix)))
    if not ids:
        return []
    renamed = catalog.load_catalog(assets.default_asset_root(environ=layout.environ)).renamed
    rewritten: list[str] = []
    for mid in sorted(ids):
        record_path, live = records_dir / f"{mid}.json", scopes_dir / mid
        record = _json_file(record_path)
        settings = _json_file(live / scope.SETTINGS_RELPATH)
        lead_set = _read_bytes(live / scope.LEAD_SET_JSON)
        files: dict[str, bytes] = {}
        fence = None
        if _charmer_compiled(layout, settings) and lead_set is not None:
            lead_document = _json_file(live / scope.LEAD_SET_JSON)
            earlier_settings = _earlier_launch_items(settings, renamed)
            earlier_lead_set = (strict_json.canonical_file_bytes(_earlier_names(lead_document, renamed))
                                if isinstance(lead_document, dict) else lead_set)
            if earlier_settings != settings:
                files[scope.SETTINGS_RELPATH] = strict_json.canonical_file_bytes(earlier_settings)
            if earlier_lead_set != lead_set:
                files[scope.LEAD_SET_JSON] = earlier_lead_set
            proven = {sessions.launch_digest(settings, lead_set),
                      sessions.launch_digest(earlier_settings, earlier_lead_set)}
            if isinstance(record, dict) and record.get("launch_fence") in proven:
                fence = sessions.launch_digest(earlier_settings, earlier_lead_set)
        changed = False
        if isinstance(record, dict):
            restored = _earlier_names(record, renamed)
            applied = record.get("applied")
            if isinstance(applied, dict) and record.get("applied_hash") == strict_json.bundle_digest(applied):
                restored["applied_hash"] = strict_json.bundle_digest(restored["applied"])
            if fence is not None:
                restored["launch_fence"] = fence
            if restored != record:
                state.atomic_write(record_path, strict_json.canonical_file_bytes(restored))
                changed = True
        for name, data in files.items():
            state.atomic_write(live / name, data)
            changed = True
        if changed:
            rewritten.append(mid)
    return rewritten


def _charmer_scopes(layout: Layout) -> list[str]:
    """The sessions whose scope in the earlier state root still carries a
    charmer name in a field the earlier release reads: hooks or a token
    helper naming charmer's shims, charmer's folders in a permission, a
    ``CHARMER_`` variable, charmer's model picker text, or a selector under
    its charmer name (``settings.json`` and ``lead-set.json``)."""

    directory = layout.legacy_state / "scopes"
    if not os.path.isdir(directory) or os.path.islink(directory):
        return []
    renamed = catalog.load_catalog(assets.default_asset_root(environ=layout.environ)).renamed
    markers = [str(layout.state / "bin").encode(), CHARMER_ENV_PREFIX.encode(), PICKER_PREFIX.encode(),
               *(f"~/{root}/{layout.data.name}/".encode() for root in (".config", ".local/share", ".local/state"))]
    markers.extend(str(spec["selector"]).removesuffix(operator.SELECTOR_SUFFIX).encode()
                   for spec in renamed.values() if isinstance(spec, Mapping) and spec.get("selector"))
    found = []
    for entry in sorted(os.listdir(directory)):
        if not sessions.UUID4.fullmatch(entry):
            continue
        data = b"".join(_read_bytes(directory / entry / name) or b""
                        for name in (scope.SETTINGS_RELPATH, scope.LEAD_SET_JSON))
        if any(marker in data for marker in markers):
            found.append(entry)
    return found


def _repair_lines(output: Sequence[str]) -> list[str]:
    """The summary and the per-session skip and failure lines of the earlier
    repair (its last lines when it printed none of them)."""

    kept = [line.strip() for line in output if line.strip().startswith(("Repair-all:", "- "))]
    return kept or [line.strip() for line in list(output)[-5:] if line.strip()]


def _repair_earlier(journal: Journal, layout: Layout, seams: Seams, report: Report) -> None:
    """The last step of ``--back``. When a session scope in the earlier
    state root still carries a charmer name (:func:`_charmer_scopes`: its
    hooks name charmer's shims, which are gone, or its permissions name
    charmer's folders), or a session was handed back
    (:func:`_restore_sessions`), the retained earlier launcher's own
    :data:`REPAIR` runs once and recompiles every scope item its launch
    fence does not prove in the earlier release's own format, so the
    earlier doctor finds every session as it left it. The journal is marked
    reverted either way (the roots are back); a failed repair, or a scope
    that still carries a charmer name, raises with the fix."""

    stale = _charmer_scopes(layout)
    outcome: dict[str, Any] | None = None
    if stale or journal.document.get("sessions_restored"):
        ok, output = seams.repair(dict(layout.environ))
        left = _charmer_scopes(layout)
        outcome = {"command": REPAIR, "ok": bool(ok), "recompiled": [mid for mid in stale if mid not in left],
                   "left": left, "output": _repair_lines(output)}
    journal.document["repair"] = outcome
    journal.document["status"] = REVERTED
    journal.document["done"] = []
    journal.document["error"] = None
    failed: MigrationError | None = None
    if outcome is not None and outcome["left"]:
        left = outcome["left"]
        message = (f"the roots are back, but {len(left)} session scope(s) still carry a charmer name after "
                   f"{REPAIR} ({', '.join(mid[:8] for mid in left)})")
        failed = MigrationError(
            message, lines=[message, *outcome["output"]],
            remedy=f"{legacy.NAME} doctor --repair {left[0]}" + (" (and each other one)" if len(left) > 1 else "")
            + f"; if {legacy.NAME} doctor still names it, resume it once: {legacy.NAME} -r {left[0]}")
    elif outcome is not None and not outcome["ok"]:
        message = f"the roots are back, but {REPAIR} reported a failure"
        failed = MigrationError(message, remedy=f"fix what it names, then: {REPAIR}",
                                lines=[message, *outcome["output"]])
    if failed is not None:
        journal.document["error"] = str(failed)
    _write_journal(journal, seams)
    if failed is not None:
        raise failed
    if outcome is not None:
        report.lines.append(f"{legacy.NAME} recompiled {len(outcome['recompiled'])} session scope(s) charmer had "
                            f"compiled ({REPAIR})")


def _undo_gateway(layout: Layout, seams: Seams) -> None:
    """charmer's unit files stopped, disabled and removed; a unit the
    service manager does not stop and disable refuses before its file goes."""

    reload = False
    for name, path in reversed(layout.charmer_units()):
        if os.path.lexists(path) and systemd_unit.written_here(_first_line(path) or ""):
            if not linux_service.disable_now(name, seams.runner):
                raise MigrationError(f"the service manager did not stop and disable {name}; nothing was moved back",
                                     remedy=f"check it: {linux_service.hint('status', unit=name)}; then: {BACK}")
            os.unlink(path)
            reload = True
    if reload and not linux_service.daemon_reload(seams.runner):
        raise MigrationError("the service manager did not reload its unit files",
                             remedy=f"{linux_service.RELOAD_HINT}, then: {BACK}")


def _undo_launchers(journal: Journal, layout: Layout) -> None:
    removed = journal.document.get("launchers_removed") or {}
    for name in legacy.LEGACY_LAUNCHERS:
        data = _read_bytes(journal.saved("launchers", name))
        target = layout.bin_dir / name
        if data is not None and not os.path.lexists(target):
            mode = removed.get(name) if isinstance(removed, dict) else None
            _put_bytes(target, data, mode if isinstance(mode, int) else 0o755)


def _undo_shims(journal: Journal, layout: Layout) -> None:
    for root in (layout.state, layout.legacy_state):
        bin_dir = root / "bin"
        if not os.path.isdir(bin_dir):
            continue
        for name in legacy.LEGACY_SHIMS:
            data = _read_bytes(journal.saved("shims", name))
            if data is not None and not os.path.lexists(bin_dir / name):
                state.atomic_write(bin_dir / name, data)
                os.chmod(bin_dir / name, 0o700)
        for name in CHARMER_SHIMS:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(bin_dir / name)


def _undo_authorities(journal: Journal, layout: Layout, plan: Sequence[Move]) -> None:
    """The saved bytes of each rewritten file, back in the directory that
    holds the moved gateway data, before the roots move back: charmer's
    gateway directory while the checked reverse plan still moves it (or the
    root holding it) back, an empty earlier directory waiting there
    included. Once that data is back it was restored before it moved;
    nothing was rewritten while it had not moved."""

    holding = any(move.target == layout.gateway or move.target in layout.gateway.parents for move in plan)
    directory = layout.gateway if holding and os.path.isdir(layout.gateway) else None
    if directory is not None:
        for name in AUTHORITY_FILES:
            data = _read_bytes(journal.saved("authorities", name))
            if data is not None and _read_bytes(directory / name) != data:
                state.atomic_write(directory / name, data)
    receipt = _read_bytes(journal.saved("authorities", RECEIPT_SAVED))
    if receipt is not None and os.path.isdir(layout.install):
        state.atomic_write(install_receipt.receipt_path(layout.install), receipt)


def _undo_roots(plan: Sequence[Move]) -> None:
    """The renames of a checked reverse plan (:func:`_reverse_plan`)."""

    for move in plan:
        if os.path.lexists(move.source):
            os.rmdir(move.source)  # the plan's empty re-created directory
        move.source.parent.mkdir(parents=True, exist_ok=True)
        os.rename(move.target, move.source)


def _undo_units(journal: Journal, layout: Layout, seams: Seams) -> None:
    """The earlier unit files from their saved bytes (never re-rendered),
    then the earlier gateway enabled and started as it was."""

    units = journal.document.get("units", {})
    present = [(name, path) for name, path in layout.legacy_units() if units.get(name, {}).get("present")]
    for name, path in present:
        data = _read_bytes(journal.saved("units", path.name))
        if data is not None and _read_bytes(path) != data:
            mode = units[name].get("mode")
            _put_bytes(path, data, mode if isinstance(mode, int) else 0o644)
    if present and not linux_service.daemon_reload(seams.runner):
        raise MigrationError("the service manager did not reload its unit files",
                             remedy=f"{linux_service.RELOAD_HINT}, then: {BACK}")
    for name, _path in present:
        recorded = units[name]
        # Enablement as it was (a runtime enablement as a runtime one, a
        # disabled unit stays disabled), then started when it was enabled or
        # running: the earlier gateway runs again as it would have.
        if recorded.get("enabled") and not linux_service.enable(
                name, seams.runner, runtime=recorded.get("unit_file_state") == "enabled-runtime"):
            raise MigrationError(f"the service manager did not enable {name}",
                                 remedy=f"check it: {linux_service.hint('status', unit=name)}; then: {BACK}")
        if (recorded.get("enabled") or recorded.get("active")) and not linux_service.start(name, seams.runner):
            raise MigrationError(f"the service manager did not start {name}",
                                 remedy=f"check it: {linux_service.hint('status', unit=name)}; then: {BACK}")


# ------------------------------------------------------------ purge

@dataclass(frozen=True)
class Removal:
    """One thing ``--purge`` removes: the whole tree (``recursive``), or the
    directory alone, and only while it is empty then."""

    path: Path
    recursive: bool


def purge_targets(layout: Layout) -> list[Removal]:
    """What ``--purge`` removes: the retained earlier install tree and the
    earlier caches, each whole; then, only while they are empty directories
    (``rmdir``: a file something wrote there after the move, such as a
    sign-in, stays and is named), the earlier Nix selection directory, the
    earlier data root and the earlier state and configuration roots. Nothing
    else: every other directory is untouched by construction, and
    :func:`purge_problems` refuses a target that is, lies in or holds Claude
    Code's own directory or one of charmer's roots."""

    removals = [Removal(layout.legacy_install, True), *(Removal(path, True) for path in layout.legacy_cache_dirs()),
                Removal(layout.legacy_data / KEPT_DATA[1], False), Removal(layout.legacy_data, False),
                *(Removal(root, False) for root in layout.legacy_roots())]
    seen: set[Path] = set()
    found = []
    for removal in removals:
        if removal.path not in seen and os.path.lexists(removal.path):
            seen.add(removal.path)
            found.append(removal)
    return found


_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# The folders on the way to a purge target are opened for traversal only
# (``O_PATH``, Linux): their descriptors serve as the base of the next step,
# of ``fstat`` and of the removal, and a search-only folder (mode 0711, as on
# some multi-user hosts) is passable. macOS has no ``O_PATH``: there each
# folder on the way is opened for reading, so a search-only one refuses the
# purge. With ``O_NOFOLLOW`` a link on the way still fails (ENOTDIR with
# ``O_PATH``, ELOOP without). A target directory itself is opened for
# reading: its contents are listed.
_WALK_DIRECTORY = (getattr(os, "O_PATH", 0) or os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _identity_of(path: Path) -> tuple[int, int] | None:
    try:
        return _identity(os.stat(path))
    except OSError:
        return None


def _protected(layout: Layout) -> list[Path]:
    """The roots the overlap check names, resolved: Claude Code's own
    directory (``~/.claude`` and ``CLAUDE_CONFIG_DIR``) and charmer's roots.
    ``--purge`` removes only its listed targets (:func:`purge_targets`), so
    every other directory is untouched by construction; this check only
    refuses a target that is, lies in or holds one of these."""

    roots = [layout.home / ".claude", layout.state, layout.config, layout.gateway, layout.data]
    configured = layout.environ.get("CLAUDE_CONFIG_DIR")
    if configured and os.path.isabs(configured):
        roots.append(Path(configured))
    return [Path(os.path.realpath(root)) for root in dict.fromkeys(roots)]


def _selected_key_files(layout: Layout) -> tuple[list[Path], list[str]]:
    """Every key file charmer can use, as its path is written (``~/``
    expanded, nothing else resolved): the key-file pointer's path (read
    leniently), the environment override, the effective selection
    (override, pointer or the default) and the supervised gateway's (its
    unit drops the override: the pointer or the default). Also why a
    selection cannot be told (an unreadable pointer: the file it selects is
    unknown). No key file is opened."""

    named: list[Path] = []
    problems: list[str] = []

    def spelled(item: str) -> Path:
        return layout.home / item[2:] if item.startswith("~/") else Path(item)

    document = _json_file(layout.gateway / SECRET_POINTER)
    text = document.get("path") if isinstance(document, dict) else None
    if isinstance(text, str) and text:
        named.append(spelled(text))
    override = layout.environ.get(secret_store.SECRET_ENV_OVERRIDE)
    if override:
        named.append(spelled(override))
    for select in (secret_store.key_file_location, secret_store.service_key_file):
        try:
            named.append(Path(select(layout.environ).path))
        except (OSError, ValueError, errors.CharmerError) as exc:
            problems.append(f"charmer's gateway key file selection cannot be read ({exc}); --purge cannot tell "
                            "which file it selects")
    return list(dict.fromkeys(named)), list(dict.fromkeys(problems))


def _key_problems(layout: Layout, held: Sequence["Held"]) -> list[str]:
    """The rule for the key selection: it must be a plain absolute path,
    without a ``..`` component or a symbolic link anywhere on it (each
    component from ``/`` down, read with ``lstat``), whose real location
    (``os.path.realpath``) lies outside every purge target. Inside is
    decided by identity, never by how a path is spelled: the real location
    and each directory above it, ``lstat`` from ``/`` down, against what
    each held target's descriptor is. Anything else refuses the purge."""

    selection, problems = _selected_key_files(layout)
    targets = {}
    for item in held:
        if item.directory is not None:
            targets[_identity(os.fstat(item.directory))] = item
        elif item.entry is not None:
            targets[_identity(item.entry)] = item
    for selected in selection:
        shown = layout.show(selected)
        if not selected.is_absolute():
            problems.append(f"charmer's gateway key file {shown} is not selected by an absolute path")
            continue
        if ".." in selected.parts:
            problems.append(f"charmer's gateway key file {shown} is selected through a .. component")
            continue
        linked = None
        walked = Path(selected.parts[0])
        for part in selected.parts[1:]:
            walked = walked / part
            try:
                info = os.lstat(walked)
            except FileNotFoundError:
                break
            except OSError:
                linked = walked
                break
            if stat.S_ISLNK(info.st_mode):
                linked = walked
                break
        if linked is not None:
            problems.append(f"charmer's gateway key file {shown} is selected through the link {layout.show(linked)}")
            continue
        real = Path(os.path.realpath(selected))
        walked = Path(real.parts[0])
        inside: list[Held] = []
        for part in real.parts[1:]:
            walked = walked / part
            try:
                found = targets.get(_identity(os.lstat(walked)))
            except OSError:
                break
            if found is not None:
                inside.append(found)
        if inside:
            # The tree that is deleted (the deepest whole-tree target), not
            # an emptied folder around it.
            found = next((item for item in reversed(inside) if item.removal.recursive), inside[-1])
            problems.append(f"charmer's gateway key file {shown} is in {layout.show(found.removal.path)}, which "
                            "--purge removes")
    return problems


@dataclass
class Held:
    """A purge target as its check found it, and the descriptors its removal
    uses: the directory holding it, reached from ``/`` one directory at a
    time without following a link, and the target's own when it is a
    directory. ``where`` is its path as written (no link was followed to
    reach it, so it is where it really is); ``chain`` the identity of every
    directory on the way."""

    removal: Removal
    where: Path
    parent: int | None = None
    entry: os.stat_result | None = None
    directory: int | None = None
    chain: tuple[tuple[int, int], ...] = ()
    problem: str | None = None

    def close(self) -> None:
        for descriptor in (self.directory, self.parent):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        self.directory = self.parent = None


def _hold(layout: Layout, removal: Removal) -> Held:
    """Open ``removal``'s way from ``/``, one component at a time, never
    following a link (``O_NOFOLLOW``): a component that is a link (HOME's,
    an XDG folder's or any other) or not a directory refuses, and so does a
    path that is not absolute or holds ``..``. Nothing is resolved."""

    path = Path(removal.path)
    shown = layout.show(removal.path)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        return Held(removal, path, problem=f"{shown} is not a plain absolute path; --purge removes nothing by it")
    try:
        descriptor = os.open(path.parts[0], _WALK_DIRECTORY)
    except OSError as exc:
        return Held(removal, path, problem=f"{path.parts[0]} cannot be opened ({exc.strerror or exc})")
    held = Held(removal, path, parent=descriptor, chain=(_identity(os.fstat(descriptor)),))
    walked = Path(path.parts[0])
    for part in path.parts[1:-1]:
        walked = walked / part
        try:
            step = os.open(part, _WALK_DIRECTORY, dir_fd=held.parent)
        except FileNotFoundError:
            held.close()
            return held  # gone since it was listed
        except OSError as exc:
            held.close()
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                held.problem = (f"{layout.show(walked)} is a link (or not a folder) above {shown}; --purge never "
                                "follows a link to what it removes")
            else:
                held.problem = f"{layout.show(walked)} cannot be opened ({exc.strerror or exc})"
            return held
        os.close(held.parent)
        held.parent = step
        held.chain += (_identity(os.fstat(step)),)
    name = path.parts[-1]
    try:
        held.entry = os.stat(name, dir_fd=held.parent, follow_symlinks=False)
    except FileNotFoundError:
        return held
    if stat.S_ISDIR(held.entry.st_mode):
        try:
            held.directory = os.open(name, _OPEN_DIRECTORY, dir_fd=held.parent)
        except OSError as exc:
            held.problem = f"{shown} cannot be opened ({exc.strerror or exc})"
            return held
        if _identity(os.fstat(held.directory)) != _identity(held.entry):
            held.problem = f"{shown} changed while --purge checked it"
    return held


@contextlib.contextmanager
def _held(layout: Layout, removals: Sequence[Removal]) -> Iterator[list[Held]]:
    """Every target held (:func:`_hold`) for as long as the block runs: its
    check and its removal use the same descriptors."""

    held: list[Held] = []
    try:
        for removal in removals:
            held.append(_hold(layout, removal))
        yield held
    finally:
        for item in held:
            item.close()


def _held_problems(layout: Layout, held: Sequence[Held]) -> list[str]:
    """Why the held targets must not go (none: they may): what the walk
    refused (:func:`_hold`), a directory on the way to one, or the target
    itself, that is a protected root (by identity or by its path), a
    protected root inside a target, and the key selection's rule
    (:func:`_key_problems`)."""

    problems = [item.problem for item in held if item.problem]
    present = [item for item in held if item.problem is None and item.entry is not None]
    for root in _protected(layout):
        identity = _identity_of(root)
        above = {found for found in map(_identity_of, root.parents) if found is not None}
        for item in present:
            own = _identity(item.entry) if item.entry is not None else None
            if (item.where == root or root in item.where.parents or item.where in root.parents
                    or (identity is not None and (identity in item.chain or identity == own))
                    or (item.directory is not None and own in above)):
                problems.append(f"{layout.show(item.removal.path)} leads to {layout.show(root)}, which --purge "
                                "never touches")
    problems.extend(_key_problems(layout, held))
    return list(dict.fromkeys(problems))


def purge_problems(layout: Layout, removals: Sequence[Removal]) -> list[str]:
    """Why ``removals`` must not go (each with its reason; none: they may),
    checked on the targets as a walk without links reaches them
    (:func:`_held_problems`)."""

    with _held(layout, removals) as held:
        return _held_problems(layout, held)


def purge_listing(layout: Layout, removals: Sequence[Removal]) -> list[str]:
    """One line per target for the question: a tree as it is, a folder to
    remove only while empty so marked, and a file or link standing where
    such a folder was as kept (it is never removed)."""

    lines = []
    for removal in removals:
        shown = layout.show(removal.path)
        if removal.recursive:
            lines.append(shown)
            continue
        try:
            info = os.lstat(removal.path)
        except OSError:
            lines.append(f"{shown} (only if nothing is left in it)")
            continue
        if stat.S_ISDIR(info.st_mode):
            lines.append(f"{shown} (only if nothing is left in it)")
        else:
            lines.append(f"{shown} ({'a link' if stat.S_ISLNK(info.st_mode) else 'a file'}, kept)")
    return lines


def purge_refusal(problems: Sequence[str], layout: Layout | None = None) -> MigrationError:
    # The fix for the problem the refusal leads with; every problem is listed.
    keyed = "key file" in problems[0]
    linked = "never follows a link" in problems[0]
    data = layout.show(layout.legacy_data) if layout is not None else "the earlier data folder"
    return MigrationError(f"{problems[0]}; nothing was removed", lines=problems,
                          remedy=(f"select the key file by a plain path outside {data} (charmer setup --keys-file "
                                  "PATH), then run --purge again" if keyed else
                                  "remove that folder yourself where the link leads (check what it holds first), "
                                  "or put the real folder in the link's place, then run --purge again" if linked else
                                  "point that path back to where it belongs (it must not lead into a protected "
                                  "directory), then run --purge again"))


def purge(environ: Mapping[str, str], seams: Seams, *, confirmed: Sequence[Path] | None = None) -> Report:
    """Remove the retained earlier installation after a completed migration,
    under the transaction lock (both install locks): the journal, the units,
    the targets and their safety are read under it and it is held through
    the last removal and the journal write. Each target is checked and
    removed through the same descriptors (:func:`_held`), nothing resolved
    again in between. ``confirmed``: the targets the person agreed to, which
    must still be exactly what would be removed."""

    layout = Layout.of(environ)
    with _install_locks(layout):
        journal = locate(layout)
        if journal is None or journal.status != DONE or journal.root != layout.state:
            raise MigrationError("no completed migration was found; nothing was removed",
                                 remedy=f"migrate first: {COMMAND}")
        if journal.document.get("purged"):
            raise MigrationError("the earlier installation was already purged; nothing was removed")
        remaining = [layout.show(path) for _name, path in layout.legacy_units() if os.path.lexists(path)]
        if remaining:
            raise MigrationError(f"{remaining[0]} still exists; nothing was removed",
                                 remedy="move that unit file away, then retry")
        removals = purge_targets(layout)
        with _held(layout, removals) as held:
            problems = _held_problems(layout, held)
            if problems:
                raise purge_refusal(problems, layout)
            if confirmed is not None and [removal.path for removal in removals] != list(confirmed):
                raise MigrationError("what --purge would remove changed after it was shown; nothing was removed",
                                     remedy=f"run it again: {PURGE}")
            report = Report()
            for item in held:
                report.lines.append(_remove(layout, item))
            journal.document["purged"] = _now_text(seams)
            _write_journal(journal, seams)
    return report


def _remove(layout: Layout, held: Held) -> str:
    """Remove a held target through its descriptors, never following a
    link. A whole-tree target: a link or file standing there is unlinked
    from the directory checked to hold it, a tree's contents are removed
    through the tree's own descriptor (``shutil.rmtree`` on directory
    descriptors), then the emptied directory from the held parent. Any
    other target goes only while it is an empty directory (``rmdir``);
    anything else there, a file or a link included, is kept and named. What
    stands at the name must still be what the check found. One report line."""

    shown = layout.show(held.removal.path)
    if held.parent is None or held.entry is None:
        return f"{shown} is already gone"
    name = held.where.name
    try:
        now = os.stat(name, dir_fd=held.parent, follow_symlinks=False)
    except FileNotFoundError:
        return f"{shown} is already gone"
    if _identity(now) != _identity(held.entry):
        raise MigrationError(f"{shown} changed while --purge ran; it and what follows it were not removed",
                             remedy=f"check it, then: {PURGE}")
    if held.directory is None:
        if not held.removal.recursive:
            kind = "a link" if stat.S_ISLNK(now.st_mode) else "a file"
            return (f"kept {shown}: it is {kind}, not the folder the earlier installation left (check it; remove "
                    "it yourself)")
        os.unlink(name, dir_fd=held.parent)
        return f"removed {shown}"
    if held.removal.recursive:
        for child in os.listdir(held.directory):
            info = os.stat(child, dir_fd=held.directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(child, dir_fd=held.directory)
            else:
                os.unlink(child, dir_fd=held.directory)
    try:
        os.rmdir(name, dir_fd=held.parent)
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
        children = sorted(os.listdir(held.directory))
        return (f"kept {shown}: it holds {', '.join(children[:5])}{' …' if len(children) > 5 else ''}, "
                "which the earlier installation did not leave for --purge (check it; remove it yourself)")
    return f"removed {shown}"


# ------------------------------------------------------------ reports

def dry_run(environ: Mapping[str, str], seams: Seams) -> tuple[list[str], bool]:
    """``(lines, ready)``: the preconditions, then every rename, rewrite,
    unit action and removal a run would make; names only, nothing written."""

    layout = Layout.of(environ)
    lines: list[str] = []
    journal = locate(layout)
    if journal is not None and journal.status in (RUNNING, REVERTING, DONE):
        lines.append(f"a migration is recorded ({journal.status}): {STATUS} shows it")
        return lines, journal.status == RUNNING
    checks = check_preconditions(layout, seams)
    lines.append("Preconditions:")
    for check in checks:
        if check.ok:
            lines.append(f"  ok  {check.name}")
        else:
            lines.append(f"  NO  {check.name}: {check.problem}" + (f" (fix: {check.remedy})" if check.remedy else ""))
    if not layout.legacy_present():
        return lines, False
    units = [(name, path) for name, path in layout.legacy_units() if os.path.lexists(path)]
    lines.append("Gateway units:")
    for name, path in units:
        lines.append(f"  stop and disable {name}; its file {layout.show(path)} is saved for --back, then removed")
    lines.append(f"  install, enable and start {CHARMER_UNIT}; its start renders the configuration again; wait "
                 "until it is ready on the recorded endpoint"
                 if units or (_endpoint_document(layout.legacy_gateway) or {}).get("backend") == endpoint.SYSTEMD
                 else "  no supervised gateway service: the gateway keeps starting on demand")
    lines.append("Moves (rename on one file system; every file moves as it is, keys and sign-ins unopened):")
    for move in plan_moves(layout):
        lines.append(f"  {layout.show(move.source)} -> {layout.show(move.target)}")
    for root, kind in dict.fromkeys(((layout.state, "state"), (layout.config, "config"), (layout.gateway, "config"))):
        removable, foreign = skeleton(root, kind)
        if removable and not foreign:
            lines.append(f"  charmer's empty {layout.show(root)} is removed first ({len(removable)} file(s))")
    lines.append("Rewrites (one value each; every other byte stays):")
    document = _json_file(layout.legacy_gateway / continuity.FILE_NAME)
    if isinstance(document, dict) and isinstance(document.get("state_root"), str):
        lines.append(f"  {layout.show(layout.gateway / continuity.FILE_NAME)}: state_root")
    document = _json_file(layout.legacy_gateway / SECRET_POINTER)
    if isinstance(document, dict) and isinstance(document.get("path"), str):
        selected = (layout.home / document["path"][2:] if document["path"].startswith("~/")
                    else Path(document["path"]))
        moved = layout.mapped(selected) if selected.is_absolute() else None
        lines.append(f"  {layout.show(layout.gateway / SECRET_POINTER)}: "
                     + ("path (its key file moves)" if moved is not None else "unchanged (its key file stays)"))
    if (_endpoint_document(layout.legacy_gateway) or {}).get("unit") == legacy.LEGACY_UNIT:
        lines.append(f"  {layout.show(layout.gateway / endpoint.ENDPOINT_FILE)}: unit {CHARMER_UNIT}")
    taken = _path_lines_to_take(layout)
    if taken:
        lines.append(f"  {layout.show(install_receipt.receipt_path(layout.install))}: takes over the earlier "
                     f"installer's PATH line(s) in {', '.join(sorted({layout.show(item.file) for item in taken}))}")
    lines.append("Shims:")
    for name in legacy.LEGACY_SHIMS:
        if os.path.lexists(layout.legacy_state / "bin" / name):
            lines.append(f"  remove bin/{name}")
    lines.append(f"  write {', '.join(f'bin/{name}' for name in CHARMER_SHIMS)}; compiled scopes are recompiled at "
                 "their next resume")
    lines.append("Removals:")
    for name in legacy.LEGACY_LAUNCHERS:
        path = layout.bin_dir / name
        if os.path.lexists(path):
            lines.append(f"  {layout.show(path)}" + ("" if launcher_owned(path)
                                                     else " stays: the earlier installer did not write it"))
    lines.append("Kept for --back (--purge removes it later):")
    for name in KEPT_DATA:
        if os.path.lexists(layout.legacy_data / name):
            lines.append(f"  {layout.show(layout.legacy_data / name)}")
    notes = report_only(layout, state_root=layout.legacy_state, config=layout.legacy_config,
                        gateway=layout.legacy_gateway, data=layout.legacy_data)
    if notes:
        lines.append("Left as they are (for you to check):")
        lines.extend(f"  {finding} ({remedy})" for finding, remedy in notes)
    return lines, all(check.ok for check in checks)


def status_lines(environ: Mapping[str, str]) -> list[str]:
    layout = Layout.of(environ)
    journal = locate(layout)
    if journal is None:
        found = (f"an installation of {legacy.NAME} exists" if layout.legacy_present()
                 else f"no installation of {legacy.NAME} exists")
        return [f"no migration is recorded; {found}"]
    lines = [f"migration: {journal.status} ({layout.show(journal_path(journal.root))})",
             f"  started {journal.document.get('started')}, updated {journal.document.get('updated')}"]
    if journal.status in (RUNNING, DONE):
        for step in STEPS:
            lines.append(f"  {'done' if step in journal.done else 'todo'}  {step_text(step, journal)}")
    if journal.document.get("error"):
        lines.append(f"  stopped: {journal.document['error']}")
    for problem in journal.document.get("verify") or ():
        lines.append(f"  doctor: {problem}")
    repair = journal.document.get("repair")
    if journal.status == REVERTED and isinstance(repair, dict):
        left = repair.get("left") or []
        lines.append(f"  {REPAIR}: " + (f"left {len(left)} session scope(s) with a charmer name" if left
                                         else "failed" if not repair.get("ok")
                                         else f"recompiled {len(repair.get('recompiled') or [])} session scope(s)"))
    if journal.document.get("purged"):
        lines.append(f"  the earlier installation was purged {journal.document['purged']}")
    elif journal.status == DONE and os.path.lexists(layout.legacy_data):
        lines.append(f"  {layout.show(layout.legacy_data)} is kept for {BACK}; {PURGE} removes it")
    return lines


# ------------------------------------------------------------ doctor and first run

def remnants(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """What an earlier installation left on this account, ``(finding,
    remedy)`` each, for doctor's Attention (names only; nothing is changed).
    An account that never had one costs a few ``lstat`` calls."""

    layout = Layout.of(environ)
    try:
        journal = locate(layout)
    except MigrationError as exc:
        return [(str(exc), exc.remedy or STATUS)]
    completed = journal is not None and journal.status == DONE
    found: list[tuple[str, str]] = []
    roots = [layout.show(root) for root in layout.legacy_roots() if os.path.lexists(root)]
    if roots and completed:
        found.append((f"{legacy.NAME} roots exist again after the migration ({', '.join(roots)}); charmer does not "
                      "read them", "move them aside"))
    elif roots:
        found.append((f"an installation of {legacy.NAME} exists ({', '.join(roots)}); charmer does not read it",
                      f"{DRY_RUN}, then {COMMAND}"))
    if completed and not journal.document.get("purged") and os.path.lexists(layout.legacy_data):
        found.append((f"the earlier installation is kept for {BACK} ({layout.show(layout.legacy_data)})", PURGE))
    launchers = [layout.show(layout.bin_dir / name) for name in legacy.LEGACY_LAUNCHERS
                 if os.path.lexists(layout.bin_dir / name)]
    if launchers:
        found.append((f"{legacy.NAME} launchers remain: {', '.join(launchers)}",
                      "remove them; they start the earlier release" if completed else COMMAND))
    units = [layout.show(path) for _name, path in layout.legacy_units() if os.path.lexists(path)]
    if units:
        found.append((f"{legacy.NAME} gateway unit files remain: {', '.join(units)}",
                      f"remove them, then {linux_service.RELOAD_HINT}" if completed else COMMAND))
    caches = [layout.show(path) for path in layout.legacy_cache_dirs() if os.path.lexists(path)]
    if caches:
        found.append((f"{legacy.NAME} caches remain: {', '.join(caches)}",
                      PURGE if completed else "remove them once the earlier release is no longer built here"))
    link = layout.home / legacy.LEGACY_NIX_RELEASE_LINK
    if os.path.lexists(link):
        found.append((f"{legacy.NAME}'s Nix release link remains: {layout.show(link)}",
                      "remove that Nix installation through your Nix setup"))
    if completed:
        found.extend(item for item in report_only(layout, state_root=layout.state, config=layout.config,
                                                  gateway=layout.gateway, data=layout.data)
                     if not item[0].startswith("operator exports"))
    return found


def fresh_with_legacy(environ: Mapping[str, str]) -> bool:
    """An earlier installation exists, no migration started, and charmer's
    own roots hold nothing yet (the first-run hint and setup refusal)."""

    layout = Layout.of(environ)
    if not layout.legacy_present():
        return False
    try:
        journal = locate(layout)
    except MigrationError:
        return False
    if journal is not None and journal.status != REVERTED:
        return False
    return all(not skeleton(root, kind, first=True)[1]
               for root, kind in ((layout.state, "state"), (layout.config, "config"), (layout.gateway, "config")))


FRESH_START_GUIDE = "the migration guide, Start fresh instead"


def fresh_start(layout: Layout) -> str:
    """The way to set charmer up fresh beside an earlier installation that
    cannot move (a Nix one, one without a release bundle or state): its
    folders moved aside, kept, so charmer's setup no longer finds them."""

    roots = [layout.show(root) for root in layout.legacy_roots() if os.path.lexists(root)]
    return (f"start fresh instead: retire the earlier installation and stop its gateway (no process of it may "
            f"run), rename {', '.join(roots) or 'its folders'} aside (keep them: they hold its data), then charmer "
            f"setup ({FRESH_START_GUIDE})")


def first_run_hint(environ: Mapping[str, str] | None = None) -> str:
    hint = (f"an installation of {legacy.NAME} exists on this account; move it to charmer with `{COMMAND}` "
            f"(preview: `{DRY_RUN}`) instead of setting charmer up fresh")
    if environ is None:
        return hint
    return f"{hint}; when it cannot move (the preview says why), {fresh_start(Layout.of(environ))}"
