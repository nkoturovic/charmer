"""What the previous release can read of the state on disk, before a rollback.

``charmer update --rollback`` runs this check from the release being left,
before it asks. It reads the retained release's packaged catalog data and
evaluates the live state against it with this release's own validators:

- **declarations**: providers.d, the ledger and the legacy registry, loaded
  by this release's operator-layer loader against the target's catalog
  documents. A file or line the target's documents make invalid while this
  release's do not is listed, referenced or not: one invalid line makes the
  target's ``providers apply``, ``models add`` and ``models rm`` refuse;
- **profiles and named bindings**, evaluated as doctor evaluates them, with a
  lineup catalog built from the target's documents;
- **running sessions**: a selector a live record names that this release's
  gateway render serves and the target's would not. Both sets come from the
  candidate render (:func:`charmer.proxy.candidate_document`), so
  continuity aliases, the record extension, retained captures and
  credential availability count as a real render counts them; neither the
  raw packaged catalog nor the running gateway's list decides.

An item both releases report is not listed; it is counted ("unchanged by
the rollback"). The target's documents (catalog files, ``version.json`` and
the seed profiles) are evaluated with this release's schemas: a private
temporary resource root holds this release's schemas, settings and role
prompts beside them and is removed afterwards. Nothing in the state tree is
written, and no other program runs: the target's own ``doctor`` is not
started before the operator has answered, because its plain report
refreshes the shared hook and token shims to the target's launcher and
records the channel marker (a change before consent), and its read-only
JSON report redacts the untyped lines that would matter here. Run the
target's doctor after the rollback, as the update guide says.

Limits: the checks apply this release's rules, its model-id grammar
included, so the target may still refuse what they accept. A release that
widens the grammar must give this check the previous grammar too.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from charmer import strict_json, termtext

DOCS_ANCHOR = "docs/update.md#rolling-back"
DECLARATIONS, PROFILES, SESSIONS = "declarations", "profiles", "sessions"
GROUPS = (DECLARATIONS, PROFILES, SESSIONS)
# The target's documents the check reads (resource-relative); the seed
# profiles under catalog/profiles/ come too.
TARGET_DOCUMENTS = (
    "version.json", "catalog/gateway.json", "catalog/models.json", "catalog/native-contract.json",
    "catalog/providers.json", "catalog/retired.json", "catalog/roles.json",
)
_READ_LIMIT = 8 * 1024 * 1024
_REASON_LIMIT = 240
_LINE_PATH = re.compile(r"^\$\.lines\.([a-z0-9][a-z0-9-]*)(?:\.|$)")
_PROVIDER_FILE = re.compile(r"^providers\.d/([a-z][a-z0-9-]*)\.json$")


class Unreadable(Exception):
    """The target's packaged data cannot be read (the check falls through)."""


@dataclass(frozen=True)
class Finding:
    """One thing the target cannot read, with what to do about it."""

    group: str
    text: str
    remedy: str

    def line(self) -> str:
        return f"{self.text} — {self.remedy}"


@dataclass(frozen=True)
class Report:
    """The preflight's result: findings by group, the count of items both
    releases report, notes on what could not be checked and the reason the
    target's data could not be read (then nothing was checked)."""

    current: str
    target: str
    findings: tuple[Finding, ...] = ()
    unchanged: int = 0
    notes: tuple[str, ...] = ()
    unreadable: str | None = None

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.unreadable is not None:
            out.append(f"Could not check what {self.target} can read ({self.unreadable}).")
        else:
            headers = {
                DECLARATIONS: f"Declarations {self.target} cannot read ({DOCS_ANCHOR}):",
                PROFILES: f"Profiles and named bindings {self.target} cannot evaluate ({DOCS_ANCHOR}):",
                SESSIONS: f"Running sessions on selectors {self.target} does not serve ({DOCS_ANCHOR}):",
            }
            for group in GROUPS:
                items = [finding for finding in self.findings if finding.group == group]
                if items:
                    out.append(headers[group])
                    out.extend(f"  - {finding.line()}" for finding in items)
            if not self.findings:
                out.append(f"Nothing found that {self.target} cannot read.")
            # The count is printed even when it is zero: what was subtracted
            # is never silent.
            out.append(f"Also reported by {self.current}: {self.unchanged}, unchanged by the rollback.")
            out.extend(self.notes)
            out.append(f"Checked with {self.current}'s rules against {self.target}'s data; "
                       f"{self.target} may still refuse what these rules accept.")
        return [termtext.visible_text(line) for line in out]


