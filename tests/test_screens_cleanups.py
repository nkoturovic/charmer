"""Small presentation regressions over injected facts only."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from charmer import discovery, gateway_lifecycle, quota, tui, views
from charmer.cli.screens import gateway_actions

import _tui_fixture as fx
from test_tui import FakeWindow


class ShortcutLabelsTests(unittest.TestCase):
    def test_both_cards_advertise_the_findings_gateway_key(self) -> None:
        from charmer.cli import text

        for help_text in (text.QUICK_HELP, text.RESUME_QUICK_HELP):
            with self.subTest(help_text=help_text.splitlines()[0]):
                self.assertIn("W the gateway's actions", help_text)
                self.assertNotIn("G the gateway's actions", help_text)

    def test_details_and_named_binding_aliases_are_silent(self) -> None:
        from charmer.cli import text

        for bar in (text.PROVIDERS_KEYBAR_KEY_MISSING, text.PROVIDERS_KEYBAR_ACCOUNT,
                    text.PROVIDERS_KEYBAR_ACCOUNT_KEY):
            self.assertIn(("V", "details"), bar)
            self.assertNotIn(("Q", "details"), bar)
        self.assertIn(("B", "named bindings"), tui.PROFILE_EDITOR_KEYBAR)
        self.assertNotIn(("N", "named bindings"), tui.PROFILE_EDITOR_KEYBAR)
        self.assertIn("B edits named bindings", tui.PROFILE_EDITOR_HELP)
        self.assertIn("V shows details", text.PROVIDERS_HELP)


class PresentationTests(unittest.TestCase):
    def test_stop_advice_only_for_a_running_gateway(self) -> None:
        running = (gateway_lifecycle.OURS, gateway_lifecycle.STARTING)
        for status in (gateway_lifecycle.STOPPED, *running,
                       gateway_lifecycle.FOREIGN, gateway_lifecycle.UNKNOWN, None):
            with self.subTest(status=status):
                seen = SimpleNamespace(state=status, detail="fixture") if status is not None else None
                view = gateway_actions.GatewayView(None, seen)
                bodies = []

                def cancel(modal, *_args, **_kwargs):
                    bodies.append(" ".join(modal.lines))
                    return None

                with mock.patch.object(gateway_actions, "observe", return_value=view), \
                        mock.patch.object(tui.Modal, "run", cancel):
                    gateway_actions.dialog(None, FakeWindow(), tui.MONO_PALETTE)
                self.assertEqual("Stop it from a terminal" in bodies[0], status in running)

    def test_quota_fallback_names_the_profiles_filter_at_both_widths(self) -> None:
        pool = quota.PoolStatus("ok", credentials=quota.parse_auth_files(fx.quota_body(percent=95, reset=False)))
        pools = {credential.provider for credential in pool.credentials}
        for width, remedy in ((200, "P → F fallback profiles"), (45, "P → F")):
            with self.subTest(width=width):
                text, role = quota.card_summary(pool, pools=pools, login_commands={}, now=fx.FIXED_NOW, width=width)
                self.assertTrue(text.endswith("— " + remedy), text)
                self.assertEqual(role, "warn")

    def test_openrouter_help_does_not_hard_wrap_paragraphs(self) -> None:
        paragraph = next(line for line in discovery.OPENROUTER_HELP.splitlines() if line.startswith("Regular models"))
        self.assertIn("are pre-release, may retain prompts", paragraph)
        self.assertIn("with a key, a separately confirmed", paragraph)
        self.assertTrue(paragraph.endswith("account-only rows."))

    def test_not_connected_card_calls_h_checks(self) -> None:
        self.assertIn(("H", "checks"), views.CARD_KEYS_NOT_CONNECTED)
        self.assertNotIn(("H", "doctor"), views.CARD_KEYS_NOT_CONNECTED)
