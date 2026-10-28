"""Read-only verification against the installed release trust, never latest.

The two manifests are different authorities: signed SHA256SUMS authenticates
an outer release manifest; its selected asset authenticates the installed
inner manifest, which describes the content and component identities.

No installer-added paths occur inside a completed versions/<version> tree:
install.sh and self_update move the extracted tree unchanged. Their receipt,
locks, staging/download directories and current/previous links are outside it.
Only MANIFEST.json is excluded from the content digest (to avoid a cycle).

This is an integrity snapshot using installed code and trust, not attestation
of a hostile host. Descriptor-relative no-follow reads and before/after stat
checks refuse observed replacement; no filesystem-wide atomic snapshot is
claimed. There are no repairs, locks created, token reads or executable runs.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, NoReturn, TYPE_CHECKING

from . import bundle_content, errors, pin, self_update, service, strict_json, trust

if TYPE_CHECKING:
    from .gateway_lifecycle import Observation

CONTENT_EXCLUSIONS = (self_update.MANIFEST,)
GATEWAY_PATH = "libexec/charmer/" + service.GATEWAY_BINARY
_TARGETS = {"linux-x64": "linux-x86_64", "linux-arm64": "linux-aarch64",
            "darwin-x64": "darwin-x86_64", "darwin-arm64": "darwin-arm64"}
_CHUNK = 1 << 20
_HEX = re.compile(r"[0-9a-f]{64}")
_METADATA_REMEDY = ("obtain MANIFEST.json, SHA256SUMS and SHA256SUMS.sshsig for the exact installed release, "
                    "then retry with --from-dir")


@dataclass(frozen=True)
class Check:
    component: str
    status: str
    detail: str
    expected: str | None = None
    actual: str | None = None


@dataclass(frozen=True)
class Verification:
    version: str | None
    target: str | None
    checks: tuple[Check, ...]

    @property
    def status(self) -> str:
        if any(check.status in ("mismatch", "different-running-build") for check in self.checks):
            return "mismatch"
        if any(check.status not in ("match", "current-match", "stopped") for check in self.checks):
            return "incomplete"
        return "verified" if self.checks else "incomplete"

    @property
    def exit_code(self) -> int:
        return {"verified": 0, "mismatch": 1, "incomplete": 3}[self.status]

    def document(self) -> dict:
        """The same minimized facts for CLI and TUI; no absolute paths."""
        return {"status": self.status, "exit_code": self.exit_code, "version": self.version,
                "target": self.target, "checks": [asdict(check) for check in self.checks]}

    def lines(self, *, surface: str = "cli") -> tuple[str, ...]:
        lines = [f"{check.component}: {check.status} — {check.detail}" for check in self.checks]
        summary = {"verified": "Verified against the signed release using installed trust.",
                   "mismatch": "Mismatch. Reinstall with the release installer or use charmer update --rollback.",
                   "incomplete": "Partly checked; requested verification is incomplete."}[self.status]
        installed = {"signed-release", "inner-manifest", "bundle-content", "gateway", "managed-client"}
        if (self.status == "mismatch" and installed <= {check.component for check in self.checks if check.status == "match"}
                and all(check.status == "match" for check in self.checks if check.component != "running-gateway")):
            summary = ("Installed bytes verified; only the running gateway differs: "
                       "restart between turns with charmer gateway restart (it follows the persistence hold).")
        elif any(check.component == "signed-release" and check.status == "mismatch" for check in self.checks):
            summary = "Release metadata authentication failed. Do not use these files; " + _METADATA_REMEDY + "."
        lines.append(summary)
        lines.append("Nothing was changed.")
        if surface == "tui":
            lines = [line.replace("--from-dir", "Verify this installation → Downloaded files") for line in lines]
        return tuple(lines)


class _Problem(Exception):
    def __init__(self, status: str, detail: str) -> None:
        self.status, self.detail = status, detail


def _mismatch(detail: str) -> NoReturn:
    raise _Problem("mismatch", detail)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # atime is intentionally absent: reading may change it.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(path: Path | str, *, parent: int | None = None) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)


def _file(parent: int, name: str, *, capture: bool = False) -> tuple[str, os.stat_result, bytes]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        _mismatch("a required file is not a regular, non-symlink file")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if _identity(before) != _identity(info):
            raise _Problem("unavailable", "a file changed while it was opened; retry verification")
        if capture and info.st_size > self_update.METADATA_LIMIT:
            _mismatch("metadata exceeds the size limit")
        digest = hashlib.sha256()
        data = bytearray()
        size = 0
        while chunk := os.read(fd, _CHUNK):
            size += len(chunk)
            if size > info.st_size:
                raise _Problem("unavailable", "a file changed while it was read; retry verification")
            digest.update(chunk)
            if capture:
                data.extend(chunk)
        if (size != info.st_size or _identity(os.fstat(fd)) != _identity(info)
                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(info)):
            raise _Problem("unavailable", "a file changed while it was read; retry verification")
        return digest.hexdigest(), info, bytes(data)
    finally:
        os.close(fd)


def _walk(fd: int, parts: tuple[str, ...], entries: list[bundle_content.Entry],
          identities: dict[tuple[str, ...], tuple[int, ...]], *, hashing: bool) -> None:
    before = os.fstat(fd)
    identities[parts] = _identity(before)
    for name in sorted(os.listdir(fd)):
        path = (*parts, name)
        if "\n" in name or "\0" in name:
            _mismatch("the bundle contains an unsupported path name")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        identities[path] = _identity(info)
        if stat.S_ISDIR(info.st_mode):
            child = _directory(name, parent=fd)
            try:
                if _identity(os.fstat(child)) != _identity(info):
                    raise _Problem("unavailable", "a bundle directory changed; retry verification")
                entries.append((path, "dir", info.st_mode, ""))
                _walk(child, path, entries, identities, hashing=hashing)
            finally:
                os.close(child)
        elif stat.S_ISLNK(info.st_mode):
            entries.append((path, "link", info.st_mode, os.readlink(name, dir_fd=fd)))
        elif stat.S_ISREG(info.st_mode):
            digest = _file(fd, name)[0] if hashing and path != (self_update.MANIFEST,) else ""
            entries.append((path, "file", info.st_mode, digest))
        else:
            _mismatch("the bundle contains a special file")
        if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(info):
            raise _Problem("unavailable", "a bundle entry changed; retry verification")
    if _identity(os.fstat(fd)) != _identity(before):
        raise _Problem("unavailable", "the bundle tree changed; retry verification")


def _check_links(entries: list[bundle_content.Entry]) -> None:
    """Resolve links against the inventory, never against an external path."""
    table = {parts: (kind, value) for parts, kind, _mode, value in entries}
    for parts, kind, _mode, _value in entries:
        if kind != "link":
            continue
        pending, resolved, links = list(parts), [], 0
        while pending:
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    _mismatch("a symbolic link leaves the bundle")
                resolved.pop()
                continue
            resolved.append(part)
            entry = table.get(tuple(resolved))
            if entry is None:
                _mismatch("the bundle contains a dangling symbolic link")
            if entry[0] == "link":
                links += 1
                if links > 40 or entry[1].startswith("/"):
                    _mismatch("the bundle contains an unsafe symbolic link")
                resolved.pop()
                pending = entry[1].split("/") + pending
            elif pending and entry[0] != "dir":
                _mismatch("a symbolic link traverses a non-directory")


def content_digest(root: Path) -> tuple[str, int]:
    """Safely read an installed tree and use the builder's canonical encoder."""
    fd = _directory(root)
    try:
        return _content(fd)[0]
    finally:
        os.close(fd)