# ------------------------------------------------------------ the target's data


def bundle_data_root(bundle: Path) -> Path | None:
    """The packaged resources of an installed bundle (as the updater reads
    its native contract), or None when it carries none."""

    for site in sorted(Path(bundle).glob("lib/python3*/site-packages")):
        root = site / "charmer" / "data"
        if (root / "version.json").is_file():
            return root
    return None


def _read_document(root: Path, relative: str) -> bytes:
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            raise Unreadable(f"{relative} is missing")
        with open(path, "rb") as handle:
            raw = handle.read(_READ_LIMIT + 1)
    except OSError as exc:
        raise Unreadable(f"{relative}: {exc.strerror or type(exc).__name__}") from None
    if len(raw) > _READ_LIMIT:
        raise Unreadable(f"{relative} is larger than {_READ_LIMIT} bytes")
    try:
        strict_json.loads(raw)
    except (strict_json.StrictJSONError, ValueError, RecursionError) as exc:
        raise Unreadable(f"{relative} is not valid JSON ({exc})") from None
    return raw


def read_target(root: Path) -> dict[str, bytes]:
    """The target's catalog documents, ``version.json`` and seed profiles
    (resource-relative path -> bytes); :class:`Unreadable` names what failed."""

    documents = {relative: _read_document(root, relative) for relative in TARGET_DOCUMENTS}
    profiles = root / "catalog" / "profiles"
    try:
        names = sorted(path.name for path in profiles.glob("*.json"))
    except OSError as exc:
        raise Unreadable(f"catalog/profiles: {exc.strerror or type(exc).__name__}") from None
    if not names:
        raise Unreadable("catalog/profiles holds no seed profile")
    for name in names:
        documents[f"catalog/profiles/{name}"] = _read_document(root, f"catalog/profiles/{name}")
    return documents


def compose_root(current_root: Path, documents: Mapping[str, bytes], into: Path) -> Path:
    """A resource root of this release's schemas, settings and role prompts
    with the target's ``documents`` (:func:`read_target`). Returns ``into``."""

    into.mkdir(mode=0o700)
    shutil.copytree(current_root / "schemas", into / "schemas")
    shutil.copyfile(current_root / "settings.json", into / "settings.json")
    (into / "catalog").mkdir(mode=0o700)
    shutil.copytree(current_root / "catalog" / "prompts", into / "catalog" / "prompts")
    (into / "catalog" / "profiles").mkdir(mode=0o700)
    for relative, raw in documents.items():
        (into / relative).write_bytes(raw)
    return into


# ------------------------------------------------------------ one release's view


@dataclass(frozen=True)
class _View:
    declarations: dict[tuple[str, str | None], tuple[str, ...]]
    legacy: dict[str, str]
    profiles: dict[str, str]  # text -> remedy
    served: frozenset[str]


def _line_key(json_path: str) -> str | None:
    match = _LINE_PATH.match(json_path or "")
    return match[1] if match else None


def _declaration_remedy(file: str, key: str | None) -> str:
    if key is not None:
        return f"charmer models rm {key}, or charmer models edit {key}"
    match = _PROVIDER_FILE.match(file)
    return f"charmer providers edit {match[1]}" if match else "charmer providers validate"


