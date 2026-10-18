"""An installation of the product's earlier name, as release 1.1.0 left it,
for the ``migrate from-legacy`` tests.

The files 1.1.0's own code wrote (its hook and token shims, its two gateway
unit files, its launchers and PATH line) and its session record with the
compiled scope come from ``tests/fixtures/legacy/v1.1.0-tree.json`` (recipe:
``build_v1_1_0_tree.py``, from the ``v1.1.0`` tag), with ``@HOME@`` set to
the test's HOME. The frozen record and scope name the baseline's placeholder
paths (``/fixture/bin``, ``/fixture/project``): they become the earlier state
root's ``bin`` and a project directory in HOME, and the record's launch fence
is the digest of the scope as written here, as the release computed it.

Around them this helper lays out everything else such an installation keeps,
by name and kind (the values are test data, never a credential): the 27
configuration entries, the state root's markers, gateway evidence and
history, the data root's children and the retained install tree with its
receipt; and charmer's own fresh installation beside it (its install tree,
receipt, launchers and, optionally, the empty skeleton its installer
leaves). Every name of the earlier product comes from ``charmer.legacy``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from _layout import FIXTURES_ROOT
from charmer import install_receipt, legacy, legacy_migration, scope, sessions, strict_json
from charmer.platform import systemd_unit

FIXTURE = FIXTURES_ROOT / "legacy" / "v1.1.0-tree.json"
CLIENT = "2.1.292"
SECRET = b"OPENROUTER_CLAUDE_API_KEY=test-only-not-a-credential\n"

# The configuration root's 27 entries (files, directories, locks).
CONFIG_ENTRIES = (
    "continuity.json", "endpoint.json", "secret-file.json", "api-key", "api-key.lock", "previous-key", "secrets",
    "management-key", "management-key.next", "management-key.disabled", "management-key.prepared",
    "management-key.lock", "config.yaml", "prepared.json", "choices.json", "settings.json", "preferences.json",
    "bindings.json", "custom.json", "profiles", "compositions", "README.txt", "providers.d",
    "operator-ledger.json", ".env", "served-change.lock", "key-file",
)


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def mkdirs(path: Path) -> Path:
    """Create ``path`` and every missing parent private (0700), as the product does."""

    missing = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)
    return path


def write(path: Path, data: bytes | str, mode: int = 0o600) -> Path:
    mkdirs(path.parent)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    os.chmod(path, mode)
    return path


def dump(path: Path, document: Any, mode: int = 0o600) -> Path:
    return write(path, json.dumps(document, indent=2, sort_keys=True) + "\n", mode)


def manifest(name: str, version: str) -> dict[str, Any]:
    return {"format": 1, "name": name, "version": version, "release_date": "2026-10-07", "target": "linux-x86_64",
            "state_format": 4, "claude_code": {"version": CLIENT}, "gateway": {"sha256": "0" * 64}}


@dataclass
class LegacyTree:
    home: Path
    environ: dict[str, str]
    layout: legacy_migration.Layout
    mid: str
    project: Path
    files: dict[str, str] = field(default_factory=dict)

    def launcher(self, name: str) -> Path:
        return self.layout.bin_dir / name

    def unit(self, name: str) -> Path:
        return self.layout.unit_dir / f"{name}.service"


def build(home: Path, *, xdg_config: Path | None = None, xdg_state: Path | None = None, skeleton: bool = True,
          service: bool = True, pointer: str = "inside") -> LegacyTree:
    """Lay the earlier installation (and charmer's fresh install) out under ``home``.

    ``xdg_state`` moves both state roots under that directory;
    ``xdg_config`` splits the configuration root from the gateway's
    configuration directory; ``service`` installs the supervised gateway's
    unit files (else the gateway runs on demand); ``pointer`` is where the
    selected key file is: ``inside`` the configuration root (``~/`` form),
    ``absolute`` there, ``outside`` it, ``install`` in the retained install tree.
    """

    mkdirs(home)
    environ = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    if xdg_config is not None:
        environ["XDG_CONFIG_HOME"] = str(xdg_config)
    if xdg_state is not None:
        environ["XDG_STATE_HOME"] = str(xdg_state)
    layout = legacy_migration.Layout.of(environ)
    tree = fixture()
    mid = tree["managed_id"]
    project = home / "project"
    mkdirs(project)
    texts = {name: text.replace(tree["home_placeholder"], str(home)) for name, text in tree["files"].items()}
    shim_dir = layout.legacy_state / "bin"
    for name, text in texts.items():
        text = text.replace("/fixture/bin/", f"{shim_dir}/").replace("/fixture/project", str(project))
        mode = int(tree["modes"].get(name, "0600"), 8)
        if name.startswith("bin/"):
            write(layout.bin_dir / name.removeprefix("bin/"), text, mode)
        elif name.startswith("units/"):
            if service:
                write(layout.unit_dir / name.removeprefix("units/"), text, 0o644)
        elif name.startswith("state/"):
            write(layout.legacy_state / name.removeprefix("state/"), text, mode)
        elif name == "profile":
            write(home / ".profile", text, 0o644)
        texts[name] = text
    # The record's launch fence: the digest of the scope as written here.
    scope_dir = layout.legacy_state / "scopes" / mid
    record_path = layout.legacy_state / "sessions" / f"{mid}.json"
    record = json.loads(record_path.read_text())
    settings = json.loads((scope_dir / "settings.json").read_text())
    record["launch_fence"] = sessions.launch_digest(settings, (scope_dir / scope.LEAD_SET_JSON).read_bytes())
    dump(record_path, record)
    _state(layout, mid)
    _config(layout, pointer=pointer, service=service)
    _data(layout, texts["path-line"], home)
    _charmer(layout, skeleton=skeleton)
    return LegacyTree(home, environ, layout, mid, project, texts)


def _state(layout: legacy_migration.Layout, mid: str) -> None:
    root = layout.legacy_state
    write(root / "channel", "bundle\n")
    write(root / "state-version", "4\n")
    write(root / "migration.lock", b"")
    workdir = root / "gateway"
    dump(workdir / "exec.json", {"binary": f"{layout.legacy_install}/current/runtime/gateway", "instance":
                                 "0123456789abcdef", "launcher_version": "1.1.0", "pid": 4242,
                                 "pid_namespace": "pid:[4026531836]", "signature": "s", "started_at":
                                 "2026-10-08T10:00:00+00:00"})
    dump(workdir / "persistence-hold.json", {"version": 1, "holds": [], "cleared": {}, "cleared_through": None})
    dump(workdir / "last-reload.json", {"status": "reloaded"})
    write(workdir / "logs" / "instance-0123456789abcdef.log", "a gateway log line\n")
    write(workdir / "gateway.lock", b"")
    write(root / "lineup-log" / f"{mid}.jsonl", '{"event": "applied"}\n')
    write(root / "notice" / f"{mid}.seen", "1\n")
    write(root / "hook-errors.log", "{}\n")
    write(root / f"lead-prompt-{'a' * 12}-{mid}.md", f"The lineup is in {root}/scopes/{mid}/lineup.md\n")
    dump(root / "sessions" / f"{mid}.v3.json", {"version": 3, "managed_id": mid})
    write(root / "restored-2x" / "record.json", "{}\n")
    write(root / "quarantine-3x" / "record.json", "{}\n")
    dump(root / "last-session-by-cwd" / ("b" * 64 + ".json"), {"cwd": "/x", "session_id": mid})
    dump(root / "drafts" / "line.json", {"key": "line"})
    dump(root / "drafts" / "line.review.json", {"repo": {"path": f"/repo/{legacy.LEGACY_RESOURCES.parent}"}})
    dump(root / "operator-evidence.json", {"version": 1})
    write(root / ".hook-errors.log.ab12cd.tmp", "partial\n")


def _config(layout: legacy_migration.Layout, *, pointer: str, service: bool) -> None:
    root, gateway = layout.legacy_config, layout.legacy_gateway
    home = layout.home
    key_file = {"inside": gateway / "key-file", "absolute": gateway / "key-file",
                "outside": home / "keys" / "gateway-key",
                "install": layout.legacy_install / "key-file"}[pointer]
    write(gateway / "key-file", SECRET)
    if pointer != "inside":
        write(key_file, SECRET)
    spelled = f"~/{key_file.relative_to(home).as_posix()}" if pointer == "inside" else str(key_file)
    dump(gateway / "secret-file.json", {"version": 1, "path": spelled})
    # The continuity set (its format is unchanged since 1.1.0): one alias the
    # fixture catalog seeds, a pruned one and the seed watermark.
    from charmer import catalog, continuity
    from _catalog import FIXTURE_ROOT

    seed = continuity.seed_only(catalog.load_catalog(FIXTURE_ROOT))
    alias = sorted(seed["aliases"])[0]
    dump(gateway / "continuity.json", {"version": 1, "seeded_through_catalog": seed["seeded_through_catalog"],
                                       "state_root": str(layout.legacy_state),
                                       "aliases": {alias: seed["aliases"][alias]}, "pruned": {"gone-alias": 31}})
    endpoint_document: dict[str, Any] = {"version": 1, "host": "127.0.0.1", "port": 8317}
    if service:
        endpoint_document.update(backend="systemd", unit=legacy.LEGACY_UNIT)
    dump(gateway / "endpoint.json", endpoint_document)
    for name in ("api-key", "previous-key", "management-key", "management-key.next", "management-key.disabled",
                 "management-key.prepared"):
        write(gateway / name, SECRET)
    for name in ("api-key.lock", "management-key.lock", "served-change.lock"):
        write(gateway / name, b"")
    write(gateway / "secrets" / "provider-keys.env", b"PROVIDER_KEY=test-only\n")
    write(gateway / "config.yaml", f"auth-dir: {layout.legacy_data}/auth\n")
    dump(gateway / "prepared.json", {"state_root": str(layout.legacy_state)})
    write(gateway / ".env", b"X=1\n")
    ledger = {"version": 1, "routes": {}, "admissions": {}, "aliases": {}, "pruned": {}, "removed": {},
              "transport_choices": {}}
    for name, document in (("choices.json", {"version": 1}), ("settings.json", {"version": 1}),
                           ("preferences.json", {"claude_feedback_drafts": "off"}),
                           ("bindings.json", {"version": 1, "bindings": {}}),
                           ("custom.json", {"version": 1, "providers": {}, "models": {}}),
                           ("operator-ledger.json", ledger)):
        dump(root / name, document)
    write(root / "README.txt", "the user's own notes\n")
    dump(root / "profiles" / "mine.json", {"version": 2, "name": "mine", "description": "", "lead": {"effort": "ultracode", "model": "opus"}, "agents": {}, "native_agents": {"explore": "native", "general_purpose": "on", "plan": "native"}, "workflows": "native"})
    dump(root / "profiles" / ".seed-digests.json", {})
    dump(root / "compositions" / "old.json", {})
    write(root / "providers.d" / "acme.json", (FIXTURES_ROOT / "operator" / "providers.d" / "acme.json").read_bytes())


def _data(layout: legacy_migration.Layout, path_line: str, home: Path) -> None:
    root = layout.legacy_data
    write(root / "claude" / CLIENT / "claude", b"#!/bin/sh\n", 0o755)
    dump(root / "claude" / "pins.json", {"1.1.0": CLIENT})
    write(root / "pinned-clients" / "pin.json", "{}\n")
    dump(root / "auth" / "claude-account.json", {"type": "claude"})
    write(root / "auth.pre-2026" / "old.json", "{}\n")
    write(root / "traces" / "trace.jsonl", "{}\n")
    write(root / "proxy.log", "log\n")
    write(root / "proxy.pid", "4242\n")
    install = layout.legacy_install
    dump(install / "versions" / "1.1.0" / "MANIFEST.json", manifest(legacy.NAME, "1.1.0"))
    os.symlink(Path("versions") / "1.1.0", install / "current")
    receipt = install_receipt.Receipt(
        tuple(install_receipt.Launcher(str(layout.bin_dir / name), install_receipt.file_sha256(layout.bin_dir / name)
                                       or "") for name in legacy.LEGACY_LAUNCHERS),
        (install_receipt.PathLine(str(home / ".profile"), legacy.LEGACY_PATH_MARK, path_line),))
    install_receipt.write(install, receipt)


def _charmer(layout: legacy_migration.Layout, *, skeleton: bool) -> None:
    install = layout.install
    dump(install / "versions" / "2.0.0" / "MANIFEST.json", manifest("charmer", "2.0.0"))
    os.symlink(Path("versions") / "2.0.0", install / "current")
    for name in ("charmer", "charmer-proxy"):
        write(layout.bin_dir / name, f"#!/bin/sh\n{install_receipt.LAUNCHER_MARK}\nexec '{install}/current/bin/"
              f"{name}' \"$@\"\n", 0o755)
    install_receipt.record(install, [layout.bin_dir / "charmer", layout.bin_dir / "charmer-proxy"])
    if skeleton:
        write(layout.state / "channel", "bundle\n")
        write(layout.state / "gateway-inhibition.lock", b"")


# ------------------------------------------------------------ snapshots

def snapshot(*roots: Path, skip: Callable[[Path], bool] = lambda _path: False) -> dict[str, tuple[str, Any]]:
    """Every entry under ``roots`` -> ``(kind, content)``: a file's mode and
    sha256, a link's target, a directory."""

    found: dict[str, tuple[str, Any]] = {}
    for root in roots:
        if os.path.islink(root) or os.path.isfile(root):
            found[str(root)] = _entry(root)
            continue
        if not os.path.isdir(root):
            continue
        for directory, dirnames, filenames in os.walk(root):
            for name in sorted([*dirnames, *filenames]):
                path = Path(directory) / name
                if not skip(path):
                    found[str(path)] = _entry(path)
            dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(directory, name))]
    return found


