"""Provider listing and runtime regressions using only private fixture state."""
from __future__ import annotations

import dataclasses
import gc
import json
import weakref
from unittest import mock

from charmer import launch, operator, state, strict_json
from test_cli import CLITestCase
from test_operator import _fixture_files


class RuntimePreparationTests(CLITestCase):
    def test_discarded_results_are_collected_while_held_proofs_stay_current(self) -> None:
        directory = operator.providers_dir(self.runtime.gateway_environ())
        state.ensure_private_dir(directory)
        document = _fixture_files()["lanbox"]
        document["lines"]["custom-lan-model"]["context"]["declared_tokens"] = 200000
        path = directory / "lanbox.json"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        held = self.prepare_direct_v4("custom-lan-model", read_only=True)
        held_id = id(held.result)
        cache = self.runtime._operator_prepared
        self.runtime.revalidate_operator(held)
        discarded = []
        for _ in range(5):
            prepared = self.prepare_direct_v4("custom-lan-model", read_only=True)
            discarded.append((id(prepared.result), weakref.ref(prepared.result)))
            del prepared
        gc.collect()
        for result_id, result_ref in discarded:
            self.assertIsNone(result_ref(), "discarded compile result is still retained")
            self.assertNotIn(result_id, cache)
        self.assertIn(held_id, cache)
        shown = dataclasses.replace(held)
        del held
        gc.collect()
        self.runtime.revalidate_operator(shown)
        document["lines"]["custom-lan-model"]["wire_model"] = "fixture-changed-wire"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        with self.assertRaisesRegex(launch.LaunchError, "changed or its route/provider became unavailable"):
            self.runtime.revalidate_operator(shown)
        del shown
        gc.collect()
        self.assertNotIn(held_id, cache)


class ProviderListingTests(CLITestCase):
    def _install(self, files: dict[str, object]) -> None:
        directory = operator.providers_dir(self.runtime.gateway_environ())
        state.ensure_private_dir(directory)
        for name, document in files.items():
            state.atomic_write(directory / f"{name}.json", strict_json.pretty_file_bytes(document))

    def test_credential_file_names_stay_redacted_in_plain_and_json_lists(self) -> None:
        literal = "fixture-stored-provider-secret"
        shaped = "sk-" + "a1b2c3d4" * 4
        self._install({literal: {}, shaped: {}})
        with mock.patch("charmer.proxy._stored_secret_values", return_value=frozenset({literal})):
            for options in ([], ["--json"]):
                with self.subTest(options=options):
                    code, out, err = self.run_cli_both(["providers", "list", *options], interactive=False)
                    self.assertEqual(code, 0, err)
                    for secret in (literal, shaped):
                        self.assertNotIn(secret, out + err)
                    if options:
                        rows = [row for row in json.loads(out)["providers"] if row["file"]]
                        self.assertEqual(len(rows), 1)  # one aggregate redacted row, no phantom file rows
                        self.assertEqual((rows[0]["id"], rows[0]["file"], rows[0]["state"], rows[0]["problems"]),
                                         ("<redacted>", "providers.d/<redacted>.json", "not-loaded", 2))
                        self.assertEqual(rows[0]["lines"], [])
                    else:
                        self.assertIn("<redacted>\toperator provider · not loaded · 2 problem(s)", out)

    def test_rejected_line_keys_are_screened_without_hiding_ordinary_invalid_files(self) -> None:
        literal = "fixture-stored-line-secret"
        shaped = "custom-sk-" + "a1b2c3d4" * 4
        self._install({"leaked": {"version": 1, "lines": {literal: {}, shaped: {}}},
                       "broken": {}, "Bad_Name": {}, "lanbox": _fixture_files()["lanbox"]})
        with mock.patch("charmer.proxy._stored_secret_values", return_value=frozenset({literal})):
            for options in ([], ["--json"]):
                with self.subTest(options=options):
                    code, out, err = self.run_cli_both(["providers", "list", *options], interactive=False)
                    self.assertEqual(code, 0, err)
                    self.assertNotIn(literal, out + err)
                    self.assertNotIn(shaped, out + err)
                    self.assertIn("broken", out)
                    self.assertIn("Bad_Name", out)
                    self.assertIn("custom-lan-model", out)
                    if options:
                        rows = {row["id"]: row for row in json.loads(out)["providers"]}
                        self.assertGreater(rows["leaked"]["problems"], 0)
                        self.assertEqual(rows["leaked"]["lines"], [])
                        self.assertGreater(rows["broken"]["problems"], 0)
                        self.assertEqual(rows["Bad_Name"]["problems"], 1)

    def test_stored_rejected_line_key_is_not_reintroduced(self) -> None:
        literal = "custom-fixture-stored-line-secret"
        self._install({"leaked": {"version": 1, "lines": {literal: {}}}})
        with mock.patch("charmer.proxy._stored_secret_values", return_value=frozenset({literal})):
            for options in ([], ["--json"]):
                code, out, err = self.run_cli_both(["providers", "list", *options], interactive=False)
                self.assertEqual(code, 0, err)
                self.assertNotIn(literal, out + err)
                self.assertIn("leaked", out)
