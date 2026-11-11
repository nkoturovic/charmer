"""Removed operator keys keep explicit successor intent, never a route.

The rollback control uses genuine 2.4.0 packaged data and models that
release's merge, which ignored the removed ledger rows. It is not an
execution of the old launcher or a claim about its client's capabilities.
"""

from __future__ import annotations

import copy
import dataclasses
import unittest
from unittest import mock

import test_cli
import test_fable_catalog_upgrade as upgrade
import test_operator as fixtures
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
from charmer import catalog, cli, operator, profile, proxy, render, sessions

KEY = "custom-removed"
NEXT = "custom-next"


def removed(successor, wire="removed-wire"):
    return {"successor": successor, "last_wire": wire, "at": "2026-11-11T00:00:00Z"}


class RemovedSuccessorMergeTests(unittest.TestCase):
    def merged(self, rows, *, files=None, docs=None):
        docs = docs if docs is not None else fixtures._docs()
        ledger = fixtures._ledger(removed=rows)
        layer = fixtures._layer(files or {}, docs=docs, ledger=ledger)
        before = copy.deepcopy((docs, ledger, layer))
        merged = operator.merge_docs(docs, layer)
        self.assertEqual((docs, ledger, layer), before)
        return profile.LineupCatalog.from_docs(merged), merged

    def test_removed_key_resolves_and_records_no_route(self) -> None:
        lcat, merged = self.merged({KEY: removed("opus")})
        resolved = lcat.resolve_key(KEY)
        self.assertEqual(resolved.key, "opus")
        self.assertEqual(resolved.chain, (KEY,))
        self.assertIn("-> opus", resolved.notice)
        entry = lcat.retired[KEY]
        self.assertEqual(entry["last_wire"], "removed-wire")
        self.assertIsNone(entry["provider"])
        self.assertEqual(entry["selectors"], {})
        self.assertNotIn(KEY, merged.get(operator.DISPLACED_KEY, {}))
        self.assertNotIn(KEY, lcat.lines)
        self.assertEqual(merged["providers"], fixtures._docs()["providers"])
        self.assertEqual(merged["models"], fixtures._docs()["models"])

    def test_successor_chains_follow_operator_and_catalog_retirements(self) -> None:
        docs = fixtures._docs()
        retired_key = next(iter(docs["retired"]["retired"]))
        docs["retired"]["retired"][retired_key]["successor"] = "opus"
        lcat, _ = self.merged({KEY: removed(NEXT), NEXT: removed(retired_key)}, docs=docs)
        self.assertEqual(lcat.resolve_key(KEY).key, "opus")
        self.assertEqual(lcat.resolve_key(KEY).chain, (KEY, NEXT, retired_key))

    def test_missing_removed_and_cyclic_successors_need_a_choice(self) -> None:
        for rows in ({KEY: removed(NEXT)},
                     {KEY: removed(NEXT), NEXT: removed(None)},
                     {KEY: removed(NEXT), NEXT: removed(KEY)},
                     {KEY: removed(KEY)}):
            with self.subTest(rows=rows):
                lcat, _ = self.merged(rows)
                resolution = lcat.resolve_key(KEY)
                self.assertIsNone(resolution.key)
                self.assertIn(KEY, resolution.notice)
                self.assertIn("no successor", resolution.notice)
                self.assertIn("needs a model choice", resolution.notice)
                self.assertTrue(sessions.lead_key_needs_choice(KEY, lcat))

    def test_no_successor_keeps_the_unknown_key_behavior(self) -> None:
        lcat, _ = self.merged({KEY: removed(None)})
        self.assertNotIn(KEY, lcat.retired)
        with self.assertRaisesRegex(catalog.CatalogError, "unknown model key"):
            lcat.resolve_key(KEY)

    def test_trusted_live_and_retired_keys_win_over_removal_history(self) -> None:
        docs = fixtures._docs()
        retired_key = next(iter(docs["retired"]["retired"]))
        lcat, _ = self.merged({"opus": removed("sol"), retired_key: removed("sol")}, docs=docs)
        self.assertEqual(lcat.resolve_key("opus").key, "opus")
        self.assertEqual(lcat.retired[retired_key], docs["retired"]["retired"][retired_key])

    def test_redeclared_keys_win_and_invalid_redeclarations_do_not_revive_old_intent(self) -> None:
        files = fixtures._fixture_files()
        key = "custom-acme-small"
        for valid in (True, False):
            with self.subTest(valid=valid):
                documents = copy.deepcopy(files)
                if not valid:
                    documents["acme"]["lines"][key]["unexpected"] = True
                lcat, _ = self.merged({key: removed("opus")}, files=documents)
                self.assertNotIn(key, lcat.retired)
                if valid:
                    self.assertEqual(lcat.resolve_key(key).key, key)
                else:
                    self.assertTrue(sessions.lead_key_needs_choice(key, lcat))

    def test_unrecognized_removal_keys_are_not_catalog_names(self) -> None:
        lcat, _ = self.merged({"bad/key": removed("opus")})
        self.assertNotIn("bad/key", lcat.retired)