def _content(fd: int) -> tuple[tuple[str, int], list[bundle_content.Entry], dict]:
    entries: list[bundle_content.Entry] = []
    identities: dict[tuple[str, ...], tuple[int, ...]] = {}
    _walk(fd, (), entries, identities, hashing=True)
    _check_links(entries)
    _unchanged(fd, identities)
    return bundle_content.content_digest(entries, exclude=CONTENT_EXCLUSIONS), entries, identities


def _unchanged(fd: int, identities: dict) -> None:
    after: dict[tuple[str, ...], tuple[int, ...]] = {}
    _walk(fd, (), [], after, hashing=False)
    if after != identities:
        raise _Problem("unavailable", "the bundle changed during verification; retry verification")


def _json(raw: bytes) -> dict:
    try:
        document = strict_json.loads(raw)
    except (ValueError, RecursionError):
        # JSON's integer-conversion bound raises ValueError, not JSONDecodeError.
        _mismatch("manifest is not valid strict JSON")
    if not isinstance(document, dict):
        _mismatch("manifest is not an object")
    return document


def _digest(value: object) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        _mismatch("manifest has no valid SHA-256 identity")
    return value


def _positive(value: object) -> int:
    if type(value) is not int or value < 1:
        _mismatch("manifest has no valid component size or file count")
    return value


