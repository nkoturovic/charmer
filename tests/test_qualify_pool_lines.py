"""``charmer models qualify --agents`` on first-party account (OAuth pool)
lines of your own: the ChatGPT account's codex pool and the Claude
account's pool both reach the offline exact-client check, and a line whose
exact-client compile cannot be built refuses before any request.

Every provider-bound request goes through the injected
``Runtime.qualify_http`` fake gateway (never 8317/8316); the exact-client
run is the injected ``exact_client_runner`` (no client process). Lines are
fixture-local.
"""

from __future__ import annotations

import copy
import unittest
from unittest import mock

import test_cli  # module import: no test classes re-exported
from _catalog import FIXTURE_ROOT
from charmer import operator as operator_mod, profile, qualify, state, strict_json

CONTRACTS = qualify.ContractIdentity("c" * 64, "d" * 64)

# A ChatGPT-account line of your own: a gateway-effort map of the codex
# pool's reviewed contracts (the fixture pool reviews high and xhigh).
CODEX_LINE = {"wire_model": "gpt-fixture-terra-9", "display": "GPT fixture Terra", "efforts": {
    "high": "reasoning-effort-high", "xhigh": "reasoning-effort-xhigh"}, "default_effort": "high",
    "context": {"declared_tokens": 1050000, "source": "operator"}}
CLAUDE_LINE = {"wire_model": "claude-fixture-haiku-9", "display": "Claude fixture Haiku",
               "efforts": ["low", "medium", "high"], "default_effort": "medium",
               "context": {"declared_tokens": 1000000, "source": "operator"}}


class _PoolLineCase(test_cli.OperatorCommandCase):
    PROVIDER = ""
    KEY = ""
    LINE: dict = {}

    def setUp(self) -> None:
        super().setUp()
        state.ensure_private_dir(self.pdir)
        state.atomic_write(self.pdir / f"{self.PROVIDER}.json", strict_json.pretty_file_bytes(
            {"version": 1, "lines": {self.KEY: copy.deepcopy(self.LINE)}}))
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        self.serve_current()
        self.inputs: list[qualify.ExactClientInput] = []
        patcher = mock.patch.object(self.runtime, "contract_identity", return_value=CONTRACTS)
        patcher.start()
        self.addCleanup(patcher.stop)

        def run(request):
            self.inputs.append(request)
            return qualify.ExactClientOutcome("pass", "ok")

        self.runtime.exact_client_runner = run

    def evidence(self):
        return operator_mod.load_evidence(self.runtime.gateway_environ(),
                                          operator_mod.load_schemas(FIXTURE_ROOT)).lines[self.KEY]


class ChatGPTAccountQualifyTests(_PoolLineCase):
    PROVIDER = "openai"
    KEY = "custom-terra"
    LINE = CODEX_LINE

    def test_agents_reaches_the_exact_client_check(self) -> None:
        # The Direct seed the exact-client compile starts from leads with
        # Anthropic only; an OpenAI-account line must still lead there.
        self.assertNotIn("openai", self.runtime.catalog.seed_profiles["direct"]["lead_providers"])
        code, out, err = self.op(["models", "qualify", self.KEY, "--agents"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("exact-client check: the pinned Claude Code client runs offline", err)
        # smoke, two efforts, the two tools turns, stream — all through the fake gateway.
        self.assertEqual([label for label, _body in self.http_calls],
                         ["smoke", "effort high", "effort xhigh", "tools (forced) step 1", "tools (forced) step 2",
                          "stream"])
        self.assertIn(f"exact-client {self.KEY}: pass", out)
        (request,) = self.inputs
        self.assertEqual((request.selector, request.wire), (f"{self.KEY}-high[1m]", f"{self.KEY}-high"))
        self.assertEqual((request.efforts, request.client_effort), (("high",), False))
        self.assertEqual(request.plan.settings["model"], request.selector)
        self.assertEqual(self.evidence()["checks"]["exact_client"]["result"], "pass")
        self.assertEqual(self.evidence()["checks"]["smoke"]["result"], "pass")

    def test_an_exact_client_compile_failure_sends_nothing(self) -> None:
        # Any compile problem refuses before the consent and before the
        # battery: no request is sent and no evidence is written.
        failure = profile.ProfileValidationError(("lead_providers: fixture refusal",))
        with mock.patch("charmer.cli.commands.models.exact_client_input", side_effect=failure):
            code, out, err = self.op(["models", "qualify", self.KEY, "--agents"], "y\n")
        self.assertEqual(code, 1, out + err)
        self.assertIn("the exact-client compile failed", err)
        self.assertIn("nothing sent", err)
        self.assertNotIn("Proceed with these listed requests?", err)
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.inputs, [])
        evidence = operator_mod.load_evidence(self.runtime.gateway_environ(), operator_mod.load_schemas(FIXTURE_ROOT))
        self.assertTrue(evidence is None or self.KEY not in evidence.lines)

    def test_smoke_only_needs_no_exact_client_compile(self) -> None:
        with mock.patch("charmer.cli.commands.models.exact_client_input", side_effect=AssertionError("compiled")):
            code, out, err = self.op(["models", "qualify", self.KEY, "--smoke"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([label for label, _body in self.http_calls], ["smoke"])


class ClaudeAccountQualifyTests(_PoolLineCase):
    PROVIDER = "anthropic"
    KEY = "custom-haiku"
    LINE = CLAUDE_LINE

    def test_agents_reaches_the_exact_client_check(self) -> None:
        code, out, err = self.op(["models", "qualify", self.KEY, "--agents", "--tool-choice", "auto"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"exact-client {self.KEY}: pass", out)
        (request,) = self.inputs
        self.assertEqual((request.selector, request.wire), ("claude-fixture-haiku-9[1m]", "claude-fixture-haiku-9"))
        self.assertEqual((request.efforts, request.client_effort), (("low", "medium", "high"), True))


if __name__ == "__main__":
    unittest.main()
