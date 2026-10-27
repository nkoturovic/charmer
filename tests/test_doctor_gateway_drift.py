"""On-disk byte comparison is separate from gateway ownership and retention."""

from __future__ import annotations

import dataclasses
import io
import os
from pathlib import Path
from unittest import mock

from charmer import gateway_lifecycle as gl, tui
from charmer.cli import entry
from charmer.cli.screens import gateway_actions
from test_gateway_doctor import _GatewayDoctor
from test_tui import ESC, FakeWindow


class GatewayByteDriftTests(_GatewayDoctor):
    def setUp(self) -> None:
        super().setUp()
        self.ready("aaaaaaaaaaaaaaaa")
        self.running_path = self.root / "cli-proxy-api"
        self.installed_path = self.root / "new-cli-proxy-api"
        self.running_path.write_bytes(b"fixture gateway bytes\n")
        self.installed_path.write_bytes(self.running_path.read_bytes())
        self.runtime.environ["CHARMER_PROXY_BIN"] = str(self.installed_path)

    def test_same_bytes_on_another_path_are_optional_in_status_and_doctor(self) -> None:
        gateway = self.runtime.gateway()
        drift = gateway.binary_drift(gateway.observe())
        self.assertIn("same binary on another path; restart optional", drift)
        self.assertIn("on-disk", drift)
        self.assertIn("running bytes not verified", drift)
        self.assertNotIn("restart pending", drift)
        self.assertEqual(gateway.running_binary(), str(self.running_path))
        out = io.StringIO()
        code = entry.main(["gateway", "status"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(drift, out.getvalue())
        attention, info = self.facts()
        self.assertIn(drift, info)
        self.assertNotIn(drift, attention)

    def test_gateway_dialog_shows_the_same_optional_drift_text(self) -> None:
        gateway = self.runtime.gateway()
        drift = gateway.binary_drift(gateway.observe())
        with mock.patch.object(tui, "Modal") as modal:
            modal.return_value.run.return_value = None
            gateway_actions.dialog(self.runtime, FakeWindow([ESC], height=40, width=120), tui.MONO_PALETTE)
        text = " ".join(modal.call_args.args[1])
        self.assertIn("same binary on another path; restart optional", text)
        self.assertIn(" ".join(str(drift).split()), " ".join(text.split()))

    def test_changed_bytes_require_restart_on_all_surfaces(self) -> None:
        self.installed_path.write_bytes(b"a changed gateway\n")
        gateway = self.runtime.gateway()
        drift = gateway.binary_drift(gateway.observe())
        self.assertIn("different on-disk SHA-256 hashes", drift)
        self.assertIn("gateway restart pending", drift)
        self.assertNotIn("restart optional", drift)
        attention, info = self.facts()
        self.assertIn(drift, attention)
        self.assertNotIn(drift, info)
        self.assertIn(drift, "\n".join(gateway.status().lines(self.runtime.environ)))
        with mock.patch.object(tui, "Modal") as modal:
            modal.return_value.run.return_value = None
            gateway_actions.dialog(self.runtime, FakeWindow([ESC], height=40, width=120), tui.MONO_PALETTE)
        self.assertIn("different on-disk SHA-256 hashes", " ".join(modal.call_args.args[1]))

    def test_same_path_and_hard_links_are_quiet_only_when_readable(self) -> None:
        gateway = self.runtime.gateway()
        seen = gateway.observe()
        for path in (self.running_path, self.root / "linked-gateway"):
            if path != self.running_path:
                os.link(self.running_path, path)
            gateway.environ["CHARMER_PROXY_BIN"] = str(path)
            self.assertIsNone(gateway.binary_drift(seen))
            with mock.patch.object(gl.os, "open", side_effect=PermissionError("fixture denied")):
                drift = gateway.binary_drift(seen)
                self.assertIsNotNone(drift)
                self.assertIn("gateway restart pending", drift)

    def test_same_file_identity_does_not_read_binary_bytes(self) -> None:
        gateway = self.runtime.gateway()
        seen = gateway.observe()
        linked = self.root / "same-file-gateway"
        os.link(self.running_path, linked)
        for path in (self.running_path, linked):
            gateway.environ["CHARMER_PROXY_BIN"] = str(path)
            with self.subTest(path=path.name), mock.patch.object(
                    gl, "_binary_digest", return_value=(b"fixture", (0, 1))) as digest:
                self.assertIsNone(gateway.binary_drift(seen))
                digest.assert_not_called()

    def test_missing_or_unreadable_paths_never_claim_same_bytes(self) -> None:
        gateway = self.runtime.gateway()
        seen = gateway.observe()
        for path in (self.running_path, self.installed_path):
            with self.subTest(path=path.name):
                saved = path.read_bytes()
                path.unlink()
                self.assertIn("gateway restart pending", gateway.binary_drift(seen))
                path.write_bytes(saved)
        real_open = os.open
        for path in (self.running_path, self.installed_path):
            def opened(name, *args, **kwargs):
                if Path(name) == path:
                    raise PermissionError("fixture denied")
                return real_open(name, *args, **kwargs)
            with self.subTest(unreadable=path.name), mock.patch.object(gl.os, "open", side_effect=opened):
                drift = gateway.binary_drift(seen)
                self.assertIn("gateway restart pending", drift)
                self.assertIn("unreadable", drift)
                self.assertNotIn("same binary", drift)

    def test_an_installation_without_a_locatable_binary_stays_restart_pending(self) -> None:
        gateway = self.runtime.gateway()
        with mock.patch.object(gateway, "installed_binary", return_value=None):
            drift = gateway.binary_drift(gateway.observe())
        self.assertIsNotNone(drift)
        self.assertIn("gateway restart pending", drift)
        self.assertIn("cannot be located", drift)

    def test_nonregular_inputs_and_changed_files_do_not_hash_as_same(self) -> None:
        gateway = self.runtime.gateway()
        seen = gateway.observe()
        self.installed_path.unlink()
        self.installed_path.mkdir()
        self.assertIn("gateway restart pending", gateway.binary_drift(seen))
        self.installed_path.rmdir()
        os.mkfifo(self.installed_path)
        self.assertIn("gateway restart pending", gateway.binary_drift(seen))
        self.installed_path.unlink()
        self.installed_path.write_bytes(self.running_path.read_bytes())
        real_stat = os.stat
        def changed(path, *args, **kwargs):
            if Path(path) == self.installed_path:
                raise FileNotFoundError("fixture replacement during read")
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(gl.os, "stat", side_effect=changed):
            self.assertIn("gateway restart pending", gateway.binary_drift(seen))

    def test_byte_identity_does_not_establish_process_ownership(self) -> None:
        gateway = self.runtime.gateway()
        seen = gateway.observe()
        for status in (gl.FOREIGN, gl.UNKNOWN, gl.STOPPED):
            self.assertIsNone(gateway.binary_drift(dataclasses.replace(seen, state=status)))
        self.assertEqual(gateway.running_binary(), str(self.running_path))
