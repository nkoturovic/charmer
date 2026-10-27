"""Interrupted service installation names only a recovery that can finish it."""

from __future__ import annotations

import datetime
import os
from charmer import gateway_inhibition as inhibition, state, strict_json
from test_gateway_service import _ServiceCase


class HandoffRemedyTests(_ServiceCase):
    def test_new_handoffs_record_the_phase_specific_remedy(self) -> None:
        gateway = self.gateway()
        for phase, remedy in (("install", "charmer gateway service install"),
                              ("refresh", "charmer gateway service install"),
                              ("uninstall", "charmer gateway service uninstall")):
            with self.subTest(phase=phase):
                self.assertIsNone(gateway.begin_handoff(phase, "charmer-gateway"))
                try:
                    record = inhibition.read(self.state_root)
                    self.assertEqual(record.remedy, remedy)
                finally:
                    gateway.end_handoff()

    def test_legacy_install_handoff_has_only_the_install_remedy(self) -> None:
        for phase in ("install", "uninstall"):
            document = {"version": 1, "operation": phase, "unit": "charmer-gateway", "pid": os.getpid(),
                        "started_at": self.world.now.isoformat(), "token": "a" * 32}
            state.atomic_write(inhibition.legacy_path(self.state_root), strict_json.canonical_bytes(document))
            self.assertEqual(inhibition.read(self.state_root).remedy, f"charmer gateway service {phase}")

    def test_an_older_generic_install_remedy_is_corrected_without_a_write(self) -> None:
        token = inhibition.begin(self.state_root, owner="service", purpose="gateway service install (charmer-gateway)",
                                 phase="install", remedy="charmer gateway service install (or: charmer gateway service uninstall)",
                                 expiry=inhibition.owner_process(), now=self.world.now)
        before = inhibition.record_path(self.state_root).read_bytes()
        message, remedy = inhibition.refusal(inhibition.read(self.state_root), "nothing changed", now=self.world.now)
        self.assertIn("gateway service install", remedy)
        self.assertNotIn("uninstall", remedy)
        self.assertIn("inhibited", message)
        self.assertEqual(inhibition.record_path(self.state_root).read_bytes(), before)
        inhibition.end(self.state_root, token)

    def test_uninstall_refuses_an_install_or_refresh_handoff_without_a_service(self) -> None:
        for phase in ("install", "refresh"):
            with self.subTest(phase=phase):
                token = inhibition.begin(self.state_root, owner="service", purpose=f"gateway service {phase} (charmer-gateway)",
                                         phase=phase, remedy="charmer gateway service install", expiry=inhibition.owner_process(),
                                         now=self.world.now - datetime.timedelta(hours=1))
                before = inhibition.record_path(self.state_root).read_bytes()
                manager = self.service()
                effects = list(self.events)
                outcome = manager.uninstall()
                self.assertEqual(self.events, effects)
                after = inhibition.record_path(self.state_root).read_bytes()
                inhibition.end(self.state_root, token)
                self.assertFalse(outcome.ok, outcome)
                self.assertEqual(outcome.status, "inhibited")
                self.assertIn("charmer gateway service install", outcome.remedy)
                self.assertNotIn("service uninstall", outcome.remedy)
                self.assertEqual(after, before)

    def test_uninstall_with_no_handoff_and_no_service_still_succeeds(self) -> None:
        outcome = self.service().uninstall()
        self.assertTrue(outcome.ok, outcome)
        self.assertIn("not installed", outcome.message)
