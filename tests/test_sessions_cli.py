"""Session command reports use complete metadata and truthful outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from charmer import errors, sessions, state, termtext
from charmer.cli import entry
from test_cli import CLITestCase, FIXED_ID, OTHER_ID


class SessionJSONFactsTests(CLITestCase):
    def test_attention_and_liveness_are_independent_facts(self) -> None:
        record = self.save_v4_session()
        for repair_needed in (False, True):
            record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED if repair_needed else sessions.IDENTITY_UNVERIFIED
            self.runtime.session_store.save(record)
            for needs_choice in (False, True):
                for live in (False, True, None):
                    verdict = sessions.BackgroundLiveness(live is not None,
                                                         frozenset({FIXED_ID[:8]}) if live else frozenset())
                    with self.subTest(repair=repair_needed, needs_choice=needs_choice, live=live), \
                            mock.patch.object(self.runtime, "background_liveness", return_value=verdict), \
                            mock.patch.object(self.runtime, "resume_choice",
                                              return_value=SimpleNamespace(needs_choice=needs_choice)):
                        code, out, err = self.run_cli_both(["sessions", "list", "--json"], interactive=False)
                        self.assertEqual(code, 0, out + err)
                        document = json.loads(out)
                        self.assertEqual(document["schema_version"], 1)
                        (row,) = document["sessions"]
                        self.assertIs(row["live"], live)
                        self.assertIs(row["needs_attention"], repair_needed or needs_choice)
                        self.assertIs(row["needs_choice"], needs_choice)
                        self.assertEqual(set(row), {"managed_id", "runtime_session_id", "format", "profile", "lead",
                                                    "agents", "follow", "live", "pending_fork", "needs_attention",
                                                    "pending_change", "ended", "needs_choice", "last_used", "cwd"})


class SessionTerminalTests(CLITestCase):
    def hostile_cwd(self) -> str:
        cwd = self.root / "cwd\x1b]8;;https://bad.example\x07link\n\x7f\x85"
        cwd.mkdir(parents=True)
        return str(cwd)

    def test_listing_escapes_recorded_cwd_controls(self) -> None:
        record = self.save_v4_session()
        record["cwd"] = self.hostile_cwd()
        self.runtime.session_store.save(record)
        code, out, err = self.run_cli_both(["sessions", "list"], interactive=False)
        self.assertEqual(code, 0, out + err)
        self.assertIn(termtext.visible_text(record["cwd"]), out)
        self.assertNotIn("\x1b", out)

    def test_relink_success_escapes_recorded_and_explicit_cwd_controls(self) -> None:
        cwd = self.hostile_cwd()
        for extra in ([], ["--cwd", cwd]):
            with self.subTest(extra=extra):
                record = self.save_v4_session(write_scope=True)
                if not extra:
                    record["cwd"] = cwd
                    self.runtime.session_store.save(record)
                code, out, err = self.run_cli_both(
                    ["sessions", "relink-runtime", FIXED_ID, OTHER_ID, *extra], interactive=False)
                self.assertEqual(code, 0, out + err)
                self.assertEqual(out, f"Reconciled managed {FIXED_ID} to runtime {OTHER_ID} "
                                     f"at CWD {termtext.visible_text(cwd)}.\n")
                self.assertEqual(self.runtime.session_store.load(FIXED_ID)["cwd"], cwd)


class RelinkConvergenceTests(CLITestCase):
    def test_relink_reports_a_skipped_repair_and_keeps_the_identity_commit(self) -> None:
        for failure, reason in (
            (sessions.MigrationBusyError("fixture migration"), "migration or restore in progress"),
            (state.BarrierBusyError(), state.BARRIER_BUSY_TEXT),
        ):
            with self.subTest(failure=type(failure).__name__):
                before = self.save_v4_session(write_scope=True)
                settings = self.runtime.session_store.root / "scopes" / FIXED_ID / "settings.json"
                scope_before = settings.read_bytes()
                with mock.patch("charmer.cli.doctor_actions.sessions.served_change_phase", side_effect=failure):
                    code, out, err = self.run_cli_both(
                        ["sessions", "relink-runtime", FIXED_ID, OTHER_ID], interactive=False)
                self.assertEqual(code, 1, out + err)
                self.assertEqual(out, "")
                self.assertIn("scope convergence is unfinished", err)
                self.assertIn(reason, err)
                self.assertIn(f"charmer doctor --repair {FIXED_ID}", err)
                after = self.runtime.session_store.load(FIXED_ID)
                self.assertEqual(after["runtime_session_id"], OTHER_ID)
                self.assertGreater(after["launch_epoch"], before["launch_epoch"])
                self.assertEqual(settings.read_bytes(), scope_before)
                code, out, err = self.run_cli_both(["doctor", "--repair", FIXED_ID], interactive=False)
                self.assertEqual(code, 0, out + err)

    def test_relink_reports_a_raised_repair_failure_with_the_retry(self) -> None:
        self.save_v4_session(write_scope=True)
        with mock.patch("charmer.cli.doctor_actions._doctor_repair", side_effect=errors.CLIError("fixture failure")):
            code, out, err = self.run_cli_both(["sessions", "relink-runtime", FIXED_ID, OTHER_ID], interactive=False)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(out, "")
        self.assertIn("fixture failure", err)
        self.assertIn(f"charmer doctor --repair {FIXED_ID}", err)
        self.assertEqual(self.runtime.session_store.load(FIXED_ID)["runtime_session_id"], OTHER_ID)

    def test_relink_success_requires_successful_convergence(self) -> None:
        self.save_v4_session(write_scope=True)
        code, out, err = self.run_cli_both(["sessions", "relink-runtime", FIXED_ID, OTHER_ID], interactive=False)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"to runtime {OTHER_ID} at CWD", out)
        self.assertNotIn("unfinished", out)


class SessionInventoryTests(CLITestCase):
    def broken_record(self) -> Path:
        path = self.runtime.session_store.sessions_dir / f"{OTHER_ID}.json"
        path.write_bytes(b"not json\n")
        path.chmod(0o600)
        return path

    def test_json_refuses_an_incomplete_inventory(self) -> None:
        self.save_v4_session()
        broken = self.broken_record()
        for extra in ([], ["--here"], ["--ended"]):
            with self.subTest(extra=extra):
                code, out, err = self.run_cli_both(["sessions", "list", "--json", *extra], interactive=False)
                self.assertEqual(code, 1, out + err)
                self.assertEqual(json.loads(out), {"schema_version": 1, "list": "sessions",
                                                  "status": "unavailable", "error": entry.LIST_UNAVAILABLE})
                self.assertIn(f"session record {OTHER_ID} is unreadable", err)
                self.assertNotIn(OTHER_ID, out)
                self.assertEqual(broken.read_bytes(), b"not json\n")

    def test_human_listings_keep_readable_rows_but_fail(self) -> None:
        self.save_v4_session()
        self.broken_record()
        for argv in (["sessions", "list"], ["sessions", "list", "--here"], ["-r"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli_both(argv, interactive=False)
                self.assertEqual(code, 1, out + err)
                self.assertIn(FIXED_ID[:8], out)
                self.assertIn(f"session record {OTHER_ID} is unreadable", err)
                self.assertNotIn("(no recorded sessions)", out)

    def test_a_wholly_unreadable_inventory_is_not_reported_empty(self) -> None:
        self.broken_record()
        code, out, err = self.run_cli_both(["sessions", "list"], interactive=False)
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("(no recorded sessions)", out)
        self.assertIn(OTHER_ID, err)

    def test_an_unavailable_directory_fails_in_all_listing_forms(self) -> None:
        directory = self.runtime.session_store.sessions_dir
        iterdir = Path.iterdir

        def scan(path):
            if path == directory:
                raise PermissionError("fixture directory unavailable")
            return iterdir(path)

        for argv in (["sessions", "list", "--json"], ["sessions", "list"], ["-r"]):
            with self.subTest(argv=argv), mock.patch.object(Path, "iterdir", scan):
                code, out, err = self.run_cli_both(argv, interactive=False)
                self.assertEqual(code, 1, out + err)
                self.assertIn("session record directory is unavailable", err)
                if "--json" in argv:
                    self.assertEqual(json.loads(out)["status"], "unavailable")
                else:
                    self.assertNotIn("(no recorded sessions)", out)

    def test_a_readable_empty_inventory_still_succeeds(self) -> None:
        code, out, err = self.run_cli_both(["sessions", "list", "--json"], interactive=False)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["sessions"], [])
        code, out, err = self.run_cli_both(["sessions", "list"], interactive=False)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("(no recorded sessions)", out)