def _member(document: dict, name: str) -> dict:
    value = document.get(name)
    if not isinstance(value, dict):
        _mismatch("manifest is missing required component metadata")
    return value


def _metadata_unavailable(detail: str) -> NoReturn:
    raise _Problem("unavailable", detail + "; " + _METADATA_REMEDY)


class _LocalMetadata:
    """Bounded reads of regular offline files; signatures authenticate the bytes."""
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def fetch(self, name: str, version: str, limit: int) -> bytes:
        try:
            directory = _directory(self.directory)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    _metadata_unavailable("offline metadata must be regular, non-symlink files")
                if info.st_size > limit:
                    _metadata_unavailable("offline metadata exceeds the size limit")
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                _metadata_unavailable("offline metadata exceeds the size limit")
            return raw
        except OSError:
            _metadata_unavailable("offline signed metadata could not be safely read")


def _release(version: str, target: str, *, environ: Mapping[str, str],
             from_dir: Path | None, disclose: Callable[[str], None] | None) -> self_update.Release:
    # Installed trust only: neither the offline directory nor fetched metadata
    # supplies allowed_signers. No CLI trust override is accepted.
    try:
        signers = trust.release_signers()
    except trust.TrustError:
        raise _Problem("unavailable", "installed release trust is unavailable") from None
    if from_dir is not None:
        transport = _LocalMetadata(Path(from_dir))
    else:
        if disclose is None:
            raise _Problem("unavailable", "online verification requires an explicit network disclosure")
        from . import release_update
        try:
            transport = release_update.verification_transport(version, environ=environ, disclose=disclose)
        except self_update.UpdateError:
            raise _Problem("unavailable", "exact-version release location unavailable; use --from-dir") from None
    try:
        sums = transport.fetch(self_update.SUMS, version, self_update.METADATA_LIMIT)
        signature = transport.fetch(self_update.SIGNATURE, version, self_update.METADATA_LIMIT)
    except (self_update.UpdateError, OSError, http.client.HTTPException):
        _metadata_unavailable("signed release metadata could not be read")
    try:
        trust.verify(sums, signature, signers)
    except trust.TrustError:
        _mismatch("SHA256SUMS or its signature does not verify with installed trust")
    try:
        listed = trust.parse_sums(sums)
    except trust.TrustError:
        _metadata_unavailable("signed SHA256SUMS is malformed")
    try:
        outer = transport.fetch(self_update.MANIFEST, version, self_update.METADATA_LIMIT)
    except (self_update.UpdateError, OSError, http.client.HTTPException):
        _metadata_unavailable("the outer release manifest could not be read")
    if listed.get(self_update.MANIFEST) != hashlib.sha256(outer).hexdigest():
        _mismatch("the outer release manifest does not match the signed checksums")
    try:
        release = self_update.parse_release(outer, listed, target)
    except (self_update.UpdateError, ValueError, RecursionError):
        _metadata_unavailable("the outer release manifest is invalid for the selected target")
    if release.version != version:
        _metadata_unavailable("signed release metadata names a different version than installed")
    if release.asset.manifest_sha256 is None or release.asset.content_sha256 is None:
        _metadata_unavailable("signed metadata lacks the inner-manifest or content identity")
    return release


def _client(inner: dict, release: self_update.Release, environ: Mapping[str, str], platform: str) -> Check:
    record = _member(inner, "claude_code")
    version = record.get("version")
    digest, size = _digest(record.get("sha256")), _positive(record.get("size"))
    if (version != release.claude_code or record.get("platform") != platform
            or pin.version_key(version) is None
            or (release.claude_platforms or {}).get(platform) != {"sha256": digest, "size": size}):
        _mismatch("managed-client identities disagree between the authenticated manifests")
    path = pin.owned_path(environ, version, platform)
    root = _directory(path.parent.parent)
    try:
        directory = _directory(path.parent.name, parent=root)
        try:
            actual, info, _ = _file(directory, path.name)
            matched = actual == digest and info.st_size == size and bool(info.st_mode & 0o111)
            if _identity(os.stat(path.parent.name, dir_fd=root, follow_symlinks=False)) != _identity(os.fstat(directory)):
                raise _Problem("unavailable", "the managed-client directory changed; retry verification")
        finally:
            os.close(directory)
    finally:
        os.close(root)
    return Check("managed-client", "match" if matched else "mismatch",
                 f"Claude Code {version}: " + ("matches authenticated identity" if matched else "bytes or mode differ"),
                 digest, actual)


