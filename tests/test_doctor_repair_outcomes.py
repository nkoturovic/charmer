"""Deferred scope work stays unfinished on every repair surface."""

from __future__ import annotations

import io
from pathlib import Path
from unittest import mock

from charmer import strict_json, transition
from charmer.cli import doctor_actions, launch_flow, parser, session_facts
from test_cli import CLITestCase, FIXED_ID, OTHER_ID
from test_screens_sessions import _SessionsCase, ENTER, ESC, _flat
from test_tui import RIGHT


class SingleRepairOutcomeTests(CLITestCase):
    def deferred_record(self):
        record = self.save_v4_session(write_scope=True)
        record["applied"]["agents"]["cm-reviewer-strong"]["selector"] = "charmer-old-high"
        record["applied_hash"] = strict_json.bundle_digest(record["applied"])
        self.runtime.session_store.save(record)
        return record

    def test_single_repair_deferral_is_exit_three_with_resume_remedy(self) -> None:
        self.deferred_record()
        directory = self.runtime.session_store.root / "scopes" / FIXED_ID
        before = (directory / "settings.json").read_bytes()
        code, out, err = self.run_cli_both(["doctor", "--repair", FIXED_ID], interactive=False)
        self.assertEqual(code, 3, out + err)
        self.assertIn("Repair deferred; scope convergence is unfinished", out)
        self.assertIn(f"charmer -r {FIXED_ID}", out)
        self.assertEqual((directory / "settings.json").read_bytes(), before)

    def test_repair_uses_the_structured_flag_not_report_words(self) -> None:
        self.save_v4_session(write_scope=True)
        for deferred, text, expected in ((True, "fixture result", 3), (False, "deferred is only a word", 0)):
            with self.subTest(deferred=deferred), mock.patch.object(
                    transition, "converge", return_value=transition.ConvergeReport((text,), deferred=deferred)):
                self.assertEqual(doctor_actions._doctor_repair(self.runtime, FIXED_ID, io.StringIO()), expected)

    def test_relink_deferral_keeps_identity_commit_and_reports_on_stderr(self) -> None:
        before = self.deferred_record()
        code, out, err = self.run_cli_both(
            ["sessions", "relink-runtime", FIXED_ID, OTHER_ID], interactive=False)
        self.assertEqual(code, doctor_actions.REPAIR_DEFERRED, out + err)
        self.assertEqual(out, "")
        self.assertEqual(err.count("scope convergence is deferred"), 1)
        self.assertNotIn("scope convergence is unfinished", err)
        self.assertNotIn("charmer doctor --repair", err)
        self.assertTrue(err.rstrip().endswith(f"`charmer -r {FIXED_ID}`"), err)
        after = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(after["runtime_session_id"], OTHER_ID)
        self.assertGreater(after["launch_epoch"], before["launch_epoch"])

    def test_gen_zero_repair_is_deferred_not_success(self) -> None:
        record = self.save_v4_session()
        record["lineup_generation"] = 0
        record.pop("launch_fence", None)
        record.pop("scope_lead", None)
        self.runtime.session_store.save(record)
        code, out, err = self.run_cli_both(["doctor", "--repair", FIXED_ID], interactive=False)
        self.assertEqual(code, 3, out + err)
        self.assertIn("next launch", out)

    def test_line_picker_names_unreadable_records_and_keeps_readable_choices(self) -> None:
        self.save_v4_session()
        path = self.runtime.session_store.sessions_dir / f"{OTHER_ID}.json"
        path.write_bytes(b"not json\n")
        path.chmod(0o600)
        out = io.StringIO()
        selected = launch_flow._choose_session_line(self.runtime, io.StringIO("1\n"), out)
        self.assertEqual(selected, FIXED_ID)
        self.assertIn(f"session record {OTHER_ID} is unreadable", out.getvalue())

    def test_line_picker_does_not_call_an_unreadable_inventory_empty(self) -> None:
        with mock.patch.object(session_facts, "_session_record_scan", return_value=([], ["fixture unavailable"], [])):
            out = io.StringIO()
            self.assertIsNone(launch_flow._choose_session_line(self.runtime, io.StringIO(), out))
        self.assertIn("fixture unavailable", out.getvalue())
        self.assertNotIn("(no recorded sessions)", out.getvalue())

    def test_single_repair_help_discloses_the_deferred_exit_status(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            parser.build_parser().parse_args(["doctor", "--help"])
        self.assertIn("exits 3 when the scope is deferred, not converged", " ".join(out.getvalue().split()))

    def test_repair_all_help_discloses_catalog_deferrals(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            parser.build_parser().parse_args(["doctor", "--help"])
        self.assertIn("deferred until resume or a model choice", " ".join(out.getvalue().split()))


class SessionsDeferredRepairTests(_SessionsCase):
    def test_p_reports_deferred_instead_of_done(self) -> None:
        record = self.record()
        with mock.patch.object(transition, "converge", return_value=transition.ConvergeReport(
                ("fixture catalog change applies at the next resume",), deferred=True)):
            _result, win = self.run_screen(self.screen(), ["p", RIGHT, ENTER, ESC, ESC], width=120)
        self.assertIn(f"repair of {record['managed_id'][:8]}… deferred", win.frames[-1])
        self.assertNotIn(" done", win.frames[-1])
        self.assertIn(f"charmer -r {record['managed_id']}", _flat("\n".join(win.frames)))


class SlugEntryIsolationTests(CLITestCase):
    def test_permission_error_on_one_entry_keeps_other_candidates(self) -> None:
        # No transcript directory: the whole tree is a simulated directory listing.
        children = {Path("/"): [Path("/unreadable"), Path("/project"), Path("/project-dir")],
                    Path("/project"): [Path("/project/dir")]}

        def is_dir(path):
            if path == Path("/unreadable"):
                raise PermissionError("fixture: Python 3.11 Path.is_dir propagates this")
            return True

        with mock.patch.object(Path, "iterdir", lambda path: iter(children.get(path, []))), \
                mock.patch.object(Path, "is_dir", is_dir):
            self.assertEqual(session_facts._decode_project_slug_candidates("-project-dir"),
                             (Path("/project-dir"), Path("/project/dir")))
            self.assertIsNone(session_facts._decode_project_slug("-project-dir"))

    def test_parent_listing_errors_remain_unavailable(self) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("fixture parent")):
            self.assertEqual(session_facts._decode_project_slug_candidates("-project"), ())

    def test_unrelated_programming_errors_still_propagate(self) -> None:
        with mock.patch.object(Path, "iterdir", return_value=iter([Path("/project")])), \
                mock.patch.object(Path, "is_dir", side_effect=ValueError("fixture programming error")):
            with self.assertRaisesRegex(ValueError, "programming error"):
                session_facts._decode_project_slug_candidates("-project")