def _profile_items(environ: Mapping[str, str], cat, merged: Mapping, snapshot) -> dict[str, str]:
    """Doctor's profile and named-binding evaluation, as ``text -> remedy``."""

    from charmer import choices, profile as profile_mod, settings as settings_mod
    from charmer.cli import runtime as runtime_mod

    env = dict(environ)
    lines_doc = merged["models-v2"] if "models-v2" in merged else merged["models"]
    lcat = profile_mod.LineupCatalog.from_docs(merged)
    try:
        effective = settings_mod.effective(settings_mod.SettingsStore(env).load(),
                                           provider_ids=merged["providers"]["providers"],
                                           line_keys=lines_doc["models"])
        effective = runtime_mod.operator_effective(effective, snapshot, docs=dict(merged))
        ceiling = choices.window_ceiling(env)
        if ceiling.problem is None:
            effective = dataclasses.replace(effective, window_ceiling=ceiling.value)
    except settings_mod.SettingsError:
        effective = None
    items: dict[str, str] = {}
    try:
        bindings = profile_mod.BindingStore(env).bindings()
    except (profile_mod.BindingError, profile_mod.ProfileError) as exc:
        bindings = None
        items[f"named bindings: {exc}"] = "fix or remove bindings.json"
    profiles = profile_mod.ProfileStore.for_catalog(cat, env)
    users: dict[str, list[str]] = {}
    for name in profiles.names():
        try:
            document = profiles.load(name)
        except (profile_mod.ProfileError, OSError) as exc:
            items[f"profile {name}: {exc}"] = f"charmer profile edit {name}"
            continue
        for binding in profile_mod.referenced_bindings(document).values():
            users.setdefault(binding, []).append(name)
        evaluation = profile_mod.evaluate(document, lcat, bindings=bindings, effective=effective, ad_hoc=False)
        for error in evaluation.errors:
            items[f"profile {name}: {error}"] = f"charmer profile edit {name}"
    for name in sorted(bindings or {}):
        value = bindings[name]
        if not isinstance(value, Mapping):
            continue
        holders = sorted(set(users.get(name, ())))
        remedy = (f"charmer profile edit {holders[0]} (it uses this binding), or the profile editor (charmer, then P)"
                  if holders else "rebind it in the profile editor (charmer, then P)")
        for error in profile_mod.binding_errors(name, value, lcat, effective):
            items[error] = remedy
    return items


def _view(environ: Mapping[str, str], home: Path, state_root: Path, root: Path, legacy: Mapping) -> _View:
    """One release's reading of the live state, with the resources at ``root``."""

    from charmer import catalog, operator as operator_mod, proxy, render

    cat = catalog.load_catalog(root)
    snapshot = proxy.operator_snapshot(environ, cat.docs, legacy, asset_root=root)
    declarations: dict[tuple[str, str | None], list[str]] = {}
    for problem in operator_mod.blocking_problems(snapshot.layer):
        declarations.setdefault((problem.file, _line_key(problem.json_path)), []).append(problem.text())
    merged = operator_mod.merge_docs(cat.docs, snapshot.layer, legacy=legacy)
    profiles = _profile_items(environ, cat, merged, snapshot)
    document = proxy.candidate_document(home, environ=environ, asset_root=root, state_root=state_root)
    return _View({key: tuple(texts) for key, texts in declarations.items()}, dict(snapshot.layer.legacy_dropped),
                 profiles, render.rendered_selectors(document))


def _target_environ(environ: Mapping[str, str], target_root: Path, current_root: Path) -> dict[str, str]:
    """The environment of the target's view: its pinned registry when it
    carries one (the overlay only; it serves no selector), else this one's."""

    from charmer import catalog

    env = dict(environ)
    for candidate in (target_root / "registry", current_root / "registry"):
        if candidate.is_dir():
            env[catalog.REGISTRY_DIR_ENV] = str(candidate)
            break
    return env


# ------------------------------------------------------------ the check


def _reason(exc: BaseException) -> str:
    text = termtext.visible_text(str(exc).strip() or type(exc).__name__)
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 1] + "…"