def _previous_digest(root: int, versions: int, target: str) -> str | None:
    """An optional retained local comparison, NOT authenticated release evidence."""
    try:
        link = _link(root, "previous", optional=True, allow_dev=True)
        if link is None:
            return None
        fd = _directory(link.split("/")[1], parent=versions)
        try:
            inner = _json(_file(fd, self_update.MANIFEST, capture=True)[2])
            if inner.get("version") != link.split("/")[1] or inner.get("target") != target:
                return None
            digest = _digest(_member(inner, "gateway").get("sha256"))
            libexec = _directory("libexec", parent=fd)
            try:
                gateway = _directory("charmer", parent=libexec)
                try:
                    actual = _file(gateway, service.GATEWAY_BINARY)[0]
                finally:
                    os.close(gateway)
            finally:
                os.close(libexec)
            return digest if actual == digest else None
        finally:
            os.close(fd)
    except (OSError, _Problem):
        return None


def _link(fd: int, name: str, *, optional: bool = False, allow_dev: bool = False) -> str | None:
    try:
        value = os.readlink(name, dir_fd=fd)
    except FileNotFoundError:
        if optional:
            return None
        raise
    if not value.startswith("versions/"):
        _mismatch("installation link leaves versions/")
    version = value.removeprefix("versions/")
    try:
        if allow_dev:
            self_update.version_key(version)
        else:
            self_update._release_version(version, "installed version")
    except self_update.UpdateError:
        _mismatch("installation link does not name an exact release version")
    return value


def _os_check(component: str, exc: OSError) -> Check:
    status = "mismatch" if isinstance(exc, FileNotFoundError) else "unavailable"
    return Check(component, status, "a required file is missing" if status == "mismatch"
                 else "a required file or directory cannot be safely read")


