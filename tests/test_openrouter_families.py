"""Conservative, editable declaration defaults; fixture transports only."""
from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

import _tui_render
import test_discovery as fixtures
from _catalog import GOLDENS_ROOT
from _golden import assertGolden
from charmer import discovery, operator, proxy, state, tui
from charmer.cli import consent, onboarding
from charmer.cli.screens import common
from test_tui import DELETE, ENTER, ESC, HOME, FakeWindow


PREFIXES = {
    "anthropic/": "anthropic", "openai/": "openai", "google/": "google",
    "deepseek/": "deepseek", "x-ai/": "x-ai", "moonshotai/": "moonshot",
    "qwen/": "alibaba", "meta-llama/": "meta",
}
KNOWN = frozenset(PREFIXES.values())
PUBLIC = discovery.OPENROUTER_MODELS_URL
WIRE = "deepseek/fixture-reviewer"
NOTE = "family suggested from the deepseek/ prefix"
HINT = "suggested from the deepseek/ prefix — edit to change"


class DeclarationFamilyTests(unittest.TestCase):
    def setUp(self):
        self.docs = fixtures._fixture_docs()
        self.provider = self.docs["providers"]["providers"]["openrouter"]
        patch = mock.patch.object(proxy, "bounded_get", side_effect=AssertionError("unexpected network"))
        patch.start()
        self.addCleanup(patch.stop)

    def declare(self, wire, *, provider_id="openrouter", known=KNOWN, **kwargs):
        entry = {"id": wire, "context_length": 200000, "openrouter": provider_id == "openrouter"}
        return discovery.declaration_line(
            entry, provider=self.provider, agent_efforts=("high", "xhigh"), source_ref="fixture listing",
            provider_id=provider_id, known_families=known, **kwargs)

    def test_every_reviewed_prefix_suggests_only_its_existing_label(self):
        for prefix, family in PREFIXES.items():
            with self.subTest(prefix=prefix):
                wire = prefix + "fixture-model"
                line = self.declare(wire)
                self.assertEqual(line.get("family"), family)
                self.assertEqual(line["wire_model"], wire)
                self.assertEqual(line["notes"], f"family suggested from the {prefix} prefix")
                self.assertNotIn("capabilities", line)
                self.assertNotIn("roles", line)

    def test_unknown_namespaces_and_prefix_boundary_lookalikes_stay_unknown(self):
        for wire in ("mistralai/x", "stealth/space-bunny-alpha", "openrouter/auto", "router/auto",
                     "fixture-account-model", "deepseek", "deepseek/", "deepseek-extra/model",
                     "deepseek.ai/model", "DeepSeek/model", "not/deepseek/model", "/deepseek/model"):
            with self.subTest(wire=wire):
                line = self.declare(wire)
                self.assertEqual(line.get("family", "unknown"), "unknown")
                self.assertNotIn("family suggested", line.get("notes", ""))
        stealth = self.declare("stealth/space-bunny-alpha")
        self.assertEqual(stealth["notes"], discovery.STEALTH_CAUTION +
                         "; Listing-stated modality: unknown; tools: unknown")

    def test_catalog_must_still_trust_each_suggested_label(self):
        for prefix, family in PREFIXES.items():
            with self.subTest(prefix=prefix):
                line = self.declare(prefix + "fixture-model", known=KNOWN - {family})
                self.assertNotIn("family", line)
                self.assertNotIn("notes", line)
        self.assertNotIn("family", self.declare(WIRE, known=()))

    def test_only_openrouter_identity_gets_suggestions_not_lookalike_endpoints(self):
        for provider_id in (None, "kimi", "openai", "openrouter-copy"):
            with self.subTest(provider_id=provider_id):
                line = self.declare(WIRE, provider_id=provider_id)
                self.assertNotIn("family", line)
                self.assertNotIn("notes", line)

    def test_explicit_labels_including_unknown_win_without_a_suggestion_note(self):
        for family in ("acme", "unknown", "deepseek"):
            with self.subTest(family=family):
                line = self.declare(WIRE, family=family)
                self.assertEqual(line["family"], family)
                self.assertNotIn("notes", line)
        stealth = self.declare("stealth/space-bunny-alpha", family="acme")
        self.assertEqual(stealth["family"], "acme")
        self.assertIn(discovery.STEALTH_CAUTION, stealth["notes"])

    def test_note_keeps_other_provenance_and_the_notes_limit(self):
        line = self.declare(WIRE, context_override=300000, over_listed="x" * discovery.OVER_LISTED_MAX)
        self.assertTrue(line["notes"].startswith(NOTE + "; over-listed: "))
        self.assertLessEqual(len(line["notes"]), 512)


