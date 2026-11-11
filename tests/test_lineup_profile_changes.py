"""Profile/session changes on real compiled fixture scopes, without providers."""

from __future__ import annotations

import copy
import unittest

from charmer import lineup
from test_lineup import LineupCase


class NoSubagentsChangesTests(LineupCase):
    launch_profile = None

    def setUp(self):
        super().setUp()
        record = self.launch_fresh(self.direct_target("opus55"), no_subagents=True)
        self.mid, self.rid = record["managed_id"], record["runtime_session_id"]

    def test_agent_targets_are_refused_without_changing_record_or_scope(self):
        for request in ("profile balanced", "set implementer=opus55:xhigh"):
            with self.subTest(request=request):
                before = self.record_bytes(self.mid), self.scope_files()
                code, text, err = self.request(request)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("was started with --no-subagents", text)
                self.assertIn("delegation stays denied for its whole life", text)
                self.assertIn("For agents, start a new session: charmer --profile", text)
                self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)

    def test_lead_only_change_is_allowed(self):
        code, text, err = self.request("direct fable")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("cannot take agents", text)
        self.assertEqual(self.store.load(self.mid)["applied"]["agents"], {})
        self.assertIn("lead_target", self.store.load(self.mid))

    def test_show_warns_about_preexisting_unusable_agents(self):
        populated = self.launch_fresh(self.profile_target())
        applied = copy.deepcopy(populated["applied"])
        applied["no_subagents"] = True
        record = self.mutate(populated["managed_id"], applied=applied)
        self.assertIn("! delegation is denied in this session (--no-subagents): its agents cannot run",
                      lineup.show_text(self.runtime, record))