@uses_shipped_catalog
class RemovedSuccessorResumeTests(upgrade.FableUpgradeCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_catalog(SHIPPED_ROOT)

    def remove_local(self):
        self.apply_and_serve()
        before = self.runtime.session_store.load(test_cli.FIXED_ID)
        code, out, err = self.op(["models", "rm", upgrade.KEY, "--successor", "fable", "--yes"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.runtime.session_store.load(test_cli.FIXED_ID), before)
        self.assertEqual(self.ledger().removed[upgrade.KEY]["successor"], "fable")

    def resume_case(self, *, follow, live):
        self.remove_local()
        # Synthetic inputs: the same old applied key in each lifecycle/follow
        # state, not an operator record repair or the mechanism under test.
        record = {**self.record, "follow": follow, "last_event_source": "startup" if live else "end"}
        self.runtime.session_store.save(record)
        before = self.state_bytes()
        prepared = self.resume()
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "fable")
        self.assertEqual(prepared.record["applied"]["lead"]["selector"], upgrade.SELECTOR)
        self.assertEqual(self.state_bytes(), before)
        result = self.commit_resume()
        self.assertEqual(result["applied"]["lead"]["key"], "fable")
        self.assertEqual(result["applied"]["lead"]["effort"], "xhigh")
        self.assertEqual(result["applied"]["agents"]["cm-analyst-strong"]["key"], "fable")
        self.assertEqual(result["applied"]["agents"]["cm-analyst-strong"]["effort"], "max")
        self.assertEqual(result["follow"], follow)
        for field in ("managed_id", "runtime_session_id", "runtime_aliases", "created_at", "profile"):
            self.assertEqual(result.get(field), record.get(field), field)

    def test_pinned_ended_record_resumes_onto_successor(self):
        self.resume_case(follow=False, live=False)

    def test_following_ended_record_resumes_onto_successor(self):
        self.resume_case(follow=True, live=False)

    def test_pinned_live_record_resumes_onto_successor(self):
        self.resume_case(follow=False, live=True)

    def test_following_live_record_resumes_onto_successor(self):
        self.resume_case(follow=True, live=True)

    def test_removing_the_last_declaration_keeps_the_ledger_mapping(self):
        self.remove_local()
        code, out, err = self.op(["models", "rm", upgrade.OTHER, "--yes"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.runtime.operator_snapshot().read.files, {})
        self.assertEqual(self.runtime.lineup_catalog().resolve_key(upgrade.KEY).key, "fable")
        self.assertEqual(self.resume().record["applied"]["lead"]["key"], "fable")

    def test_lost_or_cyclic_successor_raises_a_clear_needs_choice(self):
        self.remove_local()
        schemas = operator.load_schemas(SHIPPED_ROOT)
        for rows in ({upgrade.KEY: removed(NEXT), NEXT: removed(None)},
                     {upgrade.KEY: removed(NEXT), NEXT: removed(upgrade.KEY)},
                     {upgrade.KEY: removed(None)}):
            with self.subTest(rows=rows):
                operator.update_ledger(self.env, schemas, lambda doc: doc.update(removed=rows))
                before = self.state_bytes()
                with self.assertRaisesRegex(cli.NeedsChoiceError, "needs a profile or model choice") as error:
                    self.resume()
                self.assertIn(upgrade.KEY, str(error.exception))
                self.assertEqual(self.state_bytes(), before)

    def test_240_ignores_removed_successor_and_requires_a_model_choice(self):
        self.remove_local()
        self.use_catalog(self.old_root)
        before = self.state_bytes()
        snapshot = self.runtime.operator_snapshot()
        # 2.4.0 accepted these ledger rows but did not put them in its merged
        # catalog. Model that old merge over the genuine packaged documents.
        legacy_layer = dataclasses.replace(snapshot.layer, removed={})
        old = profile.LineupCatalog.from_docs(operator.merge_docs(self.runtime.catalog.docs, legacy_layer))
        self.assertNotIn(upgrade.KEY, old.lines)
        self.assertNotIn(upgrade.KEY, old.retired)
        self.assertTrue(sessions.lead_needs_choice(self.record, old))
        self.assertIn("unknown to catalog 43", sessions.lead_choice_notice(self.record, old))
        with mock.patch.object(self.runtime, "lineup_catalog", return_value=old):
            with self.assertRaisesRegex(cli.NeedsChoiceError, "needs a profile or model choice"):
                self.resume()
        self.assertEqual(self.state_bytes(), before)

    def test_mapping_alone_never_adds_a_route_to_the_240_preflight(self):
        self.remove_local()
        schemas = operator.load_schemas(SHIPPED_ROOT)
        ledger = self.ledger()
        # A state after capture cleanup: the recorded selector is now live
        # again, but there is no declaration or capture to serve it in 2.4.0.
        operator.write_ledger(self.env, operator.apply_capture_prune(
            operator.ledger_document(ledger), [upgrade.WIRE], 44), schemas)
        self.runtime.session_store.save({**self.record, "last_event_source": "startup"})
        before = self.state_bytes()
        report = self.rollback()
        self.assertEqual([finding.group for finding in report.findings], [upgrade.rp.SESSIONS])
        self.assertIn(f"uses {upgrade.WIRE}, which 2.4.0 does not serve", report.findings[0].text)
        document = proxy.candidate_document(self.runtime.home, environ=self.runtime.gateway_environ(),
                                             asset_root=self.old_root, state_root=self.runtime.session_store.root)
        self.assertNotIn(upgrade.WIRE, render.rendered_selectors(document))
        self.assertEqual(self.state_bytes(), before)


if __name__ == "__main__":
    unittest.main()