def _entry(path: Path) -> tuple[str, Any]:
    info = os.lstat(path)
    if os.path.islink(path):
        return ("link", os.readlink(path))
    if os.path.isdir(path):
        return ("dir", oct(info.st_mode & 0o777))
    return ("file", (oct(info.st_mode & 0o777), hashlib.sha256(path.read_bytes()).hexdigest()))


# ------------------------------------------------------------ a fake service manager

class FakeSystemd:
    """``systemctl --user`` over the unit directory: enable (``--now``,
    ``--runtime``), disable (``--now``), start and stop each change
    enablement or running state on its own, daemon-reload and the
    properties the migration reads. ``enabled``/``active`` seed units
    (``active`` defaults to ``enabled``), ``runtime`` names units enabled
    for this boot only; ``fail`` names verbs (``verb``) or one unit's verb
    (``verb:unit``) that fail."""

    def __init__(self, unit_dir: Path, *, enabled: Mapping[str, bool] | None = None,
                 active: Mapping[str, bool] | None = None, runtime: Sequence[str] = ()) -> None:
        self.unit_dir = unit_dir
        self.state: dict[str, dict[str, bool]] = {
            name: {"enabled": on, "active": (active or {}).get(name, on), "runtime": name in runtime}
            for name, on in (enabled or {}).items()}
        self.calls: list[list[str]] = []
        self.fail: set[str] = set()

    def __call__(self, argv, **_kwargs) -> subprocess.CompletedProcess:
        argv = [str(item) for item in argv]
        self.calls.append(argv)
        words = [item for item in argv[2:] if item != "--no-ask-password"]
        verb = words[0] if words else ""
        unit = words[-1].removesuffix(".service") if words else ""
        if verb in self.fail or f"{verb}:{unit}" in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", f"{verb} failed")
        if verb == "daemon-reload":
            return subprocess.CompletedProcess(argv, 0, "", "")
        current = self.state.setdefault(unit, {"enabled": False, "active": False, "runtime": False})
        present = (self.unit_dir / f"{unit}.service").is_file()
        if verb in ("enable", "start") and not present:
            return subprocess.CompletedProcess(argv, 1, "", "unit not found")
        if verb == "enable":
            current.update(enabled=True, runtime="--runtime" in words)
            if "--now" in words:
                current["active"] = True
        elif verb == "disable":
            current.update(enabled=False, runtime=False)
            if "--now" in words:
                current["active"] = False
        elif verb == "start":
            current["active"] = True
        elif verb == "stop":
            current["active"] = False
        elif verb == "show":
            state = ("enabled-runtime" if current.get("runtime") else "enabled") if current["enabled"] else "disabled"
            values = {"LoadState": "loaded" if present else "not-found",
                      "FragmentPath": str(self.unit_dir / f"{unit}.service"),
                      "UnitFileState": state,
                      "ActiveState": "active" if current["active"] else "inactive",
                      "Result": "success", "ExecMainStatus": "0"}
            asked = [item.removeprefix("--property=") for item in words if item.startswith("--property=")]
            if "--value" in words:
                return subprocess.CompletedProcess(argv, 0, "\n".join(values.get(name, "") for name in asked) + "\n",
                                                   "")
            return subprocess.CompletedProcess(argv, 0, "".join(f"{name}={values.get(name, '')}\n"
                                                                for name in asked), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def unit(self, name: str) -> dict[str, bool]:
        """A unit's (enabled, runtime, active) as the service manager has it."""

        current = self.state.get(name, {"enabled": False, "active": False, "runtime": False})
        return {"enabled": current["enabled"], "runtime": bool(current.get("runtime")), "active": current["active"]}

    def verbs(self) -> list[tuple[str, str]]:
        return [(words[0], words[-1]) for words in ([item for item in call[2:] if item != "--no-ask-password"]
                                                    for call in self.calls) if words and words[0] != "show"]


def charmer_unit_text(name: str) -> str:
    return f"{systemd_unit.HEADER}\n[Unit]\nDescription=charmer gateway ({name})\n"


def read_json(path: Path) -> Any:
    return strict_json.loads(path.read_bytes())
