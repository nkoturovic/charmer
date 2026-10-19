"""Saved lead choices use disposable v4 records and the existing pending owner."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
from unittest import mock

from charmer import catalog, cli, lineup, profile, sessions, state, strict_json, tui
from charmer.cli import doctor as cli_doctor
from charmer.cli import launch_flow, resume_checks, selection, session_facts
from charmer.cli.screens import findings
import _tui_fixture as fx
import _v4
import test_sessions_bulk as bulk
from test_screens_sessions import _SessionsCase
from test_tui import DOWN, END, RIGHT, FakeWindow

ESC, ENTER, SPACE = "\x1b", "\n", " "


class SavedChoiceTests(_SessionsCase):
    def test_existing_decide_path_for_a_missing_applied_lead(self):
        record = self.record(needs_choice=True, ended=True)
        request = lineup.Request("profile", name=catalog.DEFAULT_SEED, text=f"profile {catalog.DEFAULT_SEED}")
        decision = lineup.locked_decision(self.runtime, sessions.managed_id(record), lambda current:
                                         lineup.decide_locked(self.runtime, current, request, forced=False, exec_ok=False))
        self.assertEqual(decision.kind, "pending")
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["applied"], record["applied"])
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["lineup_generation"], record["lineup_generation"])
        self.assertTrue(decision.reasons)

    def run_cli(self, argv, text="", *, interactive=False):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main(argv, runtime=self.runtime, input_stream=io.StringIO(text),
                            output_stream=output, interactive=interactive)
        return code, output.getvalue(), errors.getvalue()

    def check(self, record):
        return sessions.nonrunning_problem(record, background=self.runtime.background_liveness(),
                                           process=sessions.proc_session_scan(),
                                           self_id=self.runtime.environ.get("CHARMER_MANAGED_ID"))

    def previews(self, mids, name=catalog.DEFAULT_SEED):
        items = sessions.batch_plan(self.runtime.session_store, mids, names=session_facts._record_resume_names,
                                    check=self.check)
        return lineup.choice_previews(self.runtime, items, lineup.Request("profile", name=name, text=f"profile {name}"))

    def profile_keys(self, name=catalog.DEFAULT_SEED):
        _labels, choices = selection._profile_choice_items(self.runtime, direct=False)
        return [DOWN] * choices.index(("profile", name))

    def population(self):
        base = self.record(managed_id=bulk.fixture_id(1), ended=True, scope=False, generation=0, needs_choice=True)
        records = [base]
        for index in range(2, 63):
            if index == 2:
                record = self.record(managed_id=bulk.fixture_id(index), ended=True, needs_choice=True)
            else:
                record = copy.deepcopy(base)
                record["managed_id"] = record["runtime_session_id"] = bulk.fixture_id(index)
                record["last_event_source"] = "end" if index % 2 == 0 else None
                if index > 28:
                    applied = copy.deepcopy(record["applied"])
                    applied["lead"]["key"] = self.default_lead()
                    record = sessions.with_applied(record, applied)
                self.runtime.session_store.save(record)
            records.append(record)
        return records

    def test_62_sessions_28_missing_cli_tui_selection_and_pending_bytes_match(self):
        records = self.population()
        wanted = {record["managed_id"] for record in records[:28]}
        _code, out, _err = self.run_cli(["sessions", "list", "--needs-choice", "--json"])
        self.assertEqual({row["managed_id"] for row in json.loads(out)["sessions"]}, wanted)
        scopes = bulk.snapshot(self.runtime.session_store.root / "scopes")
        with mock.patch.object(self.runtime, "perform", side_effect=AssertionError("choose performed")), \
                mock.patch.object(self.runtime, "prepare", side_effect=AssertionError("choose prepared a launch")), \
                mock.patch("charmer.lineup._now", return_value=fx.iso(fx.FIXED_NOW)):
            code, out, err = self.run_cli(["sessions", "choose", "--needs-choice", "--profile", catalog.DEFAULT_SEED, "--yes"])
            self.assertEqual((code, err), (0, ""), out + err)
            cli_pending = {mid: strict_json.canonical_file_bytes(self.runtime.session_store.load(mid)["pending"]) for mid in wanted}
            for original in records:
                current = self.runtime.session_store.load(original["managed_id"])
                for key in ("applied", "applied_hash", "lineup_generation", "launch_epoch", "profile", "follow"):
                    self.assertEqual(current[key], original[key], (original["managed_id"], key))
                self.runtime.session_store.save(original)
            self.assertEqual(bulk.snapshot(self.runtime.session_store.root / "scopes"), scopes)
            screen = self.screen()
            screen.run(FakeWindow(["/", "/", "/", ESC]))
            self.assertEqual({record["managed_id"] for record in screen.records}, wanted)
            screen.run(FakeWindow(["*", "Q", ENTER, *self.profile_keys(), ENTER, RIGHT, ENTER, ESC, ESC]))
            self.assertIn("28 succeeded · 0 refused · 0 failed", screen.message)
            tui_pending = {mid: strict_json.canonical_file_bytes(self.runtime.session_store.load(mid)["pending"]) for mid in wanted}
        self.assertEqual(tui_pending, cli_pending)
        self.assertEqual(bulk.snapshot(self.runtime.session_store.root / "scopes"), scopes)
        self.assertEqual(self.runtime.launches, [])
        self.assertEqual(self.screen()._needs(), [])
        attention, info = cli_doctor._doctor_record_report(self.runtime, [self.runtime.session_store.load(record['managed_id']) for record in records])
        self.assertFalse(any(getattr(line, "action", None) == "needs-choice" for line in attention))
        self.assertIn(f"28 session(s) switch to profile {catalog.DEFAULT_SEED} at their next resume", info)
        for mid in wanted:
            self.assertFalse(self.runtime.resume_choice(self.runtime.session_store.load(mid)).needs_choice)

    def test_direct_choice_records_intent_without_changing_applied_or_scope(self):
        record = self.record(needs_choice=True, ended=True)
        scopes = bulk.snapshot(self.runtime.session_store.root / "scopes")
        model = self.default_lead()
        code, out, err = self.run_cli(["sessions", "choose", fx.FIXED_ID[:8], "--direct", model, "--yes"])
        self.assertEqual((code, err), (0, ""), out + err)
        current = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertEqual(current["pending"]["kind"], "lineup")
        self.assertEqual(current["pending"]["document"]["lead"]["model"], model)
        for key in ("applied", "applied_hash", "lineup_generation", "launch_epoch"):
            self.assertEqual(current[key], record[key])
        self.assertEqual(bulk.snapshot(self.runtime.session_store.root / "scopes"), scopes)
        self.assertEqual(self.runtime.launches, [])

    def test_choose_reports_stdout_and_questions_stderr_not_the_terminal(self):
        self.record(needs_choice=True, ended=True)
        class Terminal(io.StringIO):
            def close(self):
                pass  # retain the in-memory fixture output after the entry closes its terminal
        stdout, stderr, terminal = io.StringIO(), io.StringIO(), Terminal()
        with mock.patch("charmer.cli.streams._open_tty_streams", return_value=(Terminal("n\n"), terminal)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["sessions", "choose", fx.FIXED_ID, "--profile", catalog.DEFAULT_SEED], runtime=self.runtime)
        self.assertEqual(code, 3)
        self.assertIn("Save lead choices", stdout.getvalue())
        self.assertNotIn("[y/N]", stdout.getvalue())
        self.assertIn("[y/N]", stderr.getvalue())
        self.assertEqual(terminal.getvalue(), "")
        self.assertNotIn("pending", self.runtime.session_store.load(fx.FIXED_ID))

    def test_existing_pending_is_disclosed_and_replaced_only_after_confirmation(self):
        record = self.record(needs_choice=True, ended=True, pending=True)
        before = bulk.snapshot(self.tmp)
        for flags, text, expected in ((["--dry-run"], "", 0), ([], "n\n", 3)):
            code, out, err = self.run_cli(["sessions", "choose", fx.FIXED_ID, "--profile", catalog.DEFAULT_SEED, *flags],
                                          text, interactive=True)
            self.assertEqual(code, expected, out + err)
            self.assertIn(f"replaces existing pending target: profile {record['pending']['profile']}", out)
            self.assertIn("replaced by this change", out)
            self.assertEqual(bulk.snapshot(self.tmp), before)
        code, out, err = self.run_cli(["sessions", "choose", fx.FIXED_ID, "--direct", self.default_lead(), "--yes"])
        self.assertEqual((code, err), (0, ""), out + err)
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["pending"]["kind"], "lineup")

    def test_invalid_or_removed_pending_target_does_not_hide_choice_on_any_surface(self):
        record = self.record(needs_choice=True, ended=True)
        name = "saved-choice-fixture"
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document["name"] = name
        document.pop("seed", None)
        self.runtime.profiles.new(document)
        code, _out, err = self.run_cli(["sessions", "choose", fx.FIXED_ID, "--profile", name, "--yes"])
        self.assertEqual((code, err), (0, ""))
        current = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertTrue(self.runtime.pending_resolves_lead(current))
        self.assertEqual(resume_checks._evaluate_resume_gate(self.runtime, current).kind, "ok")
        self.runtime.profiles.delete(name)
        self.assertFalse(self.runtime.pending_resolves_lead(current))
        self.assertEqual(resume_checks._evaluate_resume_gate(self.runtime, current).kind, "needs-choice")
        screen = self.screen()
        self.assertEqual(screen._needs(), [0])
        _code, out, _err = self.run_cli(["sessions", "list", "--needs-choice", "--json"])
        self.assertEqual([row["managed_id"] for row in json.loads(out)["sessions"]], [fx.FIXED_ID])
        self.assertTrue(json.loads(out)["sessions"][0]["needs_attention"])
        target = cli.LaunchTarget("record", None, record.get("profile"), record.get("follow", False), "Resume")
        with self.assertRaises(cli.NeedsChoiceError):
            self.runtime.prepare(target, action="resume", session_id=fx.FIXED_ID, passthrough=[])
        pending = copy.deepcopy(current["pending"])
        pending.update(kind="lineup", profile=None, follow=False, document=profile.ad_hoc_direct("missing-fixture-model"))
        current["pending"] = pending
        self.runtime.session_store.save(current)
        self.assertTrue(self.runtime.resume_choice(current).needs_choice)
        self.assertIsNotNone(self.runtime.resume_choice(current).problem)
        attention, info = cli_doctor._doctor_record_report(self.runtime, [current])
        self.assertTrue(any(getattr(line, "action", None) == "needs-choice" for line in attention))
        self.assertTrue(any("pending relaunch change is invalid" in line for line in info))

    def test_pending_rows_show_next_resume_marker_but_keep_the_applied_lead(self):
        record = self.record(needs_choice=True, ended=True)
        self.run_cli(["sessions", "choose", fx.FIXED_ID, "--profile", catalog.DEFAULT_SEED, "--yes"])
        screen = self.screen()
        model = screen._model(120)
        self.assertIsNone(model.banner)
        self.assertFalse(model.rows[0].needs)
        self.assertIn("↻", model.rows[0].cells[-1])
        self.assertIn(record["applied"]["lead"]["key"], " ".join(model.rows[0].cells))
        _code, out, _err = self.run_cli(["sessions", "list", "--json"])
        row = json.loads(out)["sessions"][0]
        self.assertEqual(row["lead"], record["applied"]["lead"]["key"])
        self.assertFalse(row["needs_choice"])
        self.assertFalse(row["needs_attention"])
        self.assertTrue(row["pending_change"])
        output = io.StringIO()
        launch_flow._choose_session_line(self.runtime, io.StringIO("\n"), output)
        self.assertIn("↻", output.getvalue())
        self.assertNotIn("! ", output.getvalue())

    def test_invalid_requested_targets_and_healthy_sessions_are_refused_without_writes(self):
        self.record(needs_choice=True, ended=True)
        self.record(managed_id=fx.OTHER_ID, ended=True)
        before = bulk.snapshot(self.tmp)
        for target in (["--profile", "missing-profile"], ["--direct", "missing-model"],
                       ["--direct", self.default_lead(), "--effort", "invalid-fixture-effort"]):
            code, out, _err = self.run_cli(["sessions", "choose", fx.FIXED_ID, *target, "--yes"])
            self.assertEqual(code, 1, out)
            self.assertIn("refused", out)
        code, out, _err = self.run_cli(["sessions", "choose", fx.OTHER_ID, "--profile", catalog.DEFAULT_SEED, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("charmer lineup --session", out)
        self.assertEqual(bulk.snapshot(self.tmp), before)

    def test_choose_live_unknown_legacy_and_partial_failure_outcomes_are_explicit(self):
        for mid in (fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID):
            self.record(managed_id=mid, needs_choice=True, ended=True, scope=False, generation=0)
        self.v3_record(managed_id=fx.FOURTH_ID, lead={"model": fx.needs_choice_key(self.runtime)})
        self.runtime.live_prefixes = frozenset({fx.THIRD_ID[:8]})
        real_save = self.runtime.session_store.save

        def save(record):
            if record["managed_id"] == fx.OTHER_ID:
                raise OSError("fixture failed write")
            return real_save(record)

        with mock.patch.object(self.runtime.session_store, "save", side_effect=save):
            code, out, _err = self.run_cli(["sessions", "choose", "--needs-choice", "--profile", catalog.DEFAULT_SEED, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("1 succeeded · 2 refused · 1 failed", out)
        self.assertIn("legacy record", out)
        self.assertIn("live", out)
        self.assertNotIn("pending", self.runtime.session_store.load(fx.OTHER_ID))
        with mock.patch.object(self.runtime, "background_liveness", return_value=sessions.BackgroundLiveness(False, frozenset(), "fixture unknown")):
            code, out, _err = self.run_cli(["sessions", "choose", fx.OTHER_ID, "--profile", catalog.DEFAULT_SEED, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("unknown", out)

    def test_changed_record_or_target_refuses_with_current_preview(self):
        self.record(needs_choice=True, ended=True)
        previews = self.previews([fx.FIXED_ID])
        self.runtime.session_store.set_title(fx.FIXED_ID, "edited after preview")
        results = lineup.save_choice_batch(self.runtime, previews, check=self.check)
        self.assertEqual(results[0].status, "refused")
        self.assertIn("Current preview:", results[0].message)
        self.assertNotIn("pending", self.runtime.session_store.load(fx.FIXED_ID))
        name = "target-change-fixture"
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.update(name=name)
        document.pop("seed", None)
        self.runtime.profiles.new(document)
        previews = self.previews([fx.FIXED_ID], name)
        document["description"] = "changed after preview"
        self.runtime.profiles.save(document)
        results = lineup.save_choice_batch(self.runtime, previews, check=self.check)
        self.assertEqual(results[0].status, "refused")
        self.assertIn(lineup.PREVIEW_STALE, results[0].message)
        self.assertNotIn("pending", self.runtime.session_store.load(fx.FIXED_ID))

    def test_choose_rechecks_became_live_under_existing_lifecycle_lock(self):
        self.record(needs_choice=True, ended=True)
        previews = self.previews([fx.FIXED_ID])
        self.runtime.proc_ids = frozenset({fx.FIXED_ID})
        observed = []

        def check(current):
            lock = self.runtime.session_store.lifecycle_lock(fx.FIXED_ID)
            observed.append(not lock.acquire(blocking=False))
            return self.check(current)

        before = self.runtime.session_store.load_raw(fx.FIXED_ID)[1]
        results = lineup.save_choice_batch(self.runtime, previews, check=check)
        self.assertEqual(results[0].status, "refused")
        self.assertEqual(observed, [True])
        self.assertIn("live", results[0].message)
        self.assertEqual(self.runtime.session_store.load_raw(fx.FIXED_ID)[1], before)

    def test_choose_duplicate_ambiguous_corrupt_legacy_and_unsafe_record_refusals(self):
        self.record(needs_choice=True, ended=True, scope=False, generation=0)
        twin = "11111111-2222-4222-8222-222222222222"
        self.record(managed_id=twin, needs_choice=True, ended=True, scope=False, generation=0)
        code, out, _err = self.run_cli(["sessions", "choose", "11111111", "--profile", catalog.DEFAULT_SEED, "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("matches 2 sessions", out)
        self.v3_record(managed_id=fx.OTHER_ID, lead={"model": fx.needs_choice_key(self.runtime)})
        corrupt = self.runtime.session_store.sessions_dir / f"{fx.THIRD_ID}.json"
        state.atomic_write(corrupt, b"not-json\n")
        unsafe = self.runtime.session_store.sessions_dir / f"{fx.FOURTH_ID}.json"
        unsafe.symlink_to(corrupt)
        code, out, _err = self.run_cli(["sessions", "choose", fx.FIXED_ID, fx.FIXED_ID, fx.OTHER_ID,
                                       fx.THIRD_ID, fx.FOURTH_ID, "../escape", "--profile", catalog.DEFAULT_SEED, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("1 succeeded · 5 refused · 0 failed", out)
        self.assertIn("duplicate", out)
        self.assertIn("legacy", out)
        self.assertIn("corrupt", out)
        self.assertIn("symlink", out)
        self.assertTrue(corrupt.exists())
        self.assertNotIn("pending", self.runtime.session_store.load(twin))

    def test_pending_only_guard_refuses_live_lineup_decisions_before_data_writes(self):
        record = self.record(ended=True)
        mid = sessions.managed_id(record)
        before = self.runtime.session_store.load_raw(mid)[1]
        scopes = bulk.snapshot(self.runtime.session_store.root / "scopes")
        request = lineup.Request("profile", name=catalog.DEFAULT_SEED, text=f"profile {catalog.DEFAULT_SEED}")
        with self.assertRaisesRegex(lineup.LineupRefusal, "cannot apply live"):
            lineup.locked_decision(self.runtime, mid, lambda current: lineup.decide_locked(
                self.runtime, current, request, forced=False, exec_ok=False, pending_only=True))
        self.assertEqual(self.runtime.session_store.load_raw(mid)[1], before)
        self.assertEqual(bulk.snapshot(self.runtime.session_store.root / "scopes"), scopes)

    def test_a_read_only_runtime_cannot_save_pending_even_through_the_batch_service(self):
        self.record(needs_choice=True, ended=True)
        previews = self.previews([fx.FIXED_ID])
        before = bulk.snapshot(self.tmp)
        with mock.patch.object(self.runtime, "allow_state_writes", False):
            results = lineup.save_choice_batch(self.runtime, previews, check=self.check)
        self.assertEqual(results[0].status, "refused")
        self.assertIn("read-only", results[0].message)
        self.assertEqual(bulk.snapshot(self.tmp), before)

    def test_choose_grammar_requires_one_target_and_an_explicit_selection(self):
        self.record(needs_choice=True, ended=True)
        before = bulk.snapshot(self.tmp)
        for argv in (["--needs-choice"], ["--needs-choice", "--profile", catalog.DEFAULT_SEED, "--direct", self.default_lead()],
                     ["--profile", catalog.DEFAULT_SEED], [fx.FIXED_ID, "--all", "--direct", self.default_lead()],
                     ["--needs-choice", "--profile", catalog.DEFAULT_SEED, "--effort", "high"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code, _out, _err = self.run_cli(["sessions", "choose", *argv, "--yes"])
                except SystemExit as exc:
                    code = exc.code
                self.assertEqual(code, 2)
        self.assertEqual(bulk.snapshot(self.tmp), before)

    def test_banner_profile_choice_saves_every_target_without_preparing_or_launching(self):
        self.record(needs_choice=True, ended=True, scope=False, generation=0)
        self.record(managed_id=fx.OTHER_ID, needs_choice=True, ended=True, scope=False, generation=0)
        screen = self.screen()
        with mock.patch.object(self.runtime, "prepare", side_effect=AssertionError("banner prepared")), \
                mock.patch.object(self.runtime, "perform", side_effect=AssertionError("banner performed")):
            result = screen.run(FakeWindow([END, ENTER, ENTER, ENTER, RIGHT, ENTER, ESC, ESC]))
        self.assertIsNone(result)
        self.assertTrue(all("pending" in self.runtime.session_store.load(mid) for mid in (fx.FIXED_ID, fx.OTHER_ID)))
        self.assertEqual(screen._needs(), [])

    def test_doctor_pending_choices_leave_attention_and_name_the_next_resume_target(self):
        self.record(needs_choice=True, ended=True)
        code, out, err = self.run_cli(["sessions", "choose", fx.FIXED_ID, "--profile", catalog.DEFAULT_SEED, "--yes"])
        self.assertEqual((code, err), (0, ""), out + err)
        attention, info = cli_doctor._doctor_record_report(self.runtime, [self.runtime.session_store.load(fx.FIXED_ID)])
        self.assertFalse(any(getattr(line, "action", None) == "needs-choice" for line in attention))
        self.assertIn(f"1 session(s) switch to profile {catalog.DEFAULT_SEED} at their next resume", info)

    def test_h_needs_choice_row_opens_the_filtered_save_dialog_without_launching(self):
        self.record(needs_choice=True, ended=True, scope=False, generation=0)
        self.record(managed_id=fx.OTHER_ID, ended=True, scope=False, generation=0)

        def report():
            attention, info = cli_doctor._doctor_record_report(self.runtime, session_facts._session_records(self.runtime))
            return [], info, attention

        screen = findings.FindingsScreen(self.runtime, palette=tui.MONO_PALETTE, tty_in=io.StringIO(),
                                          tty_out=io.StringIO(), report=report)
        with mock.patch.object(self.runtime, "prepare", side_effect=AssertionError("H prepared")), \
                mock.patch.object(self.runtime, "perform", side_effect=AssertionError("H performed")):
            screen.run(FakeWindow([ENTER, ENTER, *self.profile_keys(), ENTER, RIGHT, ENTER, ESC, ESC]))
        self.assertIn("pending", self.runtime.session_store.load(fx.FIXED_ID))
        self.assertNotIn("pending", self.runtime.session_store.load(fx.OTHER_ID))
        self.assertFalse(any(getattr(item.row, "action", None) == "needs-choice" for item in screen.lines))

    def test_q_direct_choice_and_cancel_never_launch(self):
        self.record(needs_choice=True, ended=True)
        before = bulk.snapshot(self.tmp)
        screen = self.screen()
        screen.run(FakeWindow([SPACE, "Q", ENTER, ENTER, ENTER, ESC]))
        self.assertEqual(bulk.snapshot(self.tmp), before)
        with mock.patch.object(self.runtime, "perform", side_effect=AssertionError("direct choice performed")):
            screen.run(FakeWindow(["Q", DOWN, ENTER, ENTER, RIGHT, ENTER, RIGHT, ENTER, ESC, ESC]))
        current = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertEqual(current["pending"]["kind"], "lineup")
        self.assertTrue(self.runtime.pending_resolves_lead(current))
        self.assertEqual(self.runtime.launches, [])


class SavedChoiceEntryTests(_SessionsCase):
    actual_entry = bulk.BatchInitializationTests.actual_entry

    def test_actual_choose_dry_run_and_cancel_change_no_bytes_or_mutation_metadata(self):
        self.record(needs_choice=True, ended=True)
        before = bulk.snapshot(self.tmp)
        for flags, expected in ((["--dry-run"], 0), ([], 3)):
            code, out, err = self.actual_entry(["sessions", "choose", "--needs-choice", "--profile", catalog.DEFAULT_SEED, *flags])
            self.assertEqual(code, expected, out + err)
            self.assertEqual(bulk.snapshot(self.tmp), before)


    def test_actual_choose_rechecks_marker_and_channel_after_confirmation(self):
        self.record(needs_choice=True, ended=True)
        before = self.runtime.session_store.load_raw(fx.FIXED_ID)[1]
        self.runtime.environ["CHARMER_CHANNEL"] = "source"
        case = self
        for leaf, value, expected in (("state-version", b"999\n", "newer charmer"),
                                      ("channel", b"bundle\n", "bundle installation")):
            class Confirm(io.StringIO):
                def readline(self, *_args):
                    state.atomic_write(case.runtime.session_store.root / leaf, value)
                    return "y\n"
            code, out, err = self.actual_entry(["sessions", "choose", "--needs-choice", "--profile", catalog.DEFAULT_SEED], Confirm())
            self.assertEqual(code, 1, out + err)
            self.assertIn(expected, err)
            self.assertEqual(self.runtime.session_store.load_raw(fx.FIXED_ID)[1], before)
            state.atomic_write(self.runtime.session_store.root / "state-version", b"4\n")


class OrdinaryPendingResumeTests(_v4.V4Case):
    def test_an_ordinary_resume_consumes_a_saved_choice_without_a_chooser(self):
        original = self.launch_fresh()
        mid = sessions.managed_id(original)
        old_lead = original["applied"]["lead"]["key"]
        applied = copy.deepcopy(original["applied"])
        applied["lead"]["key"] = fx.needs_choice_key(self.runtime)
        record = sessions.with_applied(original, applied)
        record["last_event_source"] = "end"
        self.store.save(record)
        lcat = self.runtime.lineup_catalog()
        name = next(name for name in sorted(self.runtime.profiles.names())
                    if lcat.resolve_key(self.runtime.profiles.load(name)["lead"]["model"]).key != old_lead)
        target_key = lcat.resolve_key(self.runtime.profiles.load(name)["lead"]["model"]).key
        output = io.StringIO()
        with mock.patch("charmer.sessions.proc_session_scan", return_value=sessions.ProcessScan(True, frozenset())), \
                mock.patch("charmer.cli.session_facts._live_background_prefixes", return_value=frozenset()):
            code = cli.main(["sessions", "choose", mid, "--profile", name, "--yes"], runtime=self.runtime,
                            output_stream=output, interactive=False)
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(len(self.execs), 1, "saving a choice must not exec")
            self.assertEqual(self.store.load(mid)["applied"], record["applied"])
            self.assertEqual(resume_checks._evaluate_resume_gate(self.runtime, self.store.load(mid)).kind, "ok")
            with mock.patch.object(launch_flow, "_choose_lead_line", side_effect=AssertionError("ordinary resume opened a chooser")):
                code = cli.main(["-r", mid, "--line"], runtime=self.runtime, output_stream=output, interactive=False)
        self.assertEqual(code, 0, output.getvalue())
        resumed = self.store.load(mid)
        self.assertNotIn("pending", resumed)
        self.assertEqual(resumed["applied"]["lead"]["key"], target_key)
        self.assertEqual(len(self.execs), 2)