def _compare(current: _View, target: _View) -> tuple[list[Finding], int]:
    """The target's items this release does not report (same file and line
    for declarations, the exact text otherwise), and how many both report."""

    findings: list[Finding] = []
    unchanged = 0
    for (file, key), texts in sorted(target.declarations.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        if (file, key) in current.declarations:
            unchanged += len(texts)
            continue
        findings.extend(Finding(DECLARATIONS, text, _declaration_remedy(file, key)) for text in texts)
    for legacy_id, text in sorted(target.legacy.items()):
        if current.legacy.get(legacy_id) is not None:
            unchanged += 1
            continue
        findings.append(Finding(DECLARATIONS, f"custom.json: {text}", "review it with charmer custom list"))
    for text, remedy in sorted(target.profiles.items()):
        if text in current.profiles:
            unchanged += 1
            continue
        findings.append(Finding(PROFILES, text, remedy))
    return findings, unchanged


def _session_findings(state_root: Path, current: _View, target: _View, label: str) -> tuple[list[Finding], list[str]]:
    from charmer import continuity

    scan = continuity.scan_records(state_root)
    findings: list[Finding] = []
    for record in sorted(scan.live):
        for base in sorted(base for base, ids in scan.refs.items() if record in ids):
            if base in current.served and base not in target.served:
                findings.append(Finding(
                    SESSIONS, f"session {record[:8]} uses {base}, which {label} does not serve",
                    f"exit it first (end the session; after the rollback, charmer -r {record} resumes it on "
                    f"{label}'s models)"))
    notes: list[str] = []
    if scan.directory_error is not None:
        notes.append(f"Session records not checked (the sessions directory is unreadable: {scan.directory_error}).")
    if scan.unreadable:
        notes.append("Session records not checked (unreadable): "
                     + ", ".join(sorted(identifier[:8] for identifier in scan.unreadable)) + ".")
    return findings, notes


def check(*, environ: Mapping[str, str], home: Path, state_root: Path, current_root: Path,
          target_root: Path | None, current: str, target: str) -> Report:
    """The preflight of a rollback from ``current`` to ``target`` (read only).

    ``current_root``: this release's resources; ``target_root``: the
    target's packaged resources (:func:`bundle_data_root`). When the
    target's data or this check fails, the report says it could not check
    and why; the rollback then asks as it always did.
    """

    try:
        report = _check(environ=dict(environ), home=Path(home), state_root=Path(state_root),
                        current_root=Path(current_root), target_root=target_root, current=current, target=target)
    except KeyboardInterrupt:
        raise
    except Unreadable as exc:
        report = Report(current, target, unreadable=str(exc))
    except Exception as exc:  # an advisory check never shuts the rollback: it says why it did not check
        report = Report(current, target, unreadable=_reason(exc))
    return report


def _check(*, environ: dict[str, str], home: Path, state_root: Path, current_root: Path,
           target_root: Path | None, current: str, target: str) -> Report:
    from charmer import custom

    if target_root is None:
        raise Unreadable("the retained release carries no packaged data")
    documents = read_target(Path(target_root))
    legacy = custom.load_registry(environ)
    mine = _view(environ, home, state_root, current_root, legacy)
    with tempfile.TemporaryDirectory(prefix="charmer-rollback-check-") as scratch:
        root = compose_root(current_root, documents, Path(scratch) / "data")
        try:
            theirs = _view(_target_environ(environ, Path(target_root), current_root), home, state_root, root, legacy)
        except Exception as exc:
            raise Unreadable(f"{target}'s data cannot be read with {current}'s rules ({_reason(exc)})") from exc
    findings, unchanged = _compare(mine, theirs)
    sessions, notes = _session_findings(state_root, mine, theirs, target)
    return Report(current, target, tuple([*findings, *sessions]), unchanged, tuple(notes))


# ------------------------------------------------------------ the updater's seam


def for_rollback(environ: Mapping[str, str], *, state_root: Path, current_root: Path) -> Callable[..., Report]:
    """The updater's preflight of the rollback an installation shows."""

    def run(installation) -> Report:
        previous = installation.previous
        return check(environ=environ, home=Path(environ["HOME"]), state_root=state_root,
                     current_root=current_root, target_root=bundle_data_root(previous.path),
                     current=installation.current.version, target=previous.version)

    return run
