"""End-to-end profile grammar and shared session-status surfaces."""
from __future__ import annotations

import contextlib
import io
import os
import types
from unittest import mock

from charmer import assets, catalog, cli, lineup, tui
from charmer.cli import parser, runtime as runtime_module
from charmer.cli.screens import launch_sessions
from _catalog import FIXTURE_ROOT
import _tui_fixture as fx
from test_propagation import PropagationCase
from test_screens_sessions import _SessionsCase
from test_tui import FakeWindow


class ProfileCommandSurfaceTests(PropagationCase):
    def invoke(self, *words, runtime=True):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(error):
            try:
                code = cli.main(["profile", "set", "mine", *words],
                                runtime=self.runtime if runtime else None,
                                input_stream=io.StringIO(), output_stream=output, interactive=False)
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), error.getvalue()

    def test_cli_split_save_and_later_save_reach_the_only_follower(self):
        record = self.follower()
        code, text, error = self.invoke("lead=grok46:high", "cm-implementer=opus55:xhigh", "--apply")
        self.assertEqual(code, 0, text + error)
        self.assertIn("agents applied live (lineup gen 2)", text)
        code, text, error = self.invoke("cm-implementer=sol:high", "--apply")
        self.assertEqual(code, 0, text + error)
        self.assertIn("agents applied live (lineup gen 3)", text)
        code, text, error = self.invoke("cm-implementer=opus55:xhigh", "--later")
        self.assertEqual(code, 0, text + error)
        self.assertIn("not applied", text)
        self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 3)

    def test_preview_entry_constructs_a_read_only_runtime_and_writes_nothing(self):
        before = self.tree(self.root / "config"), self.tree(self.store.root)
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            self.assertFalse(kwargs["allow_state_writes"])
            self.assertFalse(kwargs["refresh_shims"])
            self.assertFalse(kwargs["initialize_session_store"])
            return self.make_runtime(**kwargs)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(assets, "default_asset_root", return_value=FIXTURE_ROOT), \
                mock.patch.object(runtime_module, "Runtime", side_effect=factory):
            code, text, error = self.invoke("lead=fable:max", "--preview", "--apply", runtime=False)
        self.assertEqual(code, 0, text + error)
        self.assertEqual(len(calls), 1)
        self.assertIn("Preview only", text)
        self.assertEqual((self.tree(self.root / "config"), self.tree(self.store.root)), before)

    def test_parser_classifies_preview_and_both_propagation_choices(self):
        for tail, writes in (([], True), (["--apply"], True), (["--later"], True), (["--preview"], False)):
            with self.subTest(tail=tail):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    try:
                        args = parser.build_parser().parse_args(["profile", "set", "mine", "lead=fable:max", *tail])
                    except SystemExit:
                        self.fail(output.getvalue())
                self.assertEqual(parser._writes_versioned_state(args), writes)
                self.assertEqual(parser._read_only_listing(args), not writes)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.build_parser().parse_args(["profile", "set", "mine", "lead=fable:max", "--apply", "--later"])

    def test_profile_assignment_placeholder_covers_the_generated_grammar(self):
        from _docs_vocab import check_span

        result = check_span("charmer profile set <name> <assignment> [--preview] [--apply | --later]")
        self.assertEqual(result.problem, "")
        self.assertGreater(result.forms, 1)

    def test_profile_set_reports_to_stdout_even_with_a_terminal_stream(self):
        from charmer.cli import streams
        tty, output = io.StringIO(), io.StringIO()
        args = types.SimpleNamespace(command="profile", profile_command="set", preview=True)
        self.assertIs(streams._report_output_stream(args, tty, output), output)

    def test_invalid_second_assignment_never_changes_the_file_through_cli(self):
        before = self.runtime.profiles.raw_bytes("mine")
        code, text, error = self.invoke("lead=fable:max", "cm-implementer=unknown", "--apply")
        self.assertEqual(code, 1, text + error)
        self.assertIn("unknown", error)
        self.assertEqual(self.runtime.profiles.raw_bytes("mine"), before)


class SharedSessionStatusTests(_SessionsCase):
    def edited_follower(self):
        record = self.record()
        agents = self.runtime.profiles.load(self.live_seed())["agents"]
        self.runtime.profiles.update(catalog.DEFAULT_SEED, lambda doc: doc["agents"].update(agents))
        return record

    def test_v_shows_the_same_profile_drift_and_remedy_as_cm_show(self):
        record = self.edited_follower()
        self.assertIn("changed since this session applied it", lineup.show_text(self.runtime, record))
        expected = lineup.profile_status_lines(self.runtime, record)
        self.assertTrue(expected)
        captured = []
        with mock.patch("charmer.cli.session_facts.explanation", side_effect=cli.CLIError("fixture unavailable")), \
                mock.patch("charmer.cli.gateway_facts.report_events", return_value=types.SimpleNamespace(coverage="unavailable")), \
                mock.patch.object(tui.TextView, "run", lambda view, _win: captured.extend(view.lines)):
            self.run_screen(self.screen(), ["v", "\x1b"])
        for line in expected:
            self.assertIn(line, captured)

    def test_resume_card_explains_stored_state_not_only_the_prepared_lineup(self):
        record = self.edited_follower()
        self.assertIn("changed since this session applied it", lineup.show_text(self.runtime, record))
        expected = lineup.profile_status_lines(self.runtime, record)
        target = cli.LaunchTarget("record", None, record["profile"], record["follow"], "Session")
        prepared = self.runtime.prepare(target, action="resume", passthrough=[], session_id=fx.FIXED_ID)
        before = self.runtime.session_store.read_record_bytes(fx.FIXED_ID)
        card = launch_sessions._LaunchCardScreen(self.runtime, target, action="resume", session_id=fx.FIXED_ID,
                                                prepared=prepared, passthrough=[], palette=tui.MONO_PALETTE,
                                                auto_setup=False)
        text = "\n".join(row.text for row in card.card.rows)
        for line in expected:
            self.assertIn("Before this resume: " + line, text)
        self.assertEqual(self.runtime.session_store.read_record_bytes(fx.FIXED_ID), before)
        self.assertEqual(self.runtime.launches, [])
