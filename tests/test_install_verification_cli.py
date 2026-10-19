"""Verification through the actual entry, with signed fixtures and private roots.

No Runtime is injected into the CLI. All process observations use a fake proc
view; every socket, mutating operation and credential open is a tripwire.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fake_release import make_key, needs_ssh_keygen
from charmer import acquire, gateway_lifecycle, install_verify, layout, pin, release_update, self_update, trust
from charmer.cli import entry, parser, runtime, streams
from test_install_verification import PLATFORM, SignedInstallation, put

PLATFORMS = ("linux", "darwin")


def snapshot(root):
    """Content, links, modes and modification times; reads may change atime."""
    return {str(path.relative_to(root)): (path.lstat().st_mode, path.lstat().st_mtime_ns,
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
            for path in root.rglob("*")}


@needs_ssh_keygen()
class _VerificationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = make_key(Path(cls.keys.name))

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = SignedInstallation(self.base, self.key)
        self.env = {**self.fixture.environ, "CHARMER_CHANNEL": "bundle", "PATH": "",
                    "CHARMER_MANAGED_ID": "fixture-session"}
        self.proc = self.base / "proc"
        for name in ("tcp", "tcp6"):
            put(self.proc / "net" / name, b"fixture table header\n")
        self.out, self.err = io.StringIO(), io.StringIO()

    @contextlib.contextmanager
    def boundary(self, *, online=False, platform="linux"):
        real_gateway = gateway_lifecycle.Gateway
        real_open, real_io_open = os.open, io.open
        secrets = {"api-key", "previous-key", "config.yaml", "management-key", "provider-keys.env"}

        def opened(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR), f"write open: {path}")
            self.assertNotIn(Path(path).name, secrets)
            return real_open(path, flags, *args, **kwargs)

        def io_opened(path, mode="r", *args, **kwargs):
            if not isinstance(path, int):
                self.assertNotIn(Path(path).name, secrets)
            self.assertFalse(any(flag in mode for flag in "wax+"), f"write mode: {mode}")
            return real_io_open(path, mode, *args, **kwargs)

        def gateway(**kwargs):
            self.assertEqual(platform, "linux", "non-Linux observation must not make TCP probes")
            return real_gateway(**kwargs, platform="linux", seams=gateway_lifecycle.Seams(proc_root=self.proc))

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
            stack.enter_context(mock.patch.object(layout, "installation", return_value=self.fixture.bundle))
            stack.enter_context(mock.patch.object(pin, "host_platform", return_value=PLATFORM))
            stack.enter_context(mock.patch.object(trust, "release_trust_path", return_value=self.fixture.signers))
            stack.enter_context(mock.patch.object(install_verify.sys, "platform", platform))
            stack.enter_context(mock.patch.object(gateway_lifecycle, "Gateway", side_effect=gateway))
            stack.enter_context(mock.patch.object(socket, "socket", side_effect=AssertionError("no sockets")))
            stack.enter_context(mock.patch.object(socket, "create_connection", side_effect=AssertionError("no TCP")))
            stack.enter_context(mock.patch.object(os, "open", side_effect=opened))
            stack.enter_context(mock.patch.object(io, "open", side_effect=io_opened))
            stack.enter_context(mock.patch.object(runtime, "Runtime", side_effect=AssertionError("no Runtime")))
            stack.enter_context(mock.patch.object(streams, "_open_tty_streams", side_effect=AssertionError("no tty")))
            stack.enter_context(mock.patch.object(acquire, "acquire", side_effect=AssertionError("no pin acquisition")))
            stack.enter_context(mock.patch.object(release_update, "journey_for", side_effect=AssertionError("no journey")))
            for name in ("apply", "rollback", "finish_switch", "tidy", "switch"):
                stack.enter_context(mock.patch.object(self_update, name, side_effect=AssertionError("no " + name)))
            for name in ("status", "ensure", "stop", "restart", "hold"):
                if hasattr(real_gateway, name):
                    stack.enter_context(mock.patch.object(real_gateway, name,
                                                         side_effect=AssertionError("no gateway " + name)))
            if not online:
                stack.enter_context(mock.patch.object(release_update, "HttpsTransport",
                                                     side_effect=AssertionError("offline transport construction")))
            stack.enter_context(contextlib.redirect_stderr(self.err))
            yield

    def invoke(self, flags, *, online=False, platform="linux"):
        before = snapshot(self.base)
        self.out, self.err = io.StringIO(), io.StringIO()
        with self.boundary(online=online, platform=platform):
            try:
                code = entry.main(["update", *flags], output_stream=self.out, interactive=False)
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(snapshot(self.base), before, "entry changed the private installation or state")
        return code, self.out.getvalue(), self.err.getvalue()

    def offline(self, *flags, **kwargs):
        return self.invoke(["--verify", "--from-dir", str(self.fixture.dir), *flags], **kwargs)



class VerificationEntryTests(_VerificationCase):
    def test_actual_entry_offline_json_is_shared_document_and_changes_nothing(self):
        # A pending transaction and a future marker must not trigger recovery.
        put(self.fixture.root / ".switch.json", b"unreadable interrupted transaction")
        put(Path(self.env["HOME"]) / ".local/state/charmer/state-version", b"999\n")
        put(Path(self.env["HOME"]) / ".config/charmer/api-key", b"fixture secret (must not open)")
        expected = self.fixture.run()
        code, output, error = self.offline("--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), expected.document())
        self.assertEqual(error, "")
        self.assertEqual(json.loads(output)["checks"][-1]["status"], "stopped")
        self.assertNotIn("ready", output.lower())

    def test_actual_entry_offline_text_is_shared_lines(self):
        code, output, error = self.offline()
        self.assertEqual(code, 0, error)
        self.assertEqual(output, "\n".join(self.fixture.run().lines()) + "\n")

    def test_actual_entry_mismatch_and_incomplete_are_non_success(self):
        put(self.fixture.bundle / "bin/charmer", b"modified launcher", 0o755)
        code, output, _ = self.offline("--json")
        self.assertEqual((code, json.loads(output)["status"]), (1, "mismatch"))
        self.fixture.dir.rename(self.base / "unavailable-release")
        code, output, _ = self.offline("--json")
        self.assertEqual((code, json.loads(output)["status"]), (3, "incomplete"))

    def test_actual_entry_online_discloses_before_exact_version_fetches(self):
        calls = []
        owner = self

        class Metadata:
            def __init__(self, base_url, latest_url=None, **kwargs):
                owner.assertIsNone(latest_url)
                owner.assertNotIn("{version}", base_url)
                owner.assertTrue(base_url.endswith("/releases/download/v" + owner.fixture.version))

            def fetch(self, name, version, limit):
                owner.assertIn("contacting", owner.err.getvalue())
                owner.assertIn("exact version", owner.err.getvalue())
                owner.assertIn(owner.fixture.version, owner.err.getvalue())
                owner.assertEqual(owner.out.getvalue(), "")
                owner.assertEqual(version, owner.fixture.version)
                calls.append((name, version))
                return (owner.fixture.dir / name).read_bytes()

        with mock.patch.object(release_update, "HttpsTransport", Metadata):
            code, output, error = self.invoke(["--verify", "--json"], online=True)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), self.fixture.run().document())
        self.assertEqual(calls, [(name, self.fixture.version) for name in
                                 ("SHA256SUMS", "SHA256SUMS.sshsig", "MANIFEST.json")])
        self.assertIn("never latest or a provider", error)

    def test_rollback_selects_exact_older_release_not_latest(self):
        newer = self.fixture
        older = SignedInstallation(self.base / "older", self.key, version="2.0.1")
        older.bundle.rename(newer.root / "versions" / older.version)
        older.bundle = newer.root / "versions" / older.version
        older.signers = older.bundle / "lib/python3.14/site-packages/charmer/data/release-trust/allowed_signers"
        older.root, older.current, older.environ = newer.root, newer.current, newer.environ
        # The real updater's two-link switch is the rollback selection boundary.
        self_update.switch(newer.root, current="2.0.1", previous="2.1.0")
        self.fixture = older
        self.test_actual_entry_online_discloses_before_exact_version_fetches()

    def test_invalid_modes_refuse_before_any_state_or_core_access(self):
        bad = (["--verify", "--check"], ["--verify", "--rollback"], ["--verify", "--yes"],
               ["--verify", "--base-url", "https://release.invalid/{version}"], ["--json"],
               ["--check", "--json"], ["--rollback", "--json"], ["--check", "--yes"],
               ["--rollback", "--from-dir", "unused"], ["--rollback", "--base-url", "unused"],
               ["--from-dir", "unused", "--base-url", "unused"], ["--verify", "--", "--repair"])
        with mock.patch.object(install_verify, "verify_current", side_effect=AssertionError("invalid mode ran")):
            for flags in bad:
                with self.subTest(flags=flags):
                    code, output, error = self.invoke(flags)
                    self.assertEqual(code, 2)
                    self.assertEqual(output, "")
                    self.assertTrue(error)

    def test_missing_directory_is_not_an_online_fallback(self):
        code, output, error = self.invoke(["--verify", "--from-dir", "", "--json"])
        self.assertEqual(code, 3, error)
        self.assertEqual(json.loads(output)["status"], "incomplete")

    def test_interrupted_verification_leaves_no_state_or_partial_json(self):
        with mock.patch.object(install_verify._LocalMetadata, "fetch", side_effect=KeyboardInterrupt):
            code, output, error = self.offline("--json")
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        self.assertIn("cancelled", error)

    def test_source_and_nix_are_unsupported_without_fetch_or_observation(self):
        for channel in ("source", "nix", ""):
            with self.subTest(channel=channel):
                self.env["CHARMER_CHANNEL"] = channel
                with mock.patch.object(gateway_lifecycle.Gateway, "observe", side_effect=AssertionError("no probe")):
                    code, output, error = self.invoke(["--verify", "--json"])
                self.assertEqual(code, 3, error)
                self.assertEqual(json.loads(output)["checks"][0]["status"], "unsupported")

    def test_unrecognized_host_platform_uses_shared_unsupported_result(self):
        from charmer.release_manifest import ReleaseManifestUnavailable

        before = snapshot(self.base)
        with self.boundary(), mock.patch.object(pin, "host_platform", side_effect=ReleaseManifestUnavailable(
                "untrusted platform text\x1b[2J")):
            code = entry.main(["update", "--verify", "--json"], output_stream=self.out, interactive=False)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(code, 3)
        report = json.loads(self.out.getvalue())
        self.assertEqual(report["checks"][0]["status"], "unsupported")
        self.assertNotIn("untrusted platform text", self.out.getvalue())

    def test_actual_package_location_not_hook_or_asset_override_identifies_launcher(self):
        self.env["CHARMER_HOOK_COMMAND"] = str(self.fixture.current / "bin/charmer")
        self.env["CHARMER_ASSETS"] = str(self.fixture.bundle)
        with self.boundary(), mock.patch.object(layout, "installation", return_value=self.base / "other-code"):
            code = entry.main(["update", "--verify", "--from-dir", str(self.fixture.dir), "--json"],
                              output_stream=self.out, interactive=False)
        self.assertEqual(code, 3)
        self.assertIn("not the selected installed release", self.out.getvalue())

    def test_non_linux_offline_never_constructs_gateway_or_probes_tcp(self):
        code, output, error = self.offline("--json", platform="darwin")
        self.assertEqual(code, 3, error)
        report = json.loads(output)
        self.assertEqual(report["checks"][-1]["status"], "unavailable")
        self.assertIn("only be verified on Linux", report["checks"][-1]["detail"])

    def test_json_and_text_neutralize_external_controls(self):
        result = install_verify.Verification(None, None, (
            install_verify.Check("installation", "unavailable", "hostile\x1b[2J\x07\ntext"),))
        with mock.patch.object(install_verify, "verify_current", return_value=result):
            code, text, _ = self.offline()
            self.assertEqual(code, 3)
            self.assertIn("hostile^[[2J^G^Jtext", text)
            _, machine, _ = self.offline("--json")
            self.assertEqual(json.loads(machine), result.document())
            self.assertNotIn("\x1b", machine)

    def test_read_only_and_stdout_census_contract(self):
        args = parser.build_parser().parse_args(["update", "--verify", "--json"])
        self.assertTrue(parser._read_only_listing(args))
        self.assertFalse(parser._writes_versioned_state(args))
        tty, stdout = io.StringIO(), io.StringIO()
        self.assertIs(streams._report_output_stream(args, tty, stdout), stdout)


class VerificationDocsTests(unittest.TestCase):
    def test_verification_contract_and_update_from_old_release_are_explicit(self):
        from _layout import DOCS_DIR

        text = " ".join((DOCS_DIR / "update.md").read_text().split())
        for required in ("installed trust", "outer release manifest", "inner bundle manifest",
                         "manifest_sha256", "bundle-content-v1", "only excluded path", "`MANIFEST.json`",
                         "installer adds no files", "no wildcard cache exclusions", "not ready",
                         "**0** complete", "**1** mismatch", "**3** incomplete or unsupported",
                         "**old gateway continues running**", "old updater does not guarantee refusal",
                         "macOS 13", "Linux only", "the `--verify` flag does not exist",
                         "never bypass a held stop", "not independent attestation"):
            with self.subTest(required=required):
                self.assertIn(required, text)
        reference = " ".join((DOCS_DIR / "reference/cli.md").read_text().split())
        self.assertIn("`update --verify` uses 0 complete, 1 mismatch, 3 incomplete", reference)
        backup = (DOCS_DIR / "guides/backup.md").read_text()
        self.assertIn("if the restored release supports it", backup)
        self.assertIn("charmer update --verify --from-dir <dir>", backup)
        self.assertIn("../update.md#verify-an-installation", backup)

    def test_help_explains_mode_specific_exits_and_no_mutation(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            parser.build_parser().parse_args(["update", "--help"])
        self.assertEqual(caught.exception.code, 0)
        text = " ".join(output.getvalue().split())
        self.assertIn("0 complete, 1 mismatch, 3 incomplete or unsupported", text)
        self.assertIn("no repair, restart or state writes", text)