def verify_installation(*, environ: Mapping[str, str], channel: str | None, running: Path | None,
                        observe: Callable[[], Observation] | None = None, from_dir: Path | None = None,
                        disclose: Callable[[str], None] | None = None, client_platform: str | None = None,
                        proc_root: Path = Path("/proc"), platform: str = sys.platform) -> Verification:
    """Verify current, including after rollback. No network in from-dir mode.

    ``running`` is the launcher's installation prefix, not an assets override.
    ``observe`` is Gateway.observe, not status/health/readiness (no token or
    HTTP request). A missing observation is incomplete, never assumed stopped.
    Online callers supply a disclosure callback; TUI confirmation precedes it.
    """
    if channel != "bundle":
        detail = ("Nix installation: use nix store verify; not a signed-bundle verification" if channel == "nix"
                  else "Source or unrecognized installation: verify the checkout; not a signed-bundle verification")
        return Verification(None, None, (Check("installation", "unsupported", detail),))
    checks: list[Check] = []
    version = target = None
    component = "installation"
    descriptors: list[int] = []
    try:
        try:
            client_platform = client_platform or pin.host_platform()
        except errors.CharmerError:
            raise _Problem("unsupported", "this platform has no supported signed bundle") from None
        expected_target = _TARGETS.get(client_platform)
        if expected_target is None:
            raise _Problem("unsupported", "this platform has no supported signed bundle")
        root_path = self_update.install_root(environ)
        root = _directory(root_path)
        descriptors.append(root)
        current = _link(root, "current")
        current_stat = _identity(os.stat("current", dir_fd=root, follow_symlinks=False))
        version = current.split("/")[1]
        versions = _directory("versions", parent=root)
        descriptors.append(versions)
        fd = _directory(version, parent=versions)
        descriptors.append(fd)
        initial = _identity(os.fstat(fd))
        if running is None or Path(running).resolve() != (root_path / current).resolve():
            raise _Problem("unavailable", "this launcher is not the selected installed release")
        component = "inner-manifest"
        inner_hash, manifest_info, raw = _file(fd, self_update.MANIFEST, capture=True)
        inner = _json(raw)
        if inner.get("format") != 1 or inner.get("name") != "charmer" or inner.get("version") != version:
            _mismatch("installed inner manifest does not describe the selected release")
        target = inner.get("target")
        if target != expected_target:
            target = expected_target  # never report an arbitrary local manifest value
            _mismatch("installed inner manifest names the wrong target for this host")
        component = "signed-release"
        release = _release(version, target, environ=environ, from_dir=from_dir, disclose=disclose)
        checks.append(Check(component, "match", "signature and outer release manifest authenticated for exact installed version"))
        component = "inner-manifest"
        if inner_hash != release.asset.manifest_sha256:
            _mismatch("installed inner manifest differs from the selected asset's authenticated manifest_sha256")
        content = _member(inner, "content")
        expected_content = _digest(content.get("sha256"))
        count = _positive(content.get("files"))
        gateway = _member(inner, "gateway")
        gateway_digest = _digest(gateway.get("sha256"))
        gateway_size = _positive(gateway.get("size"))
        if (expected_content != release.asset.content_sha256 or gateway_digest != release.asset.gateway_sha256
                or gateway.get("path") != GATEWAY_PATH or inner.get("state_format") != release.state_format):
            _mismatch("authenticated outer and inner component identities disagree")
        checks.append(Check(component, "match", "installed inner manifest authenticated by selected asset"))
        component = "bundle-content"
        (actual, files), entries, identities = _content(fd)
        matches = actual == expected_content and files == count
        checks.append(Check(component, "match" if matches else "mismatch",
                            f"{files} regular files; " + ("content matches" if matches else
                            "content or file count differs (aggregate digest cannot locate individual changes)"),
                            expected_content, actual))
        component = "gateway"
        found = next((entry for entry in entries if "/".join(entry[0]) == GATEWAY_PATH), None)
        info = identities.get(tuple(GATEWAY_PATH.split("/")))
        matches = (found is not None and found[1] == "file" and found[3] == gateway_digest
                   and info[3] == gateway_size and bool(found[2] & 0o111))
        checks.append(Check(component, "match" if matches else "mismatch", GATEWAY_PATH +
                            (": matches authenticated identity" if matches else ": missing or differs from authenticated identity"),
                            gateway_digest, found[3] if found and found[1] == "file" else None))
        component = "managed-client"
        try:
            checks.append(_client(inner, release, environ, client_platform))
        except _Problem as exc:
            checks.append(Check(component, exc.status, exc.detail))
        except OSError as exc:
            checks.append(_os_check(component, exc))
        component = "running-gateway"
        previous = _previous_digest(root, versions, target)
        checks.append(verify_running_gateway(observe, gateway_digest, previous_digest=previous,
                                             proc_root=proc_root, platform=platform))
        component = "snapshot"
        _unchanged(fd, identities)
        if (_identity(os.stat(self_update.MANIFEST, dir_fd=fd, follow_symlinks=False)) != _identity(manifest_info)
                or _identity(os.fstat(fd)) != initial
                or _identity(os.stat(version, dir_fd=versions, follow_symlinks=False)) != initial
                or _identity(os.stat("versions", dir_fd=root, follow_symlinks=False)) != _identity(os.fstat(versions))
                or _identity(os.stat(root_path, follow_symlinks=False)) != _identity(os.fstat(root))
                or _identity(os.stat("current", dir_fd=root, follow_symlinks=False)) != current_stat
                or _link(root, "current") != current):
            raise _Problem("unavailable", "installation selection or content changed; retry verification")
    except _Problem as exc:
        checks.append(Check(component, exc.status, exc.detail))
    except OSError as exc:
        checks.append(_os_check(component, exc))
    except (RecursionError, UnicodeError):
        checks.append(Check(component, "unavailable", "bundle paths or metadata exceed supported reading limits"))
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    return Verification(version, target, tuple(checks))


def verify_current(*, environ: Mapping[str, str], from_dir: Path | None = None,
                   disclose: Callable[[str], None] | None = None,
                   state_root: Path | None = None) -> Verification:
    """CLI/TUI adapter with no Runtime or transaction preparation.

    The actual package location identifies the running launcher, never an
    assets or hook-command override. Only Linux's metadata-only observation
    runs here: other platforms' ownership probes can connect to local TCP.
    """
    from . import endpoint, layout, paths, resources_root

    gateway = None

    def observe() -> Observation:
        nonlocal gateway
        from .gateway_lifecycle import Gateway

        try:
            if gateway is None:
                document = strict_json.load(resources_root() / "catalog/gateway.json")
                config = document.get("gateway") if isinstance(document, dict) else None
                if (not isinstance(config, dict) or not isinstance(config.get("base_url"), str)
                        or not 0 < endpoint.port_of(config["base_url"]) < 65536):
                    raise OSError("packaged gateway observation metadata is invalid")
                gateway = Gateway(home=paths.home(environ),
                                  state_root=state_root if state_root is not None else paths.state_root(dict(environ)),
                                  environ=environ, gateway_document=document)
            return gateway.observe()
        except (errors.CharmerError, ValueError, RecursionError):
            raise OSError("gateway observation is unavailable") from None

    return verify_installation(environ=environ, channel=endpoint.channel(environ), running=layout.installation(),
                               observe=observe if sys.platform.startswith("linux") else None,
                               from_dir=from_dir, disclose=disclose, platform=sys.platform)


