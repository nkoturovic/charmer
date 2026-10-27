"""The add-a-model path, case by case, on the shipped catalog (fixtures only).

Each case is a model the documented path must take on the route its user
named: Haiku 5.5 on the Claude account (shipped now: no longer a candidate,
and adding it names the line), GPT-6.1 Terra on the ChatGPT account (an
operator line rides the codex overlay), MiniMax M3.1 Flash on OpenRouter (the
output-config contracts low to max; its default effort is max), Muse Wonder's
restricted registry ids on the Meta route (named wherever they are offered or
declared), the OpenRouter family suggestions, and the model ids the
declaration grammar does not admit (said in the user's terms). No provider
request; the served set is the fixture's.
"""

from __future__ import annotations

import json
import unittest

import test_operator_kimi_k31 as kimi  # module import: no test classes re-exported
from _catalog import SHIPPED_ROOT
from charmer import discovery, operator as operator_mod, proxy, state, strict_json

DOCS = "https://dev.meta.ai/docs/models, 2026-10-27"


def _efforts(*pairs: str) -> list[str]:
    return [item for pair in pairs for item in ("--effort", pair)]


# A generic Anthropic-compatible provider of the user's (the shape of a
# reviewed preset), for a vendor's direct model ids.
MINIMAX = {"version": 1, "lines": {}, "provider": {
    "kind": "anthropic-compatible", "display": "MiniMax", "base_url": "https://api.minimax.io/anthropic",
    "auth": {"kind": "bearer", "secret_ref": "env:MINIMAX_API_KEY"}, "independence_family": "minimax"}}


class AddPathTests(kimi._ShippedOperatorCase):
    def setUp(self) -> None:
        super().setUp()
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nOPENROUTER_CLAUDE_API_KEY=or-dummy\n"
                                             b"META_CLAUDE_API_KEY=meta-dummy\nMINIMAX_API_KEY=minimax-dummy\n")
        state.ensure_private_dir(self.pdir)
        state.atomic_write(self.pdir / "minimax.json", strict_json.pretty_file_bytes(MINIMAX))

    def rendered(self) -> str:
        code, out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, out + err)
        return (proxy.config_dir(self.runtime.home) / "config.yaml").read_text()

    def test_haiku_ships_and_is_no_longer_a_candidate(self) -> None:
        code, out, err = self.op(["models", "--candidates"])
        self.assertEqual(code, 0, out + err)
        self.assertFalse([line for line in out.splitlines() if " claude-haiku-5-5 " in f"{line} "], out)

    def test_terra_on_the_chatgpt_account_rides_the_codex_overlay(self) -> None:
        levels = ("low", "medium", "high", "xhigh", "max")
        code, out, err = self.op(["models", "add", "openai", "gpt-6.1-terra",
                                  *_efforts(*(f"{level}=reasoning-effort-{level}" for level in levels)),
                                  "--default-effort", "medium", "--context", "400000", "--source", "docs",
                                  "--source-ref", "https://developers.openai.com/api/docs/models, 2026-10-27"])
        self.assertEqual(code, 0, out + err)
        layer = self.runtime.operator_snapshot().layer
        line = layer.lines["custom-gpt-6-1-terra"]
        provider = self.runtime.ordinary_docs["providers"]["providers"]["openai"]
        self.assertEqual(operator_mod.overlay_projection(line, provider),
                         {"channel": "codex", "max_context_length": 400000, "thinking_levels": list(levels)})
        overlay = [source for source in self.runtime.operator_render_plan().overlay if source.name == "gpt-6.1-terra"]
        self.assertEqual([(source.channel, source.origin) for source in overlay],
                         [("codex", "operator line custom-gpt-6-1-terra")])

    def test_minimax_flash_on_openrouter_takes_every_output_contract(self) -> None:
        levels = ("low", "medium", "high", "xhigh", "max")
        code, out, err = self.op(["models", "add", "openrouter", "minimax/minimax-m3.1-flash",
                                  *_efforts(*(f"{level}=output-config-{level}" for level in levels)),
                                  "--default-effort", "max", "--context", "1048576", "--source", "docs",
                                  "--source-ref", "https://openrouter.ai/minimax/minimax-m3.1-flash, 2026-10-27"])
        self.assertEqual(code, 0, out + err)
        key = "custom-minimax-minimax-m3-1-flash"
        entry = self.runtime.operator_snapshot().layer.lines[key].core_entry
        self.assertEqual(entry["default_effort"], "max")
        text = self.rendered()
        for level in levels:
            with self.subTest(level=level):
                at = text.index(f'- name: "{key}-{level}"\n          protocol: "claude"', text.index("\npayload:\n"))
                params = text.index('"output_config.effort": ', at)
                self.assertEqual(text[params:text.index("\n", params)], f'"output_config.effort": "{level}"')

    def test_muse_wonder_restricted_ids_are_named(self) -> None:
        code, out, err = self.op(["models", "--candidates", "--all"])
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        for wire, note in (("muse-wonder-full-1.0", "Muse Code subscriptions only, not on Meta's public Model API"),
                           ("muse-wonder-full-1.0-contributor",
                            "Muse Code subscriptions only, not on Meta's public Model API"),
                           ("muse-wonder-1.0-contributor",
                            "Meta's Model API answered 404 for it when checked (2026-10-27)")):
            with self.subTest(wire=wire):
                at = lines.index(next(line for line in lines if line.startswith(f"meta  {wire}  ")))
                self.assertIn(f"candidate · {note} · ", lines[at + 1])
        self.assertFalse([line for line in lines if line.startswith("meta  muse-wonder-1.0  ")])  # the muse line
        code, out, err = self.op(["models", "add", "meta", "muse-wonder-full-1.0", "--effort",
                                  "high=output-config-high", "--context", "1048576", "--source", "docs",
                                  "--source-ref", DOCS])
        self.assertEqual(code, 0, out + err)
        self.assertIn("note: muse-wonder-full-1.0: Muse Code subscriptions only, not on Meta's public Model API; "
                      "a request may fail", out)

    def test_ids_the_grammar_does_not_admit_are_refused_in_plain_words(self) -> None:
        before = self.state_bytes()
        cases = {
            ("minimax", "MiniMax-M3.1-Flash"): "model ids with capital letters cannot be declared; OpenRouter "
                                               "lists every model in lowercase",
            ("openrouter", "~deepseek/deepseek-flash-latest"): "floating aliases (~…) and variants (…:free) "
                                                               "cannot be declared; use the dated id the listing "
                                                               "shows",
            ("openrouter", "deepseek/deepseek-v4.1-flash:free"): "floating aliases (~…) and variants (…:free) "
                                                                 "cannot be declared; use the dated id the listing "
                                                                 "shows",
        }
        for (provider, wire), why in cases.items():
            with self.subTest(wire=wire):
                code, out, err = self.op(["models", "add", provider, wire, "--context", "1048576", "--source",
                                          "docs", "--source-ref", DOCS])
                self.assertEqual((code, out), (1, ""), err)
                self.assertEqual(err, f"charmer: models add: {wire} cannot be declared: {why} — nothing written\n")
        self.assertEqual(self.state_bytes(), before)


