"""Shipped help, repeated results, coherent observations and stable floors."""
from __future__ import annotations

import dataclasses
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import tui
from charmer.cli import text
from charmer.cli.screens import actions, findings, launch_sessions, providers, settings
import _tui_render as render
from test_tui import FakeWindow, ENTER, ESC


# The established key paragraphs, joined at their old hard wraps. Keep
# these independent of production constants: a shorter generated help
# must not silently weaken a test by changing its expected text too.
CARD_PARAGRAPHS = {
    "launch": "Enter — launch this profile (a fresh durable session).",
    "get-started": "New here? W — Get started: connect a provider (API key or account sign-in) and pick a profile.",
    "edit": "E — edit this profile (^S saves; sessions that follow it are offered the change).",
    "next-profile": "Tab / Shift-Tab — next / previous profile (connected first, then most recently used).",
    "profiles": "P — profiles: use, create, copy, rename, delete, restore shipped versions, make one the default; B there edits named bindings; F lists fallback profiles (one provider each) for an outage.",
    "direct": "D — direct session: one model as the lead, no agents (custom models too).",
    "sessions": "S — sessions: resume, details (V), change a lineup (T), follow/pin (F), rename (R), forget (X).",
    "providers": "G — providers: Enter connects, K API key, L sign in or out, T test, X remove, P apply.",
    "models": "M — models: Enter inspects a line (or admits a new one); candidates can be declared.",
    "settings": "O — settings: compaction, Explore, workflow default, review rounds, kept environment, token.",
    "details": "V — full details: every row, the /model lead set, the kept environment, managed differences.",
    "doctor": "H — doctor: its findings as a list (BLOCKED, Attention by area, then the advisories); Enter acts on a row or shows its command, V the full report, W the gateway's actions.",
    "update": "U — check for and install a newer charmer: an installed release checks, asks and applies; a Nix install updates through its flake. Claude Code itself: W → Claude Code.",
    "help": "? — this help, then the workflow guarantees below.",
    "quit": "Esc — quit without launching (in a dialog or a text field, Esc cancels it).",
}
RESUME_PARAGRAPHS = {
    "resume": "Enter — resume this session with the lineup shown (a resume keeps the session's lineup).",
    "details": CARD_PARAGRAPHS["details"],
    "sessions": "S — sessions: change this session's lineup (T), follow/pin (F), rename (R), forget (X).",
    "doctor": CARD_PARAGRAPHS["doctor"],
    "update": CARD_PARAGRAPHS["update"],
    "help": CARD_PARAGRAPHS["help"],
    "cancel": "Esc — cancel: back without resuming (in a dialog or a text field, Esc cancels it).",
}


class ShippedHelpTests(unittest.TestCase):
    def test_existing_key_paragraphs_survive_in_the_table_and_shipped_help(self):
        for screen, paragraphs, shown in (("card", CARD_PARAGRAPHS, launch_sessions.CARD_HELP),
                                           ("resume-card", RESUME_PARAGRAPHS, launch_sessions.RESUME_HELP)):
            for verb, paragraph in paragraphs.items():
                with self.subTest(screen=screen, key=verb):
                    self.assertIn(paragraph, actions.action(f"{screen}.{verb}").help)
                    self.assertIn(paragraph, " ".join(shown.split()))
        self.assertIn("{sign_in}", launch_sessions.RESUME_HELP)

    def test_compatibility_names_are_the_actual_generated_help(self):
        self.assertIs(text.QUICK_HELP, launch_sessions.CARD_HELP)
        self.assertIs(text.RESUME_QUICK_HELP, launch_sessions.RESUME_HELP)

    def test_the_cheatsheet_hint_immediately_follows_the_help_key(self):
        for shown in (launch_sessions.CARD_HELP, launch_sessions.RESUME_HELP):
            lines = shown.splitlines()
            with self.subTest(title=lines[0]):
                self.assertIn(CARD_PARAGRAPHS["help"], lines)
                index = lines.index(CARD_PARAGRAPHS["help"])
                self.assertEqual(lines[index + 1], tui.CHEATSHEET_HINT)
                self.assertEqual(lines.count(tui.CHEATSHEET_HINT), 1)


class ScreenContractsTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="screen-contracts-"))
        self.runtime = self.enterContext(render.golden_runtime(Path(root)))

    def test_identical_long_refusals_are_acknowledged_each_time(self):
        screen = findings.FindingsScreen(self.runtime, palette=tui.MONO_PALETTE, report=lambda: ([], [], []),
                                         tty_in=io.StringIO(), tty_out=io.StringIO())
        win = FakeWindow([ENTER, ESC, ENTER, ESC, ESC], width=80, height=24)
        with mock.patch.object(screen, "_enter", side_effect=lambda _win: screen._say(text.GS_IN_SESSION, "warn")) as enter:
            screen.run(win)
        self.assertEqual(enter.call_count, 2)
        modals = [frame for frame in win.frames if "Action result" in frame]
        self.assertEqual(len(modals), 2)
        for frame in modals:
            body = " ".join(line.split("|", 2)[1].strip() for line in frame.splitlines() if "|" in line)
            self.assertIn(text.GS_IN_SESSION, body)
        self.assertEqual(screen.message, "")

    def test_each_provider_refresh_reads_the_served_models_once(self):
        screen = providers._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        with mock.patch.object(self.runtime, "served_models_callback", wraps=self.runtime.served_models_callback) as read:
            screen._load_facts(refresh_pool=True)
            self.assertEqual(read.call_count, 1)
            screen._load_facts(refresh_pool=True)
            self.assertEqual(read.call_count, 2, "each refresh gets a fresh snapshot")

    def test_settings_floor_does_not_change_with_selection(self):
        screen = settings._SettingsScreen(self.runtime, tui.MONO_PALETTE, tty_in=io.StringIO(), tty_out=io.StringIO())
        for width in (80, 100, 120):
            floors = set()
            for index in range(len(screen.entries)):
                screen.index = index
                floors.add(screen.min_size(width))
            self.assertEqual(len(floors), 1, (width, floors))

    def test_providers_floor_does_not_change_with_selection(self):
        screen = providers._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        for width in (80, 100, 120, 160):
            floors = set()
            for index in range(len(screen.rows)):
                screen.selected = index
                floors.add(screen.min_size(width))
            self.assertEqual(len(floors), 1, (width, floors))

    def test_signed_in_account_offers_sign_out_as_a_distinct_action(self):
        screen = providers._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        screen.selected = next(i for i, fact in enumerate(screen.facts) if fact["kind"] == "oauth-pool")
        pid = screen._fact()["id"]
        screen.connections[pid] = dataclasses.replace(screen.connections[pid], accounts=("fixture@example.com",))
        bar = screen._keybar()
        self.assertIn(("X", "sign out"), bar.bindings)
        for width in (80, 100, 120):
            win = FakeWindow([], width=width, height=24)
            bar.draw(win, 23, tui.MONO_PALETTE)
            self.assertIn("X sign out", win.text())
