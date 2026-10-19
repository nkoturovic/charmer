"""Test-side helpers for the real-client policy checks.

``managed_settings`` puts a fixture folder at ``/etc/claude-code`` inside the
probe's network namespace for the runs started under it, so a check can
give the pinned client a managed-settings file without touching the host:
the sandbox overlays ``/etc`` with a private tmpfs upper layer (nothing is
written to the host's ``/etc``) and binds the folder read-only over
``/etc/claude-code``. The client reads it as this machine's managed policy
(``CLAUDE_CODE_MANAGED_SETTINGS_PATH`` is not honoured by the pinned client).
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from charmer import probe, state


@contextlib.contextmanager
def managed_settings(fixture: probe.ProbeFixture, document: dict[str, Any]) -> Iterator[Path]:
    """Real runs started inside the block read ``document`` as managed settings."""

    folder = state.ensure_private_dir(Path(fixture.root) / "managed-policy")
    state.atomic_write(folder / "managed-settings.json", json.dumps(document, sort_keys=True).encode("utf-8"))
    original = probe._isolation_prefix

    def prefix(bwrap: Path, cwd: Path | str | None, *, pid_namespace: bool = False) -> list[str]:
        argv = original(bwrap, cwd, pid_namespace=pid_namespace)
        at = argv.index("--dev-bind") + 3
        return [*argv[:at], "--overlay-src", "/etc", "--tmp-overlay", "/etc",
                "--ro-bind", str(folder), "/etc/claude-code", *argv[at:]]

    with mock.patch.object(probe, "_isolation_prefix", prefix):
        yield folder
