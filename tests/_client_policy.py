"""Test-side helpers for the real-client policy checks.

``managed_settings`` puts a fixture folder at ``/etc/claude-code`` inside the
probe's network namespace for the runs started under it, so a check can
give the pinned client a managed-settings file without touching the host:
the sandbox mounts a private tmpfs at ``/etc``, binds every host ``/etc``
entry back into it read-only (a symlink is recreated as the same symlink)
and binds the folder read-only at ``/etc/claude-code``; nothing is written
to the host's ``/etc``. The client reads it as this machine's managed
policy (``CLAUDE_CODE_MANAGED_SETTINGS_PATH`` is not honoured by the pinned
client). The view uses only bwrap options every supported bubblewrap has
(``--tmpfs``, ``--ro-bind``, ``--symlink``; no overlay, which needs 0.10),
so it works on the release runner's bubblewrap 0.9 too.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from charmer import probe, state

MANAGED_FOLDER = "/etc/claude-code"


def etc_view(folder: Path | str, etc: Path | str = "/etc") -> list[str]:
    """bwrap arguments: ``etc`` rebuilt entry by entry over a tmpfs, with
    ``folder`` at ``/etc/claude-code`` (a host entry of that name is
    replaced). Sockets, fifos and devices directly in ``etc`` are left out."""

    argv = ["--tmpfs", "/etc"]
    with os.scandir(etc) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            target = f"/etc/{entry.name}"
            if target == MANAGED_FOLDER:
                continue
            if entry.is_symlink():
                argv += ["--symlink", os.readlink(entry.path), target]
            elif entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False):
                argv += ["--ro-bind", entry.path, target]
    return [*argv, "--ro-bind", str(folder), MANAGED_FOLDER]


@contextlib.contextmanager
def managed_settings(fixture: probe.ProbeFixture, document: dict[str, Any]) -> Iterator[Path]:
    """Real runs started inside the block read ``document`` as managed settings."""

    folder = state.ensure_private_dir(Path(fixture.root) / "managed-policy")
    state.atomic_write(folder / "managed-settings.json", json.dumps(document, sort_keys=True).encode("utf-8"))
    original = probe._isolation_prefix

    def prefix(bwrap: Path, cwd: Path | str | None, *, pid_namespace: bool = False) -> list[str]:
        argv = original(bwrap, cwd, pid_namespace=pid_namespace)
        at = argv.index("--dev-bind") + 3
        return [*argv[:at], *etc_view(folder), *argv[at:]]

    with mock.patch.object(probe, "_isolation_prefix", prefix):
        yield folder