def _process_identity(proc_root: Path, pid: int) -> tuple:
    """Identity around the opened executable: namespace, proc inode, start ticks."""
    namespace = os.readlink(proc_root / "self/ns/pid")
    fd = _directory(proc_root / str(pid))
    try:
        if os.readlink("ns/pid", dir_fd=fd) != namespace:
            raise _Problem("unavailable", "gateway PID is in a different namespace")
        handle = os.open("stat", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        try:
            raw = os.read(handle, 4097)
        finally:
            os.close(handle)
        if len(raw) > 4096 or int(raw.split(b" ", 1)[0]) != pid:
            raise ValueError("invalid process identity")
        fields = raw[raw.rindex(b")") + 1:].split()
        start_ticks = int(fields[19])  # field 22, after pid and (comm)
        directory = os.fstat(fd)
        executable = os.stat("exe", dir_fd=fd)
        return namespace, directory.st_dev, directory.st_ino, start_ticks, _identity(executable)
    finally:
        os.close(fd)


def verify_running_gateway(observe: Callable[[], Observation] | None, expected: str, *,
                           previous_digest: str | None = None, proc_root: Path = Path("/proc"),
                           platform: str = sys.platform) -> Check:
    """Hash /proc/PID/exe itself, never the pathname in the exec stamp.

    A retained local identity is only a diagnostic for different-running-build,
    never signed evidence and never a successful verification. Deleted Linux
    executables remain readable through the opened proc handle when ownership
    can still be proven; an unavailable ownership observation stays incomplete.
    """
    component = "running-gateway"
    if observe is None:
        detail = ("gateway ownership observation is unavailable" if platform.startswith("linux") else
                  "running executable bytes can only be verified on Linux; gateway was not probed")
        return Check(component, "unavailable", detail)
    try:
        first = observe()
        if first.state == "stopped":
            if observe().state != "stopped":
                raise _Problem("unavailable", "gateway changed during verification")
            limitation = "" if platform.startswith("linux") else "; running bytes are only inspectable on Linux"
            return Check(component, "stopped", "gateway is stopped; no running executable to verify" + limitation)
        if not platform.startswith("linux"):
            return Check(component, "unavailable", "running executable bytes can only be verified on Linux")
        stamp = first.stamp
        if (first.state != "ours" or stamp is None or first.lock is not True
                or first.listener.kind != "ours" or first.listener.pid != stamp.pid):
            raise _Problem("unavailable", "gateway is foreign, starting or ownership is unproven")
        before = _process_identity(proc_root, stamp.pid)
        if stamp.pid_namespace != before[0] or observe() != first:
            raise _Problem("unavailable", "gateway identity changed or its PID namespace is unproven")
        fd = os.open(proc_root / str(stamp.pid) / "exe", os.O_RDONLY | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _Problem("unavailable", "running executable is not a regular file")
            digest = hashlib.sha256()
            while chunk := os.read(fd, _CHUNK):
                digest.update(chunk)
            if (_identity(os.fstat(fd)) != _identity(info)
                    or _identity(os.stat(proc_root / str(stamp.pid) / "exe")) != _identity(info)
                    or _process_identity(proc_root, stamp.pid) != before or observe() != first):
                raise _Problem("unavailable", "gateway exited, restarted or changed during hashing")
        finally:
            os.close(fd)
        actual = digest.hexdigest()
        if actual == expected:
            return Check(component, "current-match", "opened running executable matches the authenticated gateway", expected, actual)
        if previous_digest is not None and actual == previous_digest:
            return Check(component, "different-running-build", "running bytes match the retained bundle's local identity, "
                         "not the authenticated installed build; a restart may be pending. "
                         "Retained identity is not signed evidence", expected, actual)
        return Check(component, "mismatch", "running executable differs from the authenticated gateway", expected, actual)
    except _Problem as exc:
        return Check(component, exc.status, exc.detail)
    except (OSError, ValueError, IndexError):
        return Check(component, "unavailable", "running executable or process identity cannot be read")
