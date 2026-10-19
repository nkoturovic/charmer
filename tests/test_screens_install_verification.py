"""Explicit H verification: actual widget keys and the same signed core as CLI."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from charmer import install_verify, release_update, tui
from charmer.cli.screens import findings
from test_install_verification_cli import _VerificationCase, snapshot
from test_tui import FakeWindow, RIGHT

PLATFORMS = ("linux", "darwin")
ESC, ENTER = "\x1b", "\n"


class VerificationScreenTests(_VerificationCase):
    def screen(self):
        runtime = SimpleNamespace(environ=self.env, home=Path(self.env["HOME"]),
                                  _session_store_root=Path(self.env["HOME"]) / ".local/state/charmer")
        return findings.FindingsScreen(runtime, palette=tui.MONO_PALETTE,
                                       tty_in=io.StringIO(), tty_out=io.StringIO(), report=lambda: ([], [], []))

    def drive(self, keys, *, online=False, platform="linux"):
        screen = self.screen()
        before = snapshot(self.base)
        shown = []
        real = tui.TextView

        class Recording(real):
            def run(self, win):
                shown.append(tuple(self.lines))
                return super().run(win)

        with self.boundary(online=online, platform=platform), mock.patch.object(tui, "TextView", Recording):
            win = FakeWindow(keys)
            screen.run(win)
        self.assertEqual(snapshot(self.base), before)
        return shown, win

    def offline_keys(self):
        return [ENTER, RIGHT, ENTER, *str(self.fixture.dir), ENTER, ESC, ESC]

    def test_h_enter_downloaded_directory_uses_shared_core_offline(self):
        shown, win = self.drive([ENTER, RIGHT, ENTER, *str(self.fixture.dir), ENTER, ESC, ESC])
        self.assertEqual(shown, [self.fixture.run().lines()])
        self.assertIn("Verify this installation", "\n".join(win.frames))
        self.assertIn("fully offline", "\n".join(win.frames))
        self.assertEqual(self.fixture.run().checks[-1].status, "stopped")

    def test_h_online_confirm_default_cancel_before_any_fetch(self):
        owner = self
        calls, confirmed = [], []
        real_modal = tui.Modal

        class RecordingModal(real_modal):
            def run(self, win, palette, **kwargs):
                value = super().run(win, palette, **kwargs)
                if self.title == "Fetch signed release metadata?":
                    confirmed.append(value)
                return value

        class Metadata:
            def __init__(self, base_url, latest_url=None, **kwargs):
                owner.assertIsNone(latest_url)

            def fetch(self, name, version, limit):
                owner.assertEqual(confirmed, [True], "fetch before online confirmation")
                owner.assertEqual(version, owner.fixture.version)
                calls.append(name)
                return (owner.fixture.dir / name).read_bytes()

        with mock.patch.object(release_update, "HttpsTransport", Metadata), \
                mock.patch.object(tui, "Modal", RecordingModal):
            # The online menu choice alone is not consent; Enter stays Cancel.
            shown, _ = self.drive([ENTER, RIGHT, RIGHT, ENTER, ENTER, ESC], online=True)
            self.assertEqual(confirmed, [False])
            self.assertEqual(calls, [])
            self.assertEqual(shown, [])
            confirmed.clear()
            shown, win = self.drive([ENTER, RIGHT, RIGHT, ENTER, RIGHT, ENTER, ESC, ESC], online=True)
        self.assertEqual(calls, ["SHA256SUMS", "SHA256SUMS.sshsig", "MANIFEST.json"])
        self.assertEqual(shown, [self.fixture.run().lines()])
        self.assertIn(self.fixture.version, "\n".join(win.frames))
        self.assertIn("never latest or a provider", " ".join(" ".join(line.strip(" |") for line in
                      frame.splitlines()) for frame in win.frames))

    def test_all_cancel_paths_write_nothing_and_do_not_fetch(self):
        for keys in ([ESC], [ENTER, ENTER, ESC], [ENTER, ESC, ESC], [ENTER, RIGHT, ENTER, ESC, ESC]):
            with self.subTest(keys=keys), mock.patch.object(install_verify, "verify_current",
                                                          side_effect=AssertionError("cancel ran verification")):
                shown, _ = self.drive(keys)
                self.assertEqual(shown, [])

    def test_cli_and_tui_keep_running_statuses_distinct(self):
        for status in ("current-match", "stopped", "unavailable", "different-running-build", "mismatch"):
            with self.subTest(status=status):
                check = install_verify.Check("running-gateway", status, "fixture running-byte result")
                with mock.patch.object(install_verify, "verify_running_gateway", return_value=check):
                    expected = self.fixture.run()
                    code, machine, _ = self.offline("--json")
                    shown, _ = self.drive(self.offline_keys())
                import json
                self.assertEqual(json.loads(machine), expected.document())
                self.assertEqual(code, expected.exit_code)
                self.assertEqual(shown, [expected.lines()])
                self.assertIn(f"running-gateway: {status}", "\n".join(shown[0]))
                self.assertNotIn("ready", "\n".join(shown[0]).lower())

    def test_h_source_and_nix_show_incomplete_not_signed_bundle_success(self):
        for channel in ("source", "nix"):
            with self.subTest(channel=channel):
                self.env["CHARMER_CHANNEL"] = channel
                shown, _ = self.drive([ENTER, RIGHT, RIGHT, ENTER, ESC, ESC])
                self.assertIn("installation: unsupported", shown[0][0])
                self.assertIn("verification is incomplete", "\n".join(shown[0]))

    def test_h_non_linux_offline_reports_limitation_without_tcp_probe(self):
        shown, _ = self.drive(self.offline_keys(), platform="darwin")
        self.assertIn("running-gateway: unavailable", "\n".join(shown[0]))
        self.assertIn("only be verified on Linux", "\n".join(shown[0]))
