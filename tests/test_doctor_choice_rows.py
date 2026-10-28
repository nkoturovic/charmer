"""One lead-choice action, with singular per-session scope descriptions."""

from __future__ import annotations

from unittest import mock

from charmer import legacy
from charmer.cli import doctor, gateway_facts
from _v4 import V4Case


class ChoiceRowTests(V4Case):
    def needs_choice(self):
        record = self.launch_fresh()
        record["applied"]["lead"]["key"] = "custom-missing"
        record["scope_lead"]["key"] = "custom-missing"
        return self.mutate(record["managed_id"], applied=record["applied"], scope_lead=record["scope_lead"])

    def test_record_and_earlier_scope_share_one_choice_action(self) -> None:
        record = self.needs_choice()
        with mock.patch.object(doctor, "_compiled_by_earlier_release", return_value=True):
            problems, _info, attention = doctor._collect_doctor_reports(self.runtime)
        rows = [row for row in doctor.present_report(problems, attention).rows if row.action == "needs-choice"]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn(record["managed_id"][:8], rows[0].text)
        self.assertIn(f"compiled by {legacy.NAME}", rows[0].text)
        self.assertEqual(rows[0].remedy, "charmer sessions choose --needs-choice --profile <name>")
        self.assertNotIn("--repair-all", rows[0].text)
        self.assertIn("launch card's S key", rows[0].text)
        self.assertIn("charmer sessions forget --needs-choice --dry-run", rows[0].text)
        self.assertNotIn("S banner", rows[0].text)

    def test_mixed_choice_rows_keep_every_session_without_claiming_all_scopes_are_earlier(self) -> None:
        earlier = self.needs_choice()
        current = self.needs_choice()
        scope_rows = doctor._check_scope_integrity(self.runtime, earlier, earlier_hooks=True)[1]
        record_rows = gateway_facts.in_area(doctor._doctor_record_report(self.runtime, [earlier, current])[0], "Sessions")
        rows = [row for row in doctor.present_report([], [*scope_rows, *record_rows]).rows
                if row.action == "needs-choice"]
        self.assertEqual(len(rows), 1)
        self.assertIn(earlier["managed_id"][:8], rows[0].text)
        self.assertIn(current["managed_id"][:8], rows[0].text)
        self.assertIn(f"1 scope(s) compiled by {legacy.NAME}", rows[0].text)

    def test_single_scope_public_and_verbose_text_are_singular(self) -> None:
        record = self.needs_choice()
        line, = doctor._check_scope_integrity(self.runtime, record, earlier_hooks=True)[1]
        for text in (line, line.public):
            self.assertNotIn("these recorded lineups", text)
            self.assertIn("this recorded lineup", text)
            self.assertIn("to forget every such session instead", text)
        record = self.launch_fresh()
        line, = doctor._check_scope_integrity(self.runtime, record, earlier_hooks=True)[1]
        self.assertIn("this stopped scope", line.public)
        self.assertNotIn("the stopped ones", line.public)
