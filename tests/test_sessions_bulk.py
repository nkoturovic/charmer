"""Bulk session operations on disposable records, processes and native files only."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from charmer import catalog, cli, sessions, state, tui
from charmer.cli import entry, runtime as runtime_mod
from charmer.cli.commands import sessions as commands
import _tui_fixture as fx
from _catalog import FIXTURE_ROOT
from test_screens_sessions import _SessionsCase
from test_tui import DOWN, END, RIGHT, FakeWindow

ESC, ENTER, SPACE = "\x1b", "\n", " "


def fixture_id(index: int) -> str:
    return f"{index:08x}-1111-4111-8111-{index:012x}"


def snapshot(root: Path) -> dict:
    """Bytes and mutation metadata, never a real home or transcript root."""
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() and not path.is_symlink() else None,
                                        path.lstat().st_mode, path.lstat().st_ino,
                                        path.lstat().st_mtime_ns, path.lstat().st_ctime_ns)
            for path in [root, *root.rglob("*")]}


class BulkHousekeepingTests(_SessionsCase):
    def run_cli(self, argv, text="", *, interactive=False, runtime=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main(argv, runtime=runtime or self.runtime, input_stream=io.StringIO(text),
                            output_stream=output, interactive=interactive)
        return code, output.getvalue(), errors.getvalue()

    def population(self):
        records = [self.record(managed_id=fixture_id(i), ended=4 <= i <= 7,
                               needs_choice=i in (8, 9), last_seen=timedelta(days=60 if 4 <= i <= 7 else 1),
                               scope=False) for i in range(1, 13)]
        forked = records[9]
        forked["pending_forks"] = [{"session_id": fixture_id(100), "observed_at": fx.iso(fx.FIXED_NOW)}]
        self.runtime.session_store.save(forked)
        elsewhere = records[11]
        elsewhere["cwd"] = str(self.tmp / "elsewhere")
        self.runtime.session_store.save(elsewhere)
        self.runtime.live_prefixes = frozenset(record["managed_id"][:8] for record in records[:3])
        return records

    def test_ended_age_filter_dry_run_then_forget_preserves_native_bytes(self):
        records = self.population()
        wanted = {record["managed_id"] for record in records[3:7]}
        native = self.transcript(records[3])
        native.write_bytes(b"synthetic conversation kept byte for byte\n")
        before = snapshot(self.tmp)
        code, out, err = self.run_cli(["sessions", "list", "--ended", "--older-than", "30d", "--json"])
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)["sessions"]
        self.assertEqual({row["managed_id"] for row in rows}, wanted)
        self.assertTrue(all(row["ended"] and not row["needs_choice"] for row in rows))
        code, out, err = self.run_cli(["sessions", "forget", "--ended", "--older-than", "30d", "--dry-run"])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(all(mid in out for mid in wanted))
        self.assertEqual(snapshot(self.tmp), before)
        code, out, err = self.run_cli(["sessions", "forget", "--ended", "--older-than", "30d", "--yes"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("4 succeeded · 0 refused · 0 failed", out)
        self.assertTrue(all(not self.runtime.session_store.exists(mid) for mid in wanted))
        self.assertEqual(native.read_bytes(), b"synthetic conversation kept byte for byte\n")

    def test_bulk_filter_unreadable_directory_refuses_without_writes(self):
        self.record(ended=True, scope=False)
        directory = self.runtime.session_store.sessions_dir
        real = Path.iterdir

        def entries(path):
            if path == directory:
                raise PermissionError("fixture directory unavailable")
            return real(path)

        before = snapshot(self.tmp)
        for verb in (["forget", "--all"], ["choose", "--all", "--profile", catalog.DEFAULT_SEED]):
            for flags in (["--dry-run"], ["--yes"]):
                with self.subTest(verb=verb, flags=flags), mock.patch.object(Path, "iterdir", entries):
                    code, out, err = self.run_cli(["sessions", *verb, *flags])
                    self.assertEqual(code, 1, out + err)
                    self.assertIn("session record directory is unavailable", out + err)
        self.assertEqual(snapshot(self.tmp), before)

    def test_bulk_selection_filters_first_scan_without_rescanning(self):
        one = self.record(ended=True, scope=False)
        two = self.record(managed_id=fx.OTHER_ID, ended=False, scope=False)
        args = cli.build_parser().parse_args(["sessions", "forget", "--ended", "--dry-run"])
        with mock.patch.object(commands.session_facts, "_session_record_scan", side_effect=[
                ([one, two], [], {fx.FIXED_ID, fx.OTHER_ID}), ([], [], set())]) as scan:
            identifiers = commands._batch_identifiers(self.runtime, args)
        self.assertEqual(identifiers, [fx.FIXED_ID])
        scan.assert_called_once_with(self.runtime)

    def test_single_forget_dry_run_uses_single_session_guards(self):
        self.record(ended=True, scope=False)
        self.v3_record(managed_id=fx.OTHER_ID)
        before = snapshot(self.tmp)
        for value in (fx.FIXED_ID, fx.FIXED_ID[:8], catalog.DEFAULT_SEED, fx.OTHER_ID):
            with self.subTest(value=value):
                code, out, err = self.run_cli(["sessions", "forget", value, "--dry-run"])
                self.assertEqual(code, 0, out + err)
                self.assertIn("Dry run: nothing written.", out)
        self.assertEqual(snapshot(self.tmp), before)
        state.atomic_write(self.runtime.session_store.sessions_dir / f"{fx.THIRD_ID}.json", b"not-json\n")
        before = snapshot(self.tmp)
        code, out, err = self.run_cli(["sessions", "forget", fx.THIRD_ID, "--dry-run"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Dry run: nothing written.", out)
        with mock.patch.object(self.runtime, "background_liveness", return_value=
                sessions.BackgroundLiveness(False, frozenset(), "fixture unknown")):
            code, out, err = self.run_cli(["sessions", "forget", fx.FIXED_ID, "--dry-run", "--force"])
            self.assertEqual(code, 0, out + err)
            code, out, err = self.run_cli(["sessions", "forget", fx.FIXED_ID, "--dry-run"])
            self.assertEqual(code, 1, out + err)
            self.assertIn("cannot tell whether", out + err)
        self.runtime.live_prefixes = frozenset({fx.FIXED_ID[:8]})
        code, out, err = self.run_cli(["sessions", "forget", fx.FIXED_ID, "--dry-run", "--force"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("live in the background", out + err)
        self.assertEqual(snapshot(self.tmp), before)

    def test_age_never_uses_created_or_falls_back_from_unknown_last_seen(self):
        selected = sessions.SessionFilter(older_than=30)
        record = {"cwd": self.runtime.cwd, "created_at": "2000-01-01T00:00:00Z"}
        for value in (None, "", "unknown", "2026-99-99T00:00:00Z", "2000-1-1T00:00:00Z", 12):
            with self.subTest(value=value):
                record["last_seen_at"] = value
                self.assertFalse(selected.matches(record, cwd=self.runtime.cwd, needs_choice=lambda _record: False,
                                                  now=fx.FIXED_NOW))
        record["last_seen_at"] = fx.iso(fx.FIXED_NOW - timedelta(days=1))
        self.assertFalse(selected.matches(record, cwd=self.runtime.cwd, needs_choice=lambda _record: False, now=fx.FIXED_NOW))
        record["last_seen_at"] = fx.iso(fx.FIXED_NOW - timedelta(days=31))
        self.assertTrue(selected.matches(record, cwd=self.runtime.cwd, needs_choice=lambda _record: False, now=fx.FIXED_NOW))

    def test_live_unknown_self_and_pending_fork_are_refused_separately(self):
        records = self.population()
        code, out, _ = self.run_cli(["sessions", "forget", records[0]["managed_id"], records[3]["managed_id"],
                                    records[9]["managed_id"], "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("1 succeeded · 2 refused · 0 failed", out)
        self.assertIn("live", out)
        self.assertIn("pending fork", out)
        before = snapshot(self.tmp)
        for unknown in ("background", "process", "self"):
            with self.subTest(unknown=unknown), contextlib.ExitStack() as stack:
                if unknown == "background":
                    stack.enter_context(mock.patch.object(self.runtime, "background_liveness",
                                                          return_value=sessions.BackgroundLiveness(False, frozenset(), reason="fixture unknown")))
                elif unknown == "process":
                    stack.enter_context(mock.patch("charmer.sessions.proc_session_scan",
                                                   return_value=sessions.ProcessScan(False, frozenset(), reason="fixture unreadable")))
                else:
                    stack.enter_context(mock.patch.dict(self.runtime.environ, {"CHARMER_MANAGED_ID": records[4]["managed_id"]}))
                code, out, _ = self.run_cli(["sessions", "forget", records[4]["managed_id"], records[5]["managed_id"], "--dry-run"])
                self.assertEqual(code, 1)
                self.assertIn("refused:", out)
        self.assertEqual(snapshot(self.tmp), before)

    def test_duplicate_ambiguous_corrupt_legacy_and_unsafe_ids(self):
        one = self.record(managed_id=fx.FIXED_ID, ended=True, scope=False)
        twin = "11111111-2222-4222-8222-222222222222"
        self.record(managed_id=twin, ended=True, scope=False)
        code, out, err = self.run_cli(["sessions", "forget", "11111111", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("matches 2 managed sessions", err)
        self.v3_record(managed_id=fx.OTHER_ID)
        corrupt = self.runtime.session_store.sessions_dir / f"{fx.THIRD_ID}.json"
        state.atomic_write(corrupt, b"not-json\n")
        unsafe = self.runtime.session_store.sessions_dir / f"{fx.FOURTH_ID}.json"
        unsafe.symlink_to(corrupt)
        code, out, _ = self.run_cli(["sessions", "forget", "11111111", fx.FIXED_ID, fx.FIXED_ID,
                                    fx.OTHER_ID, fx.THIRD_ID, fx.FOURTH_ID, "../escape", "--yes"])
        self.assertEqual(code, 1)
        for text in ("nothing was chosen", "duplicate session", "legacy record", "corrupt session", "refused"):
            self.assertIn(text, out)
        self.assertIn("1 succeeded · 6 refused · 0 failed", out)
        self.assertFalse(self.runtime.session_store.exists(one["managed_id"]))
        self.assertTrue(self.runtime.session_store.exists(twin))
        self.assertTrue(corrupt.exists())
        self.assertTrue(unsafe.is_symlink())

    def test_became_live_is_rechecked_under_each_lifecycle_lock(self):
        for mid in (fx.FIXED_ID, fx.OTHER_ID):
            self.record(managed_id=mid, ended=True, scope=False)
        scans = 0

        def process_scan(*_args):
            nonlocal scans
            scans += 1
            if scans == 2:
                lock = self.runtime.session_store.lifecycle_lock(fx.FIXED_ID)
                self.assertFalse(lock.acquire(blocking=False), "process recheck must hold the lifecycle lock")
                return sessions.ProcessScan(True, frozenset({fx.FIXED_ID}))
            return sessions.ProcessScan(True, frozenset())

        with mock.patch("charmer.sessions.proc_session_scan", side_effect=process_scan):
            code, out, _ = self.run_cli(["sessions", "forget", fx.FIXED_ID, fx.OTHER_ID, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("1 succeeded · 1 refused · 0 failed", out)
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertFalse(self.runtime.session_store.exists(fx.OTHER_ID))

    def test_mark_ended_batch_rechecks_even_an_already_ended_noop(self):
        record = self.record(ended=True, scope=False)
        check = lambda current: sessions.nonrunning_problem(current, background=self.runtime.background_liveness(),
                                                             process=sessions.proc_session_scan())
        items = sessions.batch_plan(self.runtime.session_store, [fx.FIXED_ID],
                                    names=lambda _record: (), check=check)
        self.runtime.proc_ids = frozenset({fx.FIXED_ID})
        before = self.runtime.session_store.load_raw(fx.FIXED_ID)[1]
        results = sessions.housekeeping_batch(self.runtime.session_store, items, check=check, mark_ended=True)
        self.assertEqual(results[0].status, "refused")
        self.assertIn("live", results[0].message)
        self.assertEqual(self.runtime.session_store.load_raw(fx.FIXED_ID)[1], before)
        self.assertEqual(record["last_event_source"], "end")

    def test_partial_failure_does_not_claim_atomicity_or_lose_other_records(self):
        for mid in (fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID):
            self.record(managed_id=mid, ended=True, scope=False)
        self.runtime.live_prefixes = frozenset({fx.THIRD_ID[:8]})
        real_forget = self.runtime.session_store.forget_session

        def forget(mid, **kwargs):
            if mid == fx.OTHER_ID:
                raise OSError("fixture write failure")
            return real_forget(mid, **kwargs)

        with mock.patch.object(self.runtime.session_store, "forget_session", side_effect=forget):
            code, out, _ = self.run_cli(["sessions", "forget", fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("1 succeeded · 1 refused · 1 failed", out)
        self.assertFalse(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertTrue(self.runtime.session_store.exists(fx.OTHER_ID))
        self.assertTrue(self.runtime.session_store.exists(fx.THIRD_ID))

    def test_ids_are_frozen_and_changed_record_refuses_after_confirmation(self):
        self.record(managed_id=fx.FIXED_ID, ended=True, scope=False)
        self.record(managed_id=fx.OTHER_ID, ended=True, scope=False)
        case = self

        class Confirm(io.StringIO):
            def readline(self, *_args):
                case.runtime.session_store.set_title(fx.FIXED_ID, "changed after preview")
                case.record(managed_id=fx.THIRD_ID, ended=True, scope=False)
                return "y\n"

        output = io.StringIO()
        args = cli.build_parser().parse_args(["sessions", "forget", "--all"])
        code = commands._sessions_command(self.runtime, args, input_stream=Confirm(), output_stream=output,
                                          interactive=True, no_color=True)
        self.assertEqual(code, 1)
        self.assertIn("record changed after preview", output.getvalue())
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertTrue(self.runtime.session_store.exists(fx.THIRD_ID))
        self.assertFalse(self.runtime.session_store.exists(fx.OTHER_ID))

    def test_filter_grammar_and_bulk_force_refuse_before_mutation(self):
        self.record(ended=True, scope=False)
        before = snapshot(self.tmp)
        for args in ([fx.FIXED_ID, "--all"], [], [fx.FIXED_ID, fx.OTHER_ID, "--force"], ["--all", "--force"]):
            code, _out, _err = self.run_cli(["sessions", "forget", *args, "--yes"])
            self.assertEqual(code, 2, args)
        self.assertEqual(snapshot(self.tmp), before)

    def test_cli_and_tui_filters_select_identical_ids(self):
        self.population()
        _code, out, _err = self.run_cli(["sessions", "list", "--ended", "--older-than", "30d", "--json"])
        wanted = {row["managed_id"] for row in json.loads(out)["sessions"]}
        screen = self.screen()
        screen.run(FakeWindow(["/", "/", ESC]))
        self.assertEqual(screen.filter_mode, "ended")
        self.assertEqual({record["managed_id"] for record in screen.records}, wanted)

    def test_b_combines_all_filters_and_arbitrary_days_with_cli_selection_parity(self):
        records = self.population()
        for record in records[3:5]:
            applied = copy.deepcopy(record["applied"])
            applied["lead"]["key"] = fx.needs_choice_key(self.runtime)
            record = sessions.with_applied(record, applied)
            if record["managed_id"] == records[4]["managed_id"]:
                record["last_seen_at"] = "2000-99-99T00:00:00Z"
                record["created_at"] = "2000-01-01T00:00:00Z"
            self.runtime.session_store.save(record)
        code, out, err = self.run_cli(["sessions", "list", "--here", "--ended", "--needs-choice", "--older-than", "45d", "--json"])
        self.assertEqual((code, err), (0, ""))
        wanted = {row["managed_id"] for row in json.loads(out)["sessions"]}
        self.assertEqual(wanted, {records[3]["managed_id"]})
        before = snapshot(self.tmp)
        screen = self.screen()
        screen.run(FakeWindow([SPACE, "B", DOWN, ENTER, DOWN, ENTER, DOWN, ENTER,
                               "4", "5", "d", ENTER, RIGHT, ENTER, DOWN, ENTER, ESC]))
        self.assertEqual({record["managed_id"] for record in screen.records}, wanted)
        self.assertEqual(screen._filter(), sessions.SessionFilter(here=True, ended=True, needs_choice=True, older_than=45))
        self.assertEqual(screen.marked, set())
        self.assertEqual(snapshot(self.tmp), before)
        # C composes with the other filters rather than discarding them.
        screen.run(FakeWindow(["C", ESC]))
        self.assertFalse(screen._filter().here)
        self.assertTrue(screen._filter().ended and screen._filter().needs_choice)
        self.assertEqual(screen._filter().older_than, 45)

    def test_b_cancel_or_unchanged_apply_preserves_marks_and_all_state(self):
        self.record(ended=True, scope=False)
        for keys in ([SPACE, "B", DOWN, ENTER, ESC, ESC], [SPACE, "B", END, ENTER, ESC]):
            with self.subTest(keys=keys):
                screen = self.screen()
                original = screen._filter()
                before = snapshot(self.tmp)
                screen.run(FakeWindow(keys))
                self.assertEqual(screen._filter(), original)
                self.assertEqual(screen.marked, {fx.FIXED_ID})
                self.assertEqual(snapshot(self.tmp), before)

    def test_b_rejects_invalid_days_and_obeys_small_terminal_floor(self):
        self.record(ended=True, scope=False)
        before = snapshot(self.tmp)
        for text in ("bad", "-1d", "1h", "１２d", "1000000d"):
            with self.subTest(text=text):
                screen = self.screen()
                win = FakeWindow([SPACE, "B", DOWN, DOWN, DOWN, ENTER, *text,
                                  ENTER, RIGHT, ENTER, ESC, ESC], width=44, height=24)
                screen.run(win)
                self.assertIn("use a number of days", "\n".join(win.frames))
                self.assertIsNone(screen._filter().older_than)
                self.assertEqual(screen.marked, {fx.FIXED_ID})
                self.assertEqual(snapshot(self.tmp), before)
        screen = self.screen()
        rows, cols = screen.min_size(44)
        with mock.patch.object(screen, "_filter_dialog", side_effect=AssertionError("acted below floor")):
            screen.run(FakeWindow(["B", ESC], width=cols - 1, height=rows))
        self.assertEqual(snapshot(self.tmp), before)

    def test_space_marks_three_then_x_forgets_with_one_confirmation(self):
        for mid in (fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID):
            self.record(managed_id=mid, ended=True, scope=False)
        screen = self.screen()
        win = FakeWindow([SPACE, DOWN, SPACE, DOWN, SPACE, "X", RIGHT, ENTER, ESC, ESC])
        screen.run(win)
        self.assertEqual(len(self.runtime.launches), 0)
        self.assertFalse(any(self.runtime.session_store.exists(mid) for mid in (fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID)))
        self.assertIn("3 succeeded", screen.message)
        self.assertTrue(any("3 marked" in frame for frame in win.frames))

    def test_exited_filter_mark_all_then_m_marks_every_dead_session_ended(self):
        for mid in (fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID):
            self.record(managed_id=mid, scope=False)
        self.runtime.live_prefixes = frozenset({fx.THIRD_ID[:8]})
        screen = self.screen()
        screen.run(FakeWindow(["/", "*", "M", RIGHT, ENTER, ESC, ESC]))
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["last_event_source"], "end")
        self.assertEqual(self.runtime.session_store.load(fx.OTHER_ID)["last_event_source"], "end")
        self.assertNotEqual(self.runtime.session_store.load(fx.THIRD_ID)["last_event_source"], "end")

    def test_mark_all_toggles_and_cancel_writes_nothing(self):
        self.record(managed_id=fx.FIXED_ID, ended=True, scope=False)
        screen = self.screen()
        before = snapshot(self.tmp)
        screen.run(FakeWindow(["*", "*", SPACE, "X", ENTER, ESC]))
        self.assertEqual(screen.marked, {fx.FIXED_ID})
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertEqual(snapshot(self.tmp), before)


class BatchInitializationTests(_SessionsCase):
    """Real entry construction (no injected Runtime), still entirely fixture-owned."""

    def actual_entry(self, argv, stream=None):
        runtime_type = runtime_mod.Runtime

        def factory(**kwargs):
            kwargs["asset_root"] = FIXTURE_ROOT
            return runtime_type(**kwargs, environ=self.runtime.environ, cwd=self.runtime.cwd,
                                managed_root=self.tmp / "managed",
                                background_liveness=lambda: sessions.BackgroundLiveness(True, frozenset()))

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                mock.patch.object(runtime_mod, "Runtime", side_effect=factory), contextlib.redirect_stderr(errors):
            code = entry.main(argv, input_stream=stream or io.StringIO("n\n"), output_stream=output, interactive=True)
        return code, output.getvalue(), errors.getvalue()

    def test_actual_entry_dry_run_and_cancel_are_byte_and_metadata_stable(self):
        self.record(managed_id=fx.FIXED_ID, ended=True, scope=False)
        before = snapshot(self.tmp)
        for flags, expected in ((["--dry-run"], 0), ([], 3)):
            with self.subTest(flags=flags):
                code, out, err = self.actual_entry(["sessions", "forget", "--all", *flags])
                self.assertEqual(code, expected, out + err)
                self.assertEqual(snapshot(self.tmp), before)

    def test_actual_entry_single_session_dry_run_writes_nothing(self):
        self.v3_record(managed_id=fx.FIXED_ID)
        state.atomic_write(self.runtime.session_store.sessions_dir / f"{fx.OTHER_ID}.json", b"not-json\n")
        before = snapshot(self.tmp)
        for mid in (fx.FIXED_ID, fx.OTHER_ID):
            with self.subTest(mid=mid):
                code, out, err = self.actual_entry(["sessions", "forget", mid, "--dry-run", "--force"])
                self.assertEqual(code, 0, out + err)
                self.assertIn("Dry run: nothing written.", out)
                self.assertEqual(snapshot(self.tmp), before)

    def test_changed_marker_or_channel_after_confirmation_refuses(self):
        self.record(managed_id=fx.FIXED_ID, ended=True, scope=False)
        before_record = self.runtime.session_store.load_raw(fx.FIXED_ID)[1]
        case = self
        self.runtime.environ["CHARMER_CHANNEL"] = "source"

        class Confirm(io.StringIO):
            def readline(self, *_args):
                state.atomic_write(case.runtime.session_store.root / "state-version", b"999\n")
                return "y\n"

        code, out, err = self.actual_entry(["sessions", "forget", "--all"], Confirm())
        self.assertEqual(code, 1, out + err)
        self.assertIn("newer charmer", err)
        state.atomic_write(self.runtime.session_store.root / "state-version", b"4\n")

        class ChannelConfirm(io.StringIO):
            def readline(self, *_args):
                state.atomic_write(case.runtime.session_store.root / "channel", b"bundle\n")
                return "y\n"

        code, out, err = self.actual_entry(["sessions", "forget", "--all"], ChannelConfirm())
        self.assertEqual(code, 1, out + err)
        self.assertIn("bundle installation", err)
        self.assertEqual(self.runtime.session_store.load_raw(fx.FIXED_ID)[1], before_record)

    def test_read_only_runtime_cannot_be_upgraded_even_with_deferred_flag(self):
        runtime = runtime_mod.Runtime(asset_root=FIXTURE_ROOT, environ=self.runtime.environ, cwd=self.runtime.cwd,
                                      allow_state_writes=False, refresh_shims=False, initialize_session_store=False,
                                      defer_session_batch=True)
        before = snapshot(self.tmp)
        with self.assertRaisesRegex(Exception, "read-only"):
            runtime.begin_session_batch()
        self.assertEqual(snapshot(self.tmp), before)
