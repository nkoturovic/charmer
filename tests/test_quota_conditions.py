"""One quota fixture through every surface: each typed condition
(available, stale, exhausted, management-disabled, unavailable, not-ours)
is named by the same line on the command line, in /cm quota and in the
Providers details, and the exit policy follows the condition: the command
exits 1 exactly when no reading was made, /cm quota always 0. The pool
reads are injected; no gateway, management key or provider is contacted."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import charmer
import test_cli
from test_tui import FakeWindow
from charmer import cli, quota, tui
import charmer.cli.consent as consent
import charmer.cli.screens.providers as providers_screen

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
SESSION = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
ESC = "\x1b"


def _credential(*, used: float, observed: datetime) -> quota.Credential:
    window = quota.Window("5h", used, NOW + timedelta(hours=2), timedelta(hours=5))
    return quota.Credential("claude#1", "claude", "active", None, False, False, None, 3, 0, observed, (window,))


# The shared fixture: one pool per typed condition.
POOLS = {
    "available": quota.PoolStatus("ok", read_at=NOW, credentials=(_credential(used=40, observed=NOW),)),
    "stale": quota.PoolStatus("ok", read_at=NOW,
                              credentials=(_credential(used=40, observed=NOW - timedelta(days=40)),)),
    "exhausted": quota.PoolStatus("ok", read_at=NOW, credentials=(_credential(used=100, observed=NOW),)),
    "management-disabled": quota.PoolStatus("no-key"),
    "unavailable": quota.PoolStatus("unavailable"),
    "not-ours": quota.PoolStatus("not-ours"),
}


class QuotaConditionTests(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        clock = mock.patch.object(quota, "_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def pool(self, name: str):
        return mock.patch.object(self.runtime, "pool_status", return_value=POOLS[name])

    def test_the_fixture_covers_every_condition(self) -> None:
        self.assertEqual(set(POOLS), set(quota.CONDITION_TEXT))
        self.assertEqual(set(POOLS), set(quota.CONDITION_EXIT))
        for name, pool in POOLS.items():
            with self.subTest(condition=name):
                self.assertEqual(quota.condition(pool, NOW, pools=("claude",)), name)

    def test_the_command_names_each_condition_and_exits_by_it(self) -> None:
        for name in POOLS:
            with self.subTest(condition=name), self.pool(name):
                code, out, _err = self.op(["quota"])
                self.assertEqual(code, quota.CONDITION_EXIT[name])
                self.assertIn(quota.condition_line(name), out.splitlines())
                code, out, _err = self.op(["quota", "--json"])
                self.assertEqual(code, quota.CONDITION_EXIT[name])
                facts = {fact["code"]: fact for fact in json.loads(out)["facts"]}
                self.assertEqual(facts["quota-condition"]["value"], name)

    def test_cm_quota_names_each_condition_with_status_zero(self) -> None:
        for name in POOLS:
            with self.subTest(condition=name), self.pool(name):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = cli.main(["lineup", "--session", SESSION, "quota"], runtime=self.runtime,
                                    output_stream=out)
                self.assertEqual((code, err.getvalue()), (0, ""))
                self.assertIn(quota.condition_line(name), out.getvalue().splitlines())

    def test_unavailable_cli_and_cm_name_supported_installs_but_json_stays_structured(self) -> None:
        notice = ("Quota readings require a release bundle or the Nix package with its matching gateway; "
                  "use charmer doctor for per-model 429/402/403 counts and /cm fallback <provider>.")
        for name, pool in POOLS.items():
            with self.subTest(condition=name), self.pool(name):
                code, text, err = self.op(["quota"])
                self.assertEqual(code, quota.CONDITION_EXIT[name])
                self.assertEqual(text.splitlines().count(notice), int(pool.state == "unavailable"))
                self.assertEqual(err, "")
                out = io.StringIO()
                code = cli.main(["lineup", "--session", SESSION, "quota"], runtime=self.runtime,
                                output_stream=out)
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().splitlines().count(notice), int(pool.state == "unavailable"))
                code, text, err = self.op(["quota", "--json"])
                self.assertEqual((code, err), (quota.CONDITION_EXIT[name], ""))
                document = json.loads(text)
                expected = quota.report(pool, NOW).document()
                # The original typed condition is still the only extra fact;
                # no presentation hint or diagnostic enters the JSON schema.
                condition = document["facts"].pop()
                self.assertEqual(condition["code"], "quota-condition")
                self.assertEqual(condition["value"], name)
                expected["status"] = "ready" if name == "available" else "attention"
                self.assertEqual(document, expected)
                self.assertNotIn("Nix package", text)

    def test_requested_pages_name_the_quota_boundary_and_bundle_remedy(self) -> None:
        from _layout import REPO_ROOT

        for page in ("providers/claude-account.md", "providers/chatgpt-account.md",
                     "CHEATSHEET.md", "reference/tasks.md"):
            with self.subTest(page=page):
                text = " ".join((REPO_ROOT / "docs" / page).read_text().split())
                self.assertIn("available in release bundles and the Nix package", text)
                if page.startswith("providers/"):
                    row = next(line for line in (REPO_ROOT / "docs" / page).read_text().splitlines()
                               if "requests fail with a usage-limit message" in line)
                    for phrase in ("charmer quota", "charmer doctor", "429/402/403", "/cm fallback"):
                        self.assertIn(phrase, row)

    def test_bundle_cli_cm_and_tui_share_the_failure_inclusive_collector_cache(self) -> None:
        from _layout import GATEWAY_CONTRACT
        from test_quota import FIXTURES
        from charmer import management

        management.prepare_start(self.runtime.home, stopped=True)
        self.runtime.environ.update({management.CHANNEL_ENV: "bundle",
            management.PINNED_BIN_ENV: "/fixture/gateway",
            management.PATCHES_ENV: ",".join(json.loads(GATEWAY_CONTRACT.read_bytes())["patches"])})
        mixed = json.loads((FIXTURES / "auth-files-mixed.json").read_bytes())
        body = json.dumps({"files": [row for row in mixed["files"] if row["provider"] == "claude"]}).encode()
        getter = mock.Mock(return_value=(200, body))
        self.runtime.management_callback = getter
        ticks = [100.0]
        self.runtime.pool_clock = lambda: ticks[0]
        shown = []
        real = tui.TextView

        class RecordingView(real):
            def __init__(self, title, lines, **kwargs):
                shown.extend(lines)
                super().__init__(title, lines, **kwargs)

        for status, expected_state in ((200, "ok"), (401, "mismatch")):
            getter.return_value = status, body
            getter.reset_mock()
            self.runtime._pool_cache = None
            _, text, _ = self.op(["quota", "--json"])
            facts = {row["code"]: row for row in json.loads(text)["facts"]}
            self.assertEqual(facts["management-state"]["value"], expected_state)
            condition_line = quota.condition_line(facts["quota-condition"]["value"])
            ticks[0] += 59
            out = io.StringIO()
            self.assertEqual(cli.main(["lineup", "--session", SESSION, "quota"], runtime=self.runtime,
                                      output_stream=out), 0)
            self.assertIn(condition_line, out.getvalue().splitlines())
            shown.clear()
            with mock.patch.object(charmer.tui, "TextView", RecordingView):
                screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE,
                                                           journal=lambda: None, now=NOW, tz=timezone.utc)
                screen.selected = [row.id for row in screen.rows].index("anthropic")
                screen.run(FakeWindow(["q", ESC, ESC], height=24, width=80))
            self.assertIn(condition_line, shown)
            getter.assert_called_once()
            ticks[0] += 2
            self.assertEqual(self.runtime.pool_status().state, expected_state)
            self.assertEqual(getter.call_count, 2)

    def test_the_tui_shows_each_condition(self) -> None:
        shown: list[tuple[str, list[str]]] = []
        real = tui.TextView

        class RecordingView(real):
            def __init__(self, title, lines, **kwargs):
                shown.append((title, list(lines)))
                super().__init__(title, lines, **kwargs)

        for name in POOLS:
            with self.subTest(condition=name), self.pool(name), \
                    mock.patch.object(charmer.tui, "TextView", RecordingView):
                shown.clear()
                screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE,
                                                           journal=lambda: None, now=NOW, tz=timezone.utc)
                screen.selected = [row.id for row in screen.rows].index("anthropic")
                screen.run(FakeWindow(["q", ESC, ESC], height=24, width=80))
                self.assertIn(quota.condition_line(name), shown[0][1])


if __name__ == "__main__":
    unittest.main()
