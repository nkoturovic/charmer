"""Best-effort, read-only inspection of mod manifests and declared module text.

A literal is not proof that a hook executes or that the plugin is active. No
imports are followed, no user code is run, and no installation index is assumed.
The bounded folder walk stops at a plugin manifest, never exploring its payload.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from charmer import paths

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_DEPTH = 8
MAX_MODULES = 16
MODULE_SUFFIXES = frozenset({".ts", ".tsx", ".jsx", ".js", ".mjs", ".cjs", ".mts", ".cts"})
COVERAGE = ("Scanned: session dev-mods, user skills, current-project skills, plugin store and synced folders, "
            "CLAUDE_CODE_PLUGIN_DIRS in this environment and user settings. "
            "Not scanned: plugin directories given on a client's command line (managed sessions refuse that flag), "
            "other projects, folder marketplaces elsewhere, "
            "imports, computed hook names, symlinks, and files or folders beyond the scan bounds. "
            "This check is not exhaustive and is not proof a mod is enabled or active.")
SPAWN_EFFECT = ("An agent.spawn hook may redirect spawns within the lineup fence; the exact per-agent binding "
                "is not guaranteed; other hook kinds are outside this check.")


@dataclass(frozen=True)
class Mod:
    folder: Path
    spawn: bool
    inspected: bool


@dataclass(frozen=True)
class Scan:
    mods: tuple[Mod, ...]
    incomplete: tuple[str, ...]
    coverage: str = COVERAGE


class _NotInspected(Exception):
    pass


def _directory(path: Path, root: Path) -> bool:
    """Refuse linked folders within the scan root, not its ancestors."""

    for part in (path, *path.parents):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(mode):
            raise _NotInspected("linked or non-directory location")
        if part == root:
            return True
    raise _NotInspected("folder outside the scan root")


def scan(home: Path | str, cwd: Path | str, environ: Mapping[str, str], *,
         plugin_dirs: Sequence[Path | str] = ()) -> Scan:
    """Only bounded regular UTF-8 files named by a plugin's manifests are read.
    ``plugin_dirs`` is explicit input, not a process or transcript search."""

    home, cwd = Path(os.path.realpath(home)), Path(os.path.realpath(cwd))
    claude = Path(os.path.realpath(paths.claude_settings_dir({**environ, "HOME": str(home)})))
    forbidden = (claude / "projects", Path(os.path.realpath(paths.native_projects(home, environ))))
    roots = [(claude / "dev-mods", 2), (claude / "skills", 1),
             (cwd / ".claude" / "skills", 1), (claude / "plugins" / "store", MAX_DEPTH),
             (claude / "plugins" / "synced", MAX_DEPTH)]
    incomplete: set[str] = set()
    mods: list[Mod] = []
    total_bytes = entries = 0

    def read(root: Path, target: Path) -> str:
        nonlocal total_bytes
        if (not target.is_relative_to(root)
                or any(target == denied or target.is_relative_to(denied) for denied in forbidden)
                or not _directory(target.parent, root)):
            raise _NotInspected("module outside the plugin or missing folder")
        descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            size = os.fstat(stream.fileno())
            if not stat.S_ISREG(size.st_mode):
                raise _NotInspected("non-regular file")
            remaining = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total_bytes)
            if remaining < 0 or size.st_size > remaining:
                raise _NotInspected("file or total read limit")
            data = stream.read(remaining + 1)
            total_bytes += len(data)
            if len(data) > remaining:
                raise _NotInspected("file or total read limit")
        text = data.decode("utf-8")
        if "\x00" in text:
            raise _NotInspected("non-text module")
        return text

    def extra_roots(value: object) -> None:
        if not isinstance(value, str):
            if value is not None:
                incomplete.add("invalid plugin directory setting")
            return
        if "\x00" in value:
            incomplete.add("invalid plugin directory setting")
            return
        for raw in value.split(os.pathsep):
            if not raw:
                continue
            path = home / raw[2:] if raw.startswith("~/") else Path(raw)
            if not path.is_absolute():
                incomplete.add("relative plugin directory setting")
                continue
            if len(roots) >= MAX_ENTRIES:
                incomplete.add("plugin directory limit")
                break
            roots.append((Path(os.path.normpath(path)), 0))

    extra_roots(environ.get("CLAUDE_CODE_PLUGIN_DIRS"))
    try:
        document = json.loads(read(claude, claude / "settings.json"))
        if isinstance(document, dict) and isinstance(document.get("env"), dict):
            extra_roots(document["env"].get("CLAUDE_CODE_PLUGIN_DIRS"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError, _NotInspected):
        # A missing settings directory is normal on a fresh installation.
        if os.path.lexists(claude / "settings.json"):
            incomplete.add("user settings not inspected")
    for path in plugin_dirs:
        extra_roots(str(path))

    def inspect(folder: Path) -> bool:
        """Whether a plugin boundary was found (even if unreadable)."""

        try:
            manifest = json.loads(read(folder, folder / ".claude-plugin" / "plugin.json"))
            if not isinstance(manifest, dict):
                raise _NotInspected("invalid plugin manifest")
        except FileNotFoundError:
            return False
        except (OSError, ValueError, RecursionError, _NotInspected):
            if os.path.lexists(folder / ".claude-plugin"):
                incomplete.add("plugin manifest not inspected")
                return True
            return False
        try:
            hooks = json.loads(read(folder, folder / "hooks" / "hooks.json"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError, RecursionError, _NotInspected):
            if os.path.lexists(folder / "hooks"):
                mods.append(Mod(folder, False, False))
            return True
        modules = hooks.get("modules", []) if isinstance(hooks, dict) else None
        if modules == []:
            return True
        if not isinstance(modules, list):
            mods.append(Mod(folder, False, False))
            return True
        spawn, inspected = False, len(modules) <= MAX_MODULES
        for name in modules[:MAX_MODULES]:
            try:
                if not isinstance(name, str) or Path(name).is_absolute():
                    raise _NotInspected("invalid module path")
                target = Path(os.path.normpath(folder / "hooks" / name))
                if target.suffix not in MODULE_SUFFIXES:
                    raise _NotInspected("unsupported module filename")
                spawn = "agent.spawn" in read(folder, target) or spawn
            except (OSError, ValueError, RecursionError, _NotInspected):
                inspected = False
        mods.append(Mod(folder, spawn, inspected))
        return True

    visited: set[Path] = set()
    for root, depth in roots:
        resolved = Path(os.path.realpath(root))
        if any(resolved == target or resolved.is_relative_to(target) for target in forbidden):
            incomplete.add("transcript location excluded")
            continue
        queue = [(root, depth)]
        while queue and entries < MAX_ENTRIES:
            folder, left = queue.pop()
            if folder in visited:
                continue
            visited.add(folder)
            try:
                if not _directory(folder, root):
                    continue
                found = inspect(folder)
                if found:
                    continue
                if left == 0:
                    if depth > 1:
                        incomplete.add("folder depth limit")
                    continue
                with os.scandir(folder) as children:
                    for child in children:
                        entries += 1
                        if entries >= MAX_ENTRIES:
                            incomplete.add("folder entry limit")
                            break
                        if child.name in (".git", "node_modules", ".claude-plugin", "hooks", "projects"):
                            continue
                        try:
                            if child.is_symlink():
                                incomplete.add("symlinked folder not inspected")
                            elif child.is_dir(follow_symlinks=False):
                                queue.append((Path(child.path), left - 1))
                        except OSError:
                            incomplete.add("folder entry unreadable")
            except (OSError, ValueError, _NotInspected):
                incomplete.add("folder unreadable or linked")
        if queue:
            incomplete.add("folder entry limit")
    return Scan(tuple(mods), tuple(sorted(incomplete)))
