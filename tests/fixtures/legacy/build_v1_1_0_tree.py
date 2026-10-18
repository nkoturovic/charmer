#!/usr/bin/env python3
"""Recipe of ``v1.1.0-tree.json``: an installation of release 1.1.0 (the
product's earlier name), as that release wrote it, for the
``migrate from-legacy`` tests.

Run from the repository root (needs git and the ``v1.1.0`` tag)::

    python3 tests/fixtures/legacy/build_v1_1_0_tree.py

The bytes come from release 1.1.0 itself, never from today's code: the
tag's source tree is extracted (``git archive v1.1.0 src``) and its own
functions render the gateway unit files and write the hook and token
shims; the launcher files and the PATH line follow the tag's installer;
the session record and its compiled scope are the frozen 1.0.0 baseline
(``tests/fixtures/permissive/catalog-1.0.0.json``, the same v4 record
format 1.1.0 reads). Every absolute path is written with the placeholder
``@HOME@``, which the test helper (``tests/_legacy_tree.py``) replaces with
the test's HOME. The output is checked in and pinned by the name census;
rerun this only to reproduce it.
"""

from __future__ import annotations

import importlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from charmer import legacy  # noqa: E402  (names only: the bytes come from the tag)

TAG = "v1.1.0"
OUT = Path(__file__).resolve().parent / "v1.1.0-tree.json"
PLACEHOLDER = "@HOME@"
PACKAGE = legacy.SPELLINGS[1]  # the tag's import package
MID = "11111111-1111-4111-8111-111111111111"


def tag_text(path: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{TAG}:{path}"], check=True,
                          capture_output=True, text=True).stdout


def tag_source(directory: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", TAG, "src"], check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(directory, filter="data")
    return directory / "src"


def placeholder(text: str, home: str) -> str:
    return text.replace(home, PLACEHOLDER)


def main() -> int:
    files: dict[str, str] = {}
    modes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="legacy-tree-") as scratch:
        source = tag_source(Path(scratch) / "tag")
        sys.path.insert(0, str(source))
        for name in [name for name in sys.modules if name == PACKAGE or name.startswith(PACKAGE + ".")]:
            del sys.modules[name]
        scope = importlib.import_module(f"{PACKAGE}.scope")
        systemd_unit = importlib.import_module(f"{PACKAGE}.platform.systemd_unit")
        service = importlib.import_module(f"{PACKAGE}.service")
        home = Path(scratch) / "home"
        state_root = home / ".local" / "state" / legacy.LEGACY_DIR
        launcher = home / ".local" / "share" / legacy.LEGACY_DIR / "install" / "current" / "bin" / legacy.NAME
        # The hook and token shims, by the tag's own writers.
        scope.ensure_hook_shim(state_root, str(launcher))
        scope.ensure_hook_shim_v3(state_root, str(launcher))
        scope.ensure_gateway_token_shim(state_root, home / ".config" / legacy.LEGACY_DIR / "api-key", str(launcher))
        for shim in sorted((state_root / "bin").iterdir()):
            relative = f"state/bin/{shim.name}"
            files[relative] = placeholder(shim.read_text(), str(home))
            modes[relative] = "0700"
        # The supervised gateway's unit files, by the tag's own renderer.
        spec = json.loads((source / PACKAGE / "data" / "gateway-unit.json").read_text())
        plan = systemd_unit.UnitPlan(spec["unit"], spec["exec_links"]["bundle"], "/usr/bin/notify-send",
                                     service.failure_notice(spec["unit"]))
        for name, text in sorted(systemd_unit.render(spec, plan).items()):
            files[f"units/{name}"] = text
            modes[f"units/{name}"] = "0644"
    # The launchers and the PATH line, as the tag's installer writes them.
    installer = tag_text("packaging/install.sh")
    mark = re.search(r"^SHIM_MARK='([^']*)'$", installer, re.M).group(1)
    path_mark = re.search(r"^PATH_MARK='([^']*)'$", installer, re.M).group(1)
    names = re.search(r"^SHIMS='([^']*)'$", installer, re.M).group(1).split()
    install_root = f"{PLACEHOLDER}/.local/share/{legacy.LEGACY_DIR}/install"
    for name in names:
        files[f"bin/{name}"] = f"#!/bin/sh\n{mark}\nexec '{install_root}/current/bin/{name}' \"$@\"\n"
        modes[f"bin/{name}"] = "0755"
    path_line = f'export PATH="$HOME/.local/bin:$PATH" {path_mark}'
    files["profile"] = f"# a shell profile\n{path_line}\n"
    files["path-line"] = path_line
    # The frozen v4 record and its compiled scope (the 1.0.0 baseline).
    baseline = json.loads((ROOT / "tests" / "fixtures" / "permissive" / "catalog-1.0.0.json").read_text())
    files[f"state/sessions/{MID}.json"] = json.dumps(baseline["record"], indent=2, sort_keys=True) + "\n"
    files[f"state/scopes/{MID}/settings.json"] = json.dumps(baseline["settings"], indent=2, sort_keys=True) + "\n"
    for name, text in sorted(baseline["agent_files"].items()):
        files[f"state/scopes/{MID}/{name}"] = text
    for name, text in sorted(baseline["other_files"].items()):
        files[f"state/scopes/{MID}/{name}"] = text
    document = {
        "source": f"release 1.1.0 ({TAG}): units and shims rendered by its own code; record and scope from "
                  "the frozen 1.0.0 baseline",
        "home_placeholder": PLACEHOLDER,
        "managed_id": MID,
        "files": files,
        "modes": modes,
    }
    OUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
