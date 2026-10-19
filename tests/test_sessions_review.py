"""Batch regression cases on disposable state and injected process observations."""

from __future__ import annotations

import contextlib
import io
from unittest import mock

from charmer import catalog, cli, installs, sessions, state
from charmer.cli import selection
import _tui_fixture as fx
import test_sessions_bulk as bulk
from test_screens_sessions import _SessionsCase
from test_tui import DOWN, RIGHT, FakeWindow

ESC, ENTER = "\x1b", "\n"


@contextlib.contextmanager
def channel_switch_after_first_commit(case, operation):
    """Model an owner switch between existing phases, under migration EXCLUSIVE."""
    store = case.runtime.session_store
    original_guard = sessions.launcher_write_guard
    original_commit = store.forget_session if operation == "forget" else store.save
    committed, switched, checked = [], [], []
    original_channel_check = installs.check

    def channel_check(root, environ, *, record):
        if switched and not record:
            for lock in (store.lifecycle_lock(fx.OTHER_ID), sessions.migration_lock(root)):
                acquired = lock.acquire(blocking=False)
                if acquired:
                    lock.release()
                case.assertFalse(acquired, "per-record channel check must hold lifecycle and migration locks")
            checked.append(True)
        return original_channel_check(root, environ, record=record)

    def commit(value, *args, **kwargs):
        result = original_commit(value, *args, **kwargs)
        mid = value if operation == "forget" else sessions.managed_id(value)
        if mid == fx.FIXED_ID:
            committed.append(mid)
        return result

    @contextlib.contextmanager
    def phase(root):
        with original_guard(root) as held:
            yield held
        if committed and not switched:
            lock = sessions.migration_lock(root)
            case.assertTrue(lock.acquire(blocking=False), "the first migration phase must have released")
            try:
                state.atomic_write(store.root / "channel", b"bundle\n")
                switched.append(True)
            finally:
                lock.release()

    method = "forget_session" if operation == "forget" else "save"
    with mock.patch.object(store, method, side_effect=commit), \
            mock.patch("charmer.sessions.launcher_write_guard", phase), \
            mock.patch("charmer.installs.check", side_effect=channel_check):
        yield switched
    case.assertEqual(checked, [True])


class PerRecordChannelTests(_SessionsCase):
    def exercise(self, operation, *, tui=False):
        for mid in (fx.FIXED_ID, fx.OTHER_ID):
            self.record(managed_id=mid, ended=True, needs_choice=operation == "choose")
        self.runtime.environ["CHARMER_CHANNEL"] = "source"
        store = self.runtime.session_store
        state.atomic_write(store.root / "channel", b"source\n")
        second_before = store.load_raw(fx.OTHER_ID)[1]
        second_scope = store.root / "scopes" / fx.OTHER_ID
        scope_before = bulk.snapshot(second_scope)
        with channel_switch_after_first_commit(self, operation) as switched:
            if tui:
                screen = self.screen()
                if operation == "forget":
                    keys = ["*", "X", RIGHT, ENTER, ESC, ESC]
                else:
                    names = selection.profile_pick_order(self.runtime)
                    pick = [DOWN] * names.index(catalog.DEFAULT_SEED)
                    keys = ["*", "Q", ENTER, *pick, ENTER, RIGHT, ENTER, ESC, ESC]
                win = FakeWindow(keys, width=120, height=40)
                self.assertIsNone(screen.run(win))
                output = "\n".join(win.frames)
                self.assertEqual(screen.message, "1 succeeded · 1 refused · 0 failed")
            else:
                argv = ["sessions", operation, fx.FIXED_ID, fx.OTHER_ID, "--yes"]
                if operation == "choose":
                    argv += ["--profile", catalog.DEFAULT_SEED]
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = cli.main(argv, runtime=self.runtime, output_stream=out, interactive=False)
                output = out.getvalue()
                self.assertEqual(code, 1, output + err.getvalue())
                self.assertIn("1 succeeded · 1 refused · 0 failed", output)
        self.assertEqual(switched, [True])
        self.assertIn("bundle installation", output)
        self.assertEqual(store.load_raw(fx.OTHER_ID)[1], second_before)
        self.assertEqual(bulk.snapshot(second_scope), scope_before)
        if operation == "forget":
            self.assertFalse(store.exists(fx.FIXED_ID))
        else:
            self.assertIn("pending", store.load(fx.FIXED_ID))
            self.assertNotIn("pending", store.load(fx.OTHER_ID))
        self.assertEqual(self.runtime.launches, [])

    def test_cli_forget_refuses_channel_change_between_commits(self):
        self.exercise("forget")

    def test_cli_choose_refuses_channel_change_between_commits(self):
        self.exercise("choose")

    def test_tui_forget_refuses_channel_change_between_commits(self):
        self.exercise("forget", tui=True)

    def test_tui_choose_refuses_channel_change_between_commits(self):
        self.exercise("choose", tui=True)