class FamilyAndGrammarTableTests(unittest.TestCase):
    def test_new_prefixes_suggest_only_trusted_labels(self) -> None:
        docs = json.loads((SHIPPED_ROOT / "catalog" / "providers.json").read_text())
        models = json.loads((SHIPPED_ROOT / "catalog" / "models.json").read_text())
        trusted = operator_mod.t1_families({"providers": docs, "models": models})
        self.assertEqual(discovery.openrouter_family_suggestion("openrouter", "meta/muse-fixture", trusted), "meta")
        for wire, label in (("minimax/minimax-m3.1-flash", "minimax"), ("mistralai/fixture", "mistral"),
                            ("z-ai/glm-fixture", "zhipu")):
            with self.subTest(wire=wire):
                self.assertNotIn(label, trusted)  # no new trusted label: nothing suggested
                self.assertIsNone(discovery.openrouter_family_suggestion("openrouter", wire, trusted))
                self.assertEqual(discovery.openrouter_family_suggestion("openrouter", wire, {label}), label)

    def test_the_declarable_id_is_the_schema_pattern(self) -> None:
        schema = json.loads((SHIPPED_ROOT / "schemas" / "operator-provider.schema.json").read_text())
        pattern = schema["properties"]["lines"]["additionalProperties"]["properties"]["wire_model"]["pattern"]
        self.assertEqual(discovery.WIRE.pattern, pattern)
        self.assertIsNone(discovery.wire_problem("moonshotai/kimi-k3.1-pro"))
        self.assertIn("lowercase letters, digits", discovery.wire_problem("a/b/c"))


if __name__ == "__main__":
    unittest.main()