class FamilyDiscoveryTests(fixtures.DiscoveryCLICase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(proxy, "bounded_get", side_effect=AssertionError("unexpected network"))
        patch.start()
        self.addCleanup(patch.stop)

    def saved(self):
        return json.loads((self.pdir / "openrouter.json").read_text())["lines"]

    def test_listing_add_suggests_family_and_explains_the_editable_default(self):
        wire = "deepseek/deepseek-v4"
        self.listing(PUBLIC, [{"id": wire, "context_length": 200000}])
        code, out, err = self.op(["discover", "openrouter", "--add", wire], "y\n")
        self.assertEqual(code, 0, err)
        line = next(iter(self.saved().values()))
        self.assertEqual(line.get("family"), "deepseek")
        self.assertEqual(line["notes"], NOTE)
        self.assertEqual(line["wire_model"], wire)
        self.assertIn("family: deepseek (suggested from deepseek/; --family LABEL to change)", out)
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertFalse(self.ledger() and self.ledger().admissions)
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.calls, [])

    def test_cli_explicit_family_wins_without_the_note_or_suggestion_message(self):
        for index, family in enumerate(("acme", "unknown", "deepseek")):
            wire = f"deepseek/fixture-{index}"
            self.listing(PUBLIC, [{"id": wire, "context_length": 200000}])
            code, out, err = self.op(["discover", "openrouter", "--add", wire, "--family", family], "y\n")
            self.assertEqual(code, 0, err)
            line = next(line for line in self.saved().values() if line["wire_model"] == wire)
            self.assertEqual(line["family"], family)
            self.assertNotIn("family suggested", line.get("notes", ""))
            self.assertNotIn("suggested from", out)
        self.assertEqual(self.sent, [(PUBLIC, {})] * 3)

    def test_account_only_prefixed_model_suggests_but_prefixless_stays_unknown(self):
        state.atomic_write(self.secret_file, b"OPENROUTER_CLAUDE_API_KEY=fixture-key\n")
        self.listing(PUBLIC, [])
        self.listing(PUBLIC + "/user", [{"id": WIRE, "context_length": 200000},
                                       {"id": "account-only", "context_length": 200000}])
        code, out, err = self.op(["discover", "openrouter", "--add", WIRE, "account-only"], "y\ny\n")
        self.assertEqual(code, 0, err)
        lines = {line["wire_model"]: line for line in self.saved().values()}
        self.assertEqual(lines[WIRE]["family"], "deepseek")
        self.assertEqual(lines["account-only"].get("family", "unknown"), "unknown")
        self.assertEqual(out.count("suggested from"), 1)
        self.assertIn(PUBLIC + "/user", lines[WIRE]["context"]["source_ref"])
        self.assertEqual(self.sent, [(PUBLIC, {}), (PUBLIC + "/user", {"Authorization": "Bearer fixture-key"})])

    def test_cli_rechecks_the_current_catalog_family_set(self):
        self.listing(PUBLIC, [{"id": WIRE, "context_length": 200000}])
        known = operator.t1_families(self.runtime.catalog.docs) - {"deepseek"}
        with mock.patch.object(operator, "t1_families", return_value=known):
            code, out, err = self.op(["discover", "openrouter", "--add", WIRE], "y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(next(iter(self.saved().values())).get("family", "unknown"), "unknown")
        self.assertNotIn("suggested from", out)
        self.assertNotIn("notes", next(iter(self.saved().values())))
        self.assertEqual(self.sent, [(PUBLIC, {})])

    def test_existing_declaration_is_never_relabelled(self):
        code, out, err = self.op(["models", "add", "openrouter", WIRE, "--context", "200000", "--source", "operator"])
        self.assertEqual(code, 0, err)
        self.assertEqual(next(iter(self.saved().values())).get("family", "unknown"), "unknown")
        before = self.state_bytes()
        self.listing(PUBLIC, [{"id": WIRE, "context_length": 200000}])
        code, out, err = self.op(["discover", "openrouter", "--add", WIRE], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("declaration never edits an existing line", err)
        self.assertNotIn("suggested from", out)
        self.assertEqual(self.state_bytes(), before)

    def draft(self, wire=WIRE):
        return onboarding.line_draft(self.runtime, "openrouter", {
            "id": wire, "context_length": 200000, "openrouter": True})

    def form_action(self, keys):
        win = FakeWindow(keys, height=24, width=80)
        action = common.OnboardingActions(self.runtime, win, tui.MONO_PALETTE)
        action.preview = mock.Mock(return_value=True)
        action.show = mock.Mock()
        return action, win

    def test_tui_prefills_family_shows_hint_and_saves_without_network(self):
        draft = self.draft()
        before = copy.deepcopy(draft)
        action, win = self.form_action([ENTER] * 12)
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            action.model_form("openrouter", draft)
        line = next(iter(self.saved().values()))
        self.assertEqual(line.get("family"), "deepseek")
        self.assertEqual(line["notes"], NOTE)
        self.assertIn(HINT, "\n".join(win.frames))
        self.assertIn('"family": "deepseek"', action.preview.call_args.args[0])
        self.assertEqual(draft, before)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.calls, [])

    def test_tui_edited_family_is_saved_without_a_suggestion_note(self):
        for index, family in enumerate(("acme", "unknown")):
            draft = self.draft(f"deepseek/fixture-edit-{index}")
            draft["notes"] = "operator provenance"
            keys = [ENTER] * 6 + [HOME] + [DELETE] * len("deepseek") + list(family) + [ENTER] * 6
            action, win = self.form_action(keys)
            with mock.patch.object(consent, "stdio_ttys", return_value=True):
                action.model_form("openrouter", draft)
            line = next(line for line in self.saved().values() if line["wire_model"] == draft["wire_model"])
            self.assertEqual(line["family"], family)
            self.assertEqual(line["notes"], "operator provenance")
            self.assertIn(HINT, "\n".join(win.frames))
        self.assertEqual(self.sent, [])

    def test_tui_note_stays_complete_within_the_existing_notes_limit(self):
        draft = self.draft()
        draft["notes"] = "a" * 512
        action, win = self.form_action([ENTER] * 12)
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            action.model_form("openrouter", draft)
        line = next(iter(self.saved().values()))
        self.assertEqual(len(line["notes"]), 512)
        self.assertTrue(line["notes"].endswith("; " + NOTE))
        self.assertTrue(line["notes"].startswith("aaa"))
        self.assertEqual(self.sent, [])

    def test_tui_rechecks_the_current_catalog_family_set(self):
        draft = self.draft()
        known = operator.t1_families(self.runtime.catalog.docs) - {"deepseek"}
        action, win = self.form_action([ENTER] * 6 + [ESC])
        before = self.state_bytes()
        with mock.patch.object(operator, "t1_families", return_value=known):
            action.model_form("openrouter", draft)
        self.assertNotIn(HINT, "\n".join(win.frames))
        self.assertIn("  unknown_", win.frames[-1])
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.sent, [])

    def test_tui_explicit_label_and_edit_mode_do_not_infer(self):
        for edit, family in ((False, "acme"), (False, "unknown"), (True, None), (True, "deepseek")):
            draft = self.draft()
            if family is not None:
                draft["family"] = family
            action, win = self.form_action([ENTER] * (5 if edit else 6) + [ESC])
            before = self.state_bytes()
            action.model_form("openrouter", draft, "custom-existing", edit=edit)
            self.assertNotIn(HINT, "\n".join(win.frames))
            self.assertEqual(self.state_bytes(), before)
            action.preview.assert_not_called()

    def test_tui_suggested_form_golden(self):
        assertGolden(self, GOLDENS_ROOT / "tui/openrouter-family-80x24.txt",
                     _tui_render.build("openrouter-family").encode())
