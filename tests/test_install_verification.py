"""Signed offline installations and fake proc views; no real process or provider."""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fake_release import FakeRelease, LOCATION, make_key, needs_ssh_keygen, signers_line
from _layout import REPO_ROOT
from _release import load_tool
from charmer import bundle_content, install_verify as verify, pin, release_update, self_update, service, trust
from charmer.gateway_lifecycle import Observation
from charmer.platform.observation import OwnerVerdict

PLATFORMS = ("linux", "darwin")
TARGET = "linux-x86_64"
PLATFORM = "linux-x64"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(document: dict) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()


def put(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def stopped() -> Observation:
    return Observation("stopped", "fixture stopped", None, OwnerVerdict("none", "fixture"), False)


class SignedInstallation:
    """Small genuine content/signed-metadata fixture; no installed code is executed."""
    def __init__(self, base: Path, key: Path, *, version: str = "2.1.0", gateway: bytes = b"fixture gateway\n"):
        self.key = key
        self.version = version
        self.environ = {"HOME": str(base / "home")}
        self.root = self_update.install_root(self.environ)
        self.bundle = self.root / "versions" / version
        self.bundle.mkdir(parents=True)
        self.current = self.root / "current"
        self.current.symlink_to(f"versions/{version}")
        self.dir = base / "release"
        self.dir.mkdir()
        self.gateway = gateway
        self.client = b"fixture managed client\n"
        self.client_path = pin.owned_path(self.environ, "2.1.300", PLATFORM)
        put(self.client_path, self.client, 0o755)
        put(self.bundle / verify.GATEWAY_PATH, gateway, 0o755)
        put(self.bundle / "bin/charmer", b"fixture launcher\n", 0o755)
        put(self.bundle / "bin/charmer-proxy", b"fixture proxy launcher\n", 0o755)
        put(self.bundle / "runtime/python/bin/python3.14", b"fixture interpreter\n", 0o755)
        (self.bundle / "runtime/python/bin/python3").symlink_to("python3.14")
        put(self.bundle / "lib/python3.14/site-packages/charmer/module.py", b"fixture module\n")
        self.signers = self.bundle / "lib/python3.14/site-packages/charmer/data/release-trust/allowed_signers"
        put(self.signers, signers_line(key).encode())
        self.builder = load_tool(REPO_ROOT / "tools/_build/bundle.py", "verification_bundle")
        content, files = self.builder.content_digest(self.bundle)
        self.inner = {"format": 1, "name": "charmer", "version": version, "target": TARGET,
                      "release_date": "2026-10-15", "state_format": 4,
                      "claude_code": {"version": "2.1.300", "platform": PLATFORM,
                                      "sha256": sha(self.client), "size": len(self.client)},
                      "gateway": {"path": verify.GATEWAY_PATH, "sha256": sha(gateway), "size": len(gateway)},
                      "content": {"sha256": content, "files": files}, "release": dict(LOCATION)}
        self.inner_path = self.bundle / "MANIFEST.json"
        put(self.inner_path, json_bytes(self.inner))
        self.asset_name = f"charmer-{version}-{TARGET}.tar.gz"
        archive = b"fixture archive (metadata-only verifier does not download this)\n"
        put(self.dir / self.asset_name, archive)
        self.asset = {"target": TARGET, "size": len(archive), "sha256": sha(archive),
                      "tar_sha256": sha(archive), "gateway_sha256": sha(gateway),
                      "manifest_sha256": sha(self.inner_path.read_bytes()), "content_sha256": content}
        self.outer = {"format": 1, "name": "charmer", "version": version, "release_date": "2026-10-15",
                      "state_format": 4, "claude_code": {"version": "2.1.300", "platforms": {
                          PLATFORM: {"sha256": sha(self.client), "size": len(self.client)}}},
                      "assets": {self.asset_name: self.asset}}
        self.sign()

    def sign(self) -> None:
        put(self.dir / "MANIFEST.json", json_bytes(self.outer))
        FakeRelease.write_sums(self)
        FakeRelease.sign(self, self.key)

    def authenticate_inner(self) -> None:
        put(self.inner_path, json_bytes(self.inner))
        self.asset["manifest_sha256"] = sha(self.inner_path.read_bytes())
        self.sign()

    def run(self, **kwargs) -> verify.Verification:
        values = {"environ": self.environ, "channel": "bundle", "running": self.bundle,
                  "observe": stopped, "from_dir": self.dir, "client_platform": PLATFORM, "platform": "linux"}
        values.update(kwargs)
        with mock.patch.object(trust, "release_trust_path", return_value=self.signers):
            return verify.verify_installation(**values)


@needs_ssh_keygen()
class InstallationVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = make_key(Path(cls.keys.name))
        cls.other_key = make_key(Path(cls.keys.name), "other")

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = SignedInstallation(self.base, self.key)

    def check(self, report, component):
        return next(check for check in report.checks if check.component == component)

    def assertMismatch(self, component, **kwargs):
        result = self.fixture.run(**kwargs)
        self.assertEqual(result.status, "mismatch", result.document())
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.check(result, component).status, "mismatch", result.document())
        return result

    def test_correct_signed_bundle_and_read_only_snapshot(self):
        def snapshot():
            return {str(p.relative_to(self.base)): (p.lstat().st_mode, os.readlink(p) if p.is_symlink()
                    else p.read_bytes() if p.is_file() else None) for p in self.base.rglob("*")}
        before = snapshot()
        result = self.fixture.run()
        self.assertEqual(result.status, "verified", result.document())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual([c.status for c in result.checks], ["match"] * 5 + ["stopped"])
        self.assertEqual(before, snapshot())
        self.assertNotIn(str(self.base), json.dumps(result.document()))
        self.assertIn("Verified against the signed release", "\n".join(result.lines()))

    def test_launcher_modification(self):
        put(self.fixture.bundle / "bin/charmer", b"modified launcher", 0o755)
        self.assertMismatch("bundle-content")

    def test_runtime_modification(self):
        put(self.fixture.bundle / "runtime/python/bin/python3.14", b"modified runtime", 0o755)
        self.assertMismatch("bundle-content")

    def test_executable_mode_change(self):
        (self.fixture.bundle / "bin/charmer").chmod(0o644)
        self.assertMismatch("bundle-content")

    def test_missing_content(self):
        (self.fixture.bundle / "bin/charmer").unlink()
        self.assertMismatch("bundle-content")

    def test_extra_content_including_cache_and_fake_installer_files(self):
        for name in ("extra", "installer.json", ".in-use", "lib/__pycache__/extra.pyc"):
            with self.subTest(name=name):
                path = self.fixture.bundle / name
                put(path, b"unexpected bytes")
                self.assertMismatch("bundle-content")
                path.unlink()

    def test_only_inner_manifest_excluded_and_outer_installer_records_ignored(self):
        self.assertEqual(verify.CONTENT_EXCLUSIONS, ("MANIFEST.json",))
        put(self.fixture.root / "installer.json", b"fixture receipt outside bundle")
        put(self.fixture.root / ".lock/pid", b"fixture lock outside bundle")
        self.assertEqual(self.fixture.run().status, "verified")

    def test_changed_inner_manifest_even_when_self_consistent_is_not_authenticated(self):
        put(self.fixture.bundle / "bin/charmer", b"forged launcher", 0o755)
        content, count = self.fixture.builder.content_digest(self.fixture.bundle, exclude=("MANIFEST.json",))
        self.fixture.inner["content"] = {"sha256": content, "files": count}
        put(self.fixture.inner_path, json_bytes(self.fixture.inner))
        self.assertMismatch("inner-manifest")

    def test_changed_inner_manifest_without_changing_content_is_not_authenticated(self):
        self.fixture.inner["catalog_version"] = 999
        put(self.fixture.inner_path, json_bytes(self.fixture.inner))
        self.assertMismatch("inner-manifest")

    def test_outer_manifest_is_not_the_installed_inner_manifest(self):
        put(self.fixture.dir / "MANIFEST.json", self.fixture.inner_path.read_bytes())
        FakeRelease.write_sums(self.fixture)
        FakeRelease.sign(self.fixture, self.key)
        self.assertMetadataUnavailable()

    def test_unsigned_outer_change(self):
        self.fixture.outer["state_format"] = 9
        put(self.fixture.dir / "MANIFEST.json", json_bytes(self.fixture.outer))
        self.assertMismatch("signed-release")

    def test_forged_sums(self):
        put(self.fixture.dir / "SHA256SUMS", b"0" * 64 + b"  MANIFEST.json\n")
        self.assertMismatch("signed-release")

    def test_forged_signature_and_directory_key_never_supplies_trust(self):
        FakeRelease.sign(self.fixture, self.other_key)
        put(self.fixture.dir / "allowed_signers", signers_line(self.other_key).encode())
        self.assertMismatch("signed-release")

    def test_invalid_signature(self):
        put(self.fixture.dir / "SHA256SUMS.sshsig", b"not a signature")
        report = self.assertMismatch("signed-release")
        self.assertIn("Release metadata authentication failed. Do not use these files", "\n".join(report.lines()))
        self.assertNotIn("Reinstall", "\n".join(report.lines()))

    def assertMetadataUnavailable(self, **kwargs):
        report = self.fixture.run(**kwargs)
        self.assertEqual((report.status, report.exit_code), ("incomplete", 3), report.document())
        check = self.check(report, "signed-release")
        self.assertEqual(check.status, "unavailable")
        self.assertIn("--from-dir", check.detail)
        self.assertNotIn("Reinstall", "\n".join(report.lines()))
        return report

    def test_wrong_release_version(self):
        other = SignedInstallation(self.base / "other", self.key, version="2.0.1")
        self.assertMetadataUnavailable(from_dir=other.dir)

    def test_wrong_release_target(self):
        self.fixture.asset["target"] = "darwin-arm64"
        self.fixture.sign()
        self.assertMetadataUnavailable()

    def test_wrong_inner_target(self):
        self.fixture.inner["target"] = "darwin-arm64"
        self.fixture.authenticate_inner()
        self.assertMismatch("inner-manifest")

    def test_missing_authenticated_inner_identity_is_incomplete(self):
        del self.fixture.asset["manifest_sha256"]
        self.fixture.sign()
        result = self.fixture.run()
        self.assertEqual((result.status, result.exit_code), ("incomplete", 3))

    def test_signed_outer_content_identity_must_equal_inner(self):
        self.fixture.asset["content_sha256"] = "1" * 64
        self.fixture.sign()
        self.assertMismatch("inner-manifest")

    def test_corrupt_gateway(self):
        put(self.fixture.bundle / verify.GATEWAY_PATH, b"corrupt gateway", 0o755)
        self.assertMismatch("gateway")

    def test_gateway_not_just_whole_content_digest(self):
        self.fixture.inner["gateway"]["sha256"] = "1" * 64
        self.fixture.asset["gateway_sha256"] = "1" * 64
        self.fixture.authenticate_inner()
        self.assertMismatch("gateway")

    def test_corrupt_managed_client(self):
        put(self.fixture.client_path, b"fixture managed clienT\n", 0o755)
        self.assertMismatch("managed-client")

    def test_missing_managed_client(self):
        self.fixture.client_path.unlink()
        self.assertMismatch("managed-client")

    def test_managed_client_authenticated_outer_inner_disagreement(self):
        self.fixture.outer["claude_code"]["platforms"][PLATFORM]["sha256"] = "1" * 64
        self.fixture.sign()
        self.assertMismatch("managed-client")

    def test_offline_zero_network_attempts_even_when_supplied_disclosure(self):
        with mock.patch.object(release_update, "HttpsTransport", side_effect=AssertionError("network transport")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network attempt")):
            disclosure = mock.Mock(side_effect=AssertionError("offline disclosure"))
            self.assertEqual(self.fixture.run(disclose=disclosure).exit_code, 0)
            disclosure.assert_not_called()

    def test_online_exact_version_disclosed_before_first_fetch(self):
        events = []
        local = self_update.DirectoryTransport(self.fixture.dir)
        transport = mock.Mock()
        def fetch(name, version, limit):
            self.assertTrue(events)
            self.assertEqual(version, "2.1.0")
            events.append((name, version))
            return local.fetch(name, version, limit)
        transport.fetch.side_effect = fetch
        with mock.patch.object(release_update, "HttpsTransport", return_value=transport) as make:
            result = self.fixture.run(from_dir=None, disclose=events.append)
        self.assertEqual(result.exit_code, 0, result.document())
        self.assertIn("exact version", events[0])
        self.assertEqual([e[0] for e in events[1:]], ["SHA256SUMS", "SHA256SUMS.sshsig", "MANIFEST.json"])
        canonical = json.loads((REPO_ROOT / "packaging/product.json").read_text())["release_base_url"]
        self.assertEqual(make.call_args.args, (canonical.replace("{version}", "2.1.0"),))
        transport.download.assert_not_called()

    def test_online_transport_uses_exact_urls_and_refuses_foreign_redirects(self):
        events = []
        transport = release_update.verification_transport("2.0.1", environ={}, disclose=events.append)
        seen = []
        def opener(url, **kwargs):
            self.assertIn("2.0.1", events[0])
            seen.append(url)
            return io.BytesIO(b"fixture metadata")
        transport._opener = opener
        self.assertEqual(transport.fetch("MANIFEST.json", "2.0.1", 1024), b"fixture metadata")
        canonical = json.loads((REPO_ROOT / "packaging/product.json").read_text())["release_base_url"]
        self.assertEqual(seen, [canonical.replace("{version}", "2.0.1") + "/MANIFEST.json"])
        response = io.BytesIO(b"foreign")
        response.geturl = lambda: "https://other.invalid/MANIFEST.json"
        transport._opener = lambda *args, **kwargs: response
        with self.assertRaises(release_update.TransportError):
            transport.fetch("MANIFEST.json", "2.0.1", 1024)
        self.assertTrue(response.closed)

    def test_no_signature_is_incomplete(self):
        (self.fixture.dir / "SHA256SUMS.sshsig").unlink()
        self.assertEqual(self.fixture.run().exit_code, 3)

    def test_online_without_disclosure_never_fetches(self):
        with mock.patch.object(release_update, "HttpsTransport", side_effect=AssertionError("fetch")):
            result = self.fixture.run(from_dir=None)
        self.assertEqual((result.status, result.exit_code), ("incomplete", 3))

    def test_source_nix_and_wrong_running_prefix_never_signed_success(self):
        for channel in ("source", "nix", None):
            with self.subTest(channel=channel), mock.patch.object(trust, "release_signers") as keys:
                result = self.fixture.run(channel=channel)
                self.assertEqual((result.status, result.exit_code), ("incomplete", 3))
                keys.assert_not_called()
        self.assertEqual(self.fixture.run(running=self.base).exit_code, 3)

    def test_rollback_verifies_exact_older_selected_version(self):
        old = SignedInstallation(self.base / "older", self.key, version="2.0.1")
        shutil.copytree(old.bundle, self.fixture.root / "versions/2.0.1", symlinks=True)
        (self.fixture.root / "previous").symlink_to("versions/2.0.1")
        installation = self_update.read_installation(self.fixture.root)
        @contextlib.contextmanager
        def inhibit():
            yield None
        result = self_update.rollback(self.fixture.root, state_root=self.base / "state", hold=lambda: None,
                                      inhibit=inhibit, confirmed=installation)
        self.assertEqual(result.version, "2.0.1")
        events = []
        transport = mock.Mock(wraps=self_update.DirectoryTransport(old.dir))
        with mock.patch.object(release_update, "HttpsTransport", return_value=transport):
            report = self.fixture.run(running=self.fixture.current, from_dir=None, disclose=events.append)
        self.assertEqual((report.version, report.exit_code), ("2.0.1", 0), report.document())
        self.assertTrue(all(call.args[1] == "2.0.1" for call in transport.fetch.call_args_list))
        self.assertNotIn("2.1.0", events[0])

    def test_owned_old_gateway_after_update_is_not_the_current_signed_build(self):
        older = SignedInstallation(self.base / "older", self.key, version="2.0.1", gateway=b"old fixture gateway\n")
        shutil.copytree(older.bundle, self.fixture.root / "versions/2.0.1", symlinks=True)
        (self.fixture.root / "previous").symlink_to("versions/2.0.1")
        proc = self.base / "proc"
        for directory in (proc / "self/ns", proc / "321/ns"):
            directory.mkdir(parents=True)
            (directory / "pid").symlink_to("pid:[123]")
        running = self.fixture.root / "versions/2.0.1" / verify.GATEWAY_PATH
        (proc / "321/exe").symlink_to(running)
        fields = ["S"] + ["0"] * 18 + ["1234"] + ["0"] * 20
        put(proc / "321/stat", f"321 (fixture) {' '.join(fields)}\n".encode())
        stamp = service.ExecStamp("2.0.1", "fixture", 321, "/nonexistent/stamp-path",
                                  "2026-10-15T00:00:00+00:00", "pid:[123]", "a" * 32)
        observation = Observation("ours", "fixture", stamp, OwnerVerdict("ours", "fixture", os.geteuid(), 321), True)
        report = self.fixture.run(observe=lambda: observation, proc_root=proc)
        self.assertEqual(self.check(report, "running-gateway").status, "different-running-build", report.document())
        self.assertEqual(report.exit_code, 1)
        # Losing the retained bytes loses even that local diagnostic; it must
        # not turn a local manifest's assertion into authenticated evidence.
        put(running, self.fixture.gateway, 0o755)
        self.assertEqual(self.fixture.run(observe=lambda: observation, proc_root=proc).exit_code, 0)

    def test_optional_previous_link_problems_do_not_fail_current_verification(self):
        previous = self.fixture.root / "previous"
        observe = mock.Mock(side_effect=stopped)
        for target in (None, "../outside", "versions/not-a-version", "versions/2.0.0"):
            with self.subTest(target=target):
                if target is None:
                    put(previous, b"not a link")
                else:
                    previous.symlink_to(target)
                report = self.fixture.run(observe=observe)
                previous.unlink()
                self.assertEqual(report.exit_code, 0, report.document())
                self.assertEqual(self.check(report, "running-gateway").status, "stopped")
        real = os.readlink

        def readlink(name, **kwargs):
            if name == "previous":
                raise PermissionError("fixture denied")
            return real(name, **kwargs)

        with mock.patch.object(verify.os, "readlink", side_effect=readlink):
            report = self.fixture.run(observe=observe)
        self.assertEqual(report.exit_code, 0, report.document())
        self.assertTrue(observe.called)

    def test_optional_previous_development_bundle_supplies_local_comparison(self):
        previous = self.fixture.root / "versions/2.1.0-dev"
        shutil.copytree(self.fixture.bundle, previous, symlinks=True)
        inner = {**self.fixture.inner, "version": "2.1.0-dev"}
        put(previous / "MANIFEST.json", json_bytes(inner))
        (self.fixture.root / "previous").symlink_to("versions/2.1.0-dev")
        with mock.patch.object(verify, "verify_running_gateway", wraps=verify.verify_running_gateway) as running:
            report = self.fixture.run()
        self.assertEqual(report.exit_code, 0, report.document())
        self.assertEqual(running.call_args.kwargs["previous_digest"], sha(self.fixture.gateway))

    def test_running_only_difference_recommends_guarded_restart(self):
        for status in ("different-running-build", "mismatch"):
            with self.subTest(status=status), mock.patch.object(verify, "verify_running_gateway", return_value=
                    verify.Check("running-gateway", status, "fixture running difference")):
                report = self.fixture.run()
                self.assertEqual((report.status, report.exit_code), ("mismatch", 1))
                text = "\n".join(report.lines())
                self.assertIn("restart between turns with charmer gateway restart (it follows the persistence hold)", text)
                self.assertNotIn("Reinstall", text)
                self.assertNotIn("--rollback", text)
                for other_status in ("mismatch", "unavailable"):
                    with mock.patch.object(verify, "_client", return_value=
                            verify.Check("managed-client", other_status, "fixture client problem")):
                        incomplete = self.fixture.run()
                    self.assertEqual(incomplete.exit_code, 1)
                    self.assertNotIn("restart between turns", "\n".join(incomplete.lines()))

    def test_current_selection_changed_during_verification_is_incomplete(self):
        def observe():
            self.fixture.current.unlink()
            self.fixture.current.symlink_to("versions/2.0.1")
            return stopped()
        self.assertEqual(self.fixture.run(observe=observe).exit_code, 3)

    def test_symlink_cannot_redirect_manifest_gateway_or_managed_client(self):
        for path, component in ((self.fixture.inner_path, "inner-manifest"),
                                (self.fixture.bundle / verify.GATEWAY_PATH, "bundle-content"),
                                (self.fixture.client_path, "managed-client")):
            with self.subTest(path=path.name):
                content, mode = path.read_bytes(), path.stat().st_mode
                outside = self.base / "outside"
                put(outside, content, mode)
                path.unlink()
                path.symlink_to(outside)
                self.assertMismatch(component)
                path.unlink()
                put(path, content, mode)

    def test_version_directory_symlink_and_parent_link_escape_never_succeed(self):
        original = self.fixture.bundle
        moved = self.base / "moved"
        original.rename(moved)
        original.symlink_to(moved)
        report = self.fixture.run()
        self.assertEqual(report.exit_code, 3)
        original.unlink()
        moved.rename(original)
        self.fixture.current.unlink()
        self.fixture.current.symlink_to("versions/../../moved")
        self.assertMismatch("installation")

    def test_external_metadata_symlinks_refused(self):
        path = self.fixture.dir / "SHA256SUMS"
        outside = self.base / "sums"
        path.rename(outside)
        path.symlink_to(outside)
        self.assertMetadataUnavailable()

    def test_external_metadata_file_type_and_size_are_unavailable(self):
        path = self.fixture.dir / "SHA256SUMS"
        path.unlink()
        for kind in ("directory", "fifo", "oversized"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    path.mkdir()
                elif kind == "fifo":
                    os.mkfifo(path)
                else:
                    put(path, b"x" * (self_update.METADATA_LIMIT + 1))
                try:
                    self.assertMetadataUnavailable()
                finally:
                    if kind == "directory":
                        path.rmdir()
                    else:
                        path.unlink()

    def test_local_metadata_reader_honors_the_requested_bound(self):
        path = self.fixture.dir / "SHA256SUMS"
        put(path, b"fixture metadata")
        reader = verify._LocalMetadata(self.fixture.dir)
        self.assertEqual(reader.fetch(path.name, self.fixture.version, path.stat().st_size), path.read_bytes())
        with self.assertRaises(verify._Problem) as caught:
            reader.fetch(path.name, self.fixture.version, 4)
        self.assertEqual(caught.exception.status, "unavailable")

    def test_directory_symlink_does_not_hide_or_read_external_content(self):
        (self.fixture.bundle / "unexpected-dir").symlink_to(self.base)
        with mock.patch.object(verify.os, "read", wraps=os.read) as reader:
            self.assertMismatch("bundle-content")
        self.assertTrue(reader.called)

    def test_broken_symlink_is_mismatch(self):
        path = self.fixture.bundle / "runtime/python/bin/python3"
        path.unlink()
        path.symlink_to("missing")
        self.assertMismatch("bundle-content")

    def test_special_file_refused_without_blocking(self):
        os.mkfifo(self.fixture.bundle / "fifo")
        self.assertMismatch("bundle-content")

    def test_unreadable_file_is_incomplete_not_success(self):
        original = verify._file
        def read(fd, name, **kwargs):
            if name == "module.py":
                raise PermissionError("fixture denied")
            return original(fd, name, **kwargs)
        with mock.patch.object(verify, "_file", side_effect=read):
            result = self.fixture.run()
        self.assertEqual((result.status, result.exit_code), ("incomplete", 3))

    def test_file_replaced_during_read_is_incomplete(self):
        original = os.read
        changed = False
        def read(fd, size):
            nonlocal changed
            value = original(fd, size)
            if value == b"fixture launcher\n" and not changed:
                changed = True
                path = self.fixture.bundle / "bin/charmer"
                replacement = path.with_name("replacement")
                put(replacement, value, 0o755)
                replacement.replace(path)
            return value
        with mock.patch.object(verify.os, "read", side_effect=read):
            result = self.fixture.run()
        self.assertTrue(changed)
        self.assertEqual((result.status, result.exit_code), ("incomplete", 3))

    def test_link_selection_and_previous_hashed_file_rechecked_at_end(self):
        def observe():
            put(self.fixture.bundle / "bin/charmer", b"changed after hashing", 0o755)
            return stopped()
        result = self.fixture.run(observe=observe)
        self.assertEqual((result.status, result.exit_code), ("incomplete", 3))
        self.assertEqual(self.check(result, "snapshot").status, "unavailable")

    def test_no_gateway_observation_is_incomplete(self):
        result = self.fixture.run(observe=None)
        self.assertEqual((result.status, result.exit_code), ("incomplete", 3))


class ProcessVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.pid = 321
        self.exe = self.root / "executable"
        put(self.exe, b"fixture gateway", 0o755)
        self.expected = sha(self.exe.read_bytes())
        for path in (self.proc / "self/ns", self.proc / str(self.pid) / "ns"):
            path.mkdir(parents=True)
            (path / "pid").symlink_to("pid:[123]")
        (self.proc / str(self.pid) / "exe").symlink_to(self.exe)
        self.stat(1234)
        # A wrong path is deliberate: it must never be opened for hashing.
        self.stamp = service.ExecStamp("2.1.0", "fixture", self.pid, "/nonexistent/stamp-path",
                                      "2026-10-15T00:00:00+00:00", "pid:[123]", "a" * 32)
        self.observation = Observation("ours", "fixture owned", self.stamp,
                                       OwnerVerdict("ours", "fixture", os.geteuid(), self.pid), True)
        self.observe = mock.Mock(side_effect=lambda: self.observation)

    def stat(self, ticks):
        fields = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 20
        put(self.proc / str(self.pid) / "stat", f"{self.pid} (fixture ) name) {' '.join(fields)}\n".encode())

    def run_check(self, **kwargs):
        return verify.verify_running_gateway(self.observe, self.expected, proc_root=self.proc, platform="linux", **kwargs)

    def test_opened_running_bytes_match_not_stamp_path(self):
        result = self.run_check()
        self.assertEqual(result.status, "current-match", result)
        self.assertEqual(result.actual, self.expected)

    def test_previous_build_is_distinct_and_non_success(self):
        put(self.exe, b"old build", 0o755)
        result = self.run_check(previous_digest=sha(b"old build"))
        self.assertEqual(result.status, "different-running-build")
        self.assertIn("not signed evidence", result.detail)
        self.assertEqual(verify.Verification("2.1.0", TARGET, (result,)).exit_code, 1)

    def test_unknown_running_build_is_mismatch(self):
        put(self.exe, b"unrecognized", 0o755)
        result = self.run_check()
        self.assertEqual(result.status, "mismatch")

    def test_deleted_executable_handle_can_match_when_ownership_remains_proven(self):
        # Fake proc view preserves an inode after the original pathname goes,
        # like the kernel's exe link. No real process is inspected or executed.
        retained = self.root / "held-deleted-inode"
        os.link(self.exe, retained)
        self.exe.unlink()
        link = self.proc / str(self.pid) / "exe"
        link.unlink()
        link.symlink_to(retained)
        self.assertEqual(self.run_check().status, "current-match")

    def test_deleted_executable_without_ownership_evidence_is_incomplete(self):
        self.observation = dataclasses.replace(self.observation, state="unknown")
        self.assertEqual(self.run_check().status, "unavailable")

    def test_unreadable_proc_exe(self):
        original = os.open
        def open_file(path, flags, *args, **kwargs):
            if str(path).endswith("/exe"):
                raise PermissionError("fixture unreadable")
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(verify.os, "open", side_effect=open_file):
            self.assertEqual(self.run_check().status, "unavailable")

    def test_foreign_unknown_and_starting_are_not_hashed(self):
        for state in ("foreign", "unknown", "starting"):
            with self.subTest(state=state), mock.patch.object(verify, "_process_identity") as identity:
                self.observation = dataclasses.replace(self.observation, state=state)
                self.assertEqual(self.run_check().status, "unavailable")
                identity.assert_not_called()

    def test_namespace_mismatch(self):
        self.observation = dataclasses.replace(self.observation,
                                               stamp=dataclasses.replace(self.stamp, pid_namespace="pid:[999]"))
        self.assertEqual(self.run_check().status, "unavailable")

    def test_pid_reuse_during_hash(self):
        original = os.read
        def read(fd, size):
            data = original(fd, size)
            if data == b"fixture gateway":
                self.stat(5678)
            return data
        with mock.patch.object(verify.os, "read", side_effect=read):
            self.assertEqual(self.run_check().status, "unavailable")

    def test_restart_during_hash(self):
        original = os.read
        def read(fd, size):
            data = original(fd, size)
            if data == b"fixture gateway":
                self.observation = dataclasses.replace(self.observation,
                    stamp=dataclasses.replace(self.stamp, instance="b" * 32))
            return data
        with mock.patch.object(verify.os, "read", side_effect=read):
            self.assertEqual(self.run_check().status, "unavailable")

    def test_same_pid_exec_replacement_during_hash(self):
        original = os.read
        changed = False
        def read(fd, size):
            nonlocal changed
            data = original(fd, size)
            if data == b"fixture gateway" and not changed:
                changed = True
                replacement = self.root / "replacement"
                put(replacement, data, 0o755)
                replacement.replace(self.exe)
            return data
        with mock.patch.object(verify.os, "read", side_effect=read):
            self.assertEqual(self.run_check().status, "unavailable")

    def test_stopped_and_other_platform_limitations(self):
        result = verify.verify_running_gateway(stopped, self.expected, proc_root=self.proc, platform="linux")
        self.assertEqual(result.status, "stopped")
        result = verify.verify_running_gateway(self.observe, self.expected, proc_root=self.proc, platform="darwin")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("only be verified on Linux", result.detail)

    def test_restart_after_stopped_observation(self):
        observe = mock.Mock(side_effect=[stopped(), self.observation])
        result = verify.verify_running_gateway(observe, self.expected, proc_root=self.proc, platform="linux")
        self.assertEqual(result.status, "unavailable")
