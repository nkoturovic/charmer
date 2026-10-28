"""Doctor's grouping: areas, the persistence hold first, collapsed repeats,
advisories apart from Attention, and the JSON level they carry.

The collectors tag their lines where they make them (``gateway_facts.Line``);
``doctor.present_report`` orders and collapses them for plain doctor and for H
alike. These tests run the collectors on fixture homes (operator lines from
``test_operator_agents``) and the text renderer on tagged lines; nothing
reaches a gateway or a provider.
"""

from __future__ import annotations

import copy
import io
import json
import unittest
from unittest import mock

from charmer import catalog, observations
from charmer.cli import doctor, gateway_facts
import charmer.cli.commands.doctor as doctor_command

import test_operator_agents
from test_operator_agents import AGENT_KEY, OTHER_CONTRACTS, record_passes


def _text(problems, info, attention, *, verbose=False) -> list[str]:
    return [line for _role, line in doctor.report_lines(problems, info, attention, verbose=verbose)]


def _profile_lines(n: int) -> list[str]:
    return [gateway_facts.tagged(f"profile p{i}: next launch: lead: unserved (readiness; never a block)",
                                 area="Profiles") for i in range(n)]


class OrderingTests(unittest.TestCase):
    def test_a_held_gateway_is_the_first_attention_line_whatever_came_before(self) -> None:
        hold = gateway_facts.tagged(gateway_facts.HOLD_COVERAGE_TEXT.format(reasons="the journal no longer "
                                                                                    "reaches back"),
                                    area="Gateway", action="hold-coverage")
        restart = gateway_facts.tagged("gateway restart pending: the running gateway runs /old; restart it",
                                       area="Gateway")
        attention = [*_profile_lines(30), restart, hold]  # collected after the profile lines
        lines = _text([], [], attention)
        first = lines.index("Attention") + 2
        self.assertEqual(lines[first - 1], "  Gateway (2)")
        self.assertTrue(lines[first].startswith("    - gateway persistence hold: HELD"), lines[first])
        self.assertTrue(lines[first + 1].startswith("    - gateway restart pending"))
        self.assertEqual([line for line in lines if line.startswith("  ") and not line.startswith("    ")],
                         ["  Gateway (2)", "  Profiles (30)"])

    def test_blocked_first_then_areas_in_order_then_advisories(self) -> None:
        def tag(text, area, **kwargs):
            return gateway_facts.tagged(text, area=area, **kwargs)

        attention = [tag("model m", "Models"), tag("profile p", "Profiles"), tag("session s", "Sessions"),
                     tag("provider x", "Providers"), tag("install i", "Installation"), tag("gateway g", "Gateway"),
                     "an untagged line",
                     tag("optional", "Models", level=doctor.ADVISORY, group=("model", "admission", "m: opt"),
                         short="m: opt", subject="m")]
        problems = [tag("broken session", "Sessions"), tag("broken gateway", "Gateway")]
        lines = _text(problems, [], attention)
        headers = [line for line in lines if not line.startswith(" ")]
        self.assertEqual(headers[:3], ["BLOCKED", "Attention", doctor.COMPACT_ADVISORIES_TITLE])
        self.assertEqual(lines[1:3], ["  - broken gateway", "  - broken session"])
        areas = [line.strip().split(" (")[0] for line in lines if line.startswith("  ") and line.endswith(")")
                 and not line.startswith("    ") and "- " not in line]
        self.assertEqual(areas, [*doctor.AREAS, doctor.OTHER_AREA])
        self.assertEqual(doctor.present_report(problems, attention).status, "blocked")

    def test_collapsed_session_kinds_name_the_first_three_and_expand_with_v(self) -> None:
        summary = "{n} sessions: the compiled permission default mode differs (lazy state)"
        attention = [doctor._per_session(f"session {i}{'0' * 7}: the default mode differs", f"{i}{'0' * 35}",
                                         "default-mode", summary) for i in range(1, 6)]
        lines = _text([], [], attention)
        row = next(line for line in lines if "sessions: the compiled" in line)
        self.assertEqual(row, "    - 5 sessions: the compiled permission default mode differs (lazy state) — "
                              f"10000000, 20000000, 30000000… {doctor.VERBOSE_HINT}")
        verbose = _text([], [], attention, verbose=True)
        self.assertEqual(sum("the default mode differs" in line for line in verbose), 5)
        self.assertIn("  Sessions (5)", verbose)
        # Two kinds never merge; one session is shown as it is.
        single = _text([], [], attention[:1])
        self.assertIn("    - session 10000000: the default mode differs", single)


class UnusedAliasTests(unittest.TestCase):
    def test_unused_aliases_are_one_action_with_every_frozen_subject(self) -> None:
        from charmer import operator

        facts = tuple(operator.UnusedAlias(f"alias-{i}", f"custom-removed-{i // 2}", "fixture", (), True)
                      for i in range(4))
        lines = doctor.unused_alias_lines(facts)
        row, = doctor.present_report([], lines).rows
        self.assertEqual(row.area, "Providers")
        self.assertEqual(row.level, "attention")
        self.assertEqual(row.action, "unused-alias")
        self.assertEqual(row.subjects, tuple(f"alias-{i}" for i in range(4)))
        self.assertEqual(row.remedy, "charmer doctor --prune-aliases")
        self.assertEqual(row.text, "4 aliases left by 2 removed models are not used by any running session: "
                         "charmer doctor --prune-aliases (review first: --preview) — "
                         "alias-0, alias-1, alias-2… (doctor -v lists them)")
        verbose = _text([], [], lines, verbose=True)
        self.assertEqual(len([line for line in verbose if "captured for" in line]), 4)
        single, = doctor.present_report([], doctor.unused_alias_lines(facts[:1])).rows
        self.assertIn("1 alias left by 1 removed model is not used", single.text)


class CollapseIdentityTests(unittest.TestCase):
    def test_lines_collapse_only_on_a_known_identity(self) -> None:
        same_text = ["identical", "identical"]
        lines = _text([], [], same_text)
        self.assertEqual(lines.count("    - identical"), 1)
        different = [gateway_facts.tagged("session a: x", group=("session", "k1"), summary="{n} k1"),
                     gateway_facts.tagged("session b: y", group=("session", "k2"), summary="{n} k2")]
        lines = _text([], [], different)
        self.assertIn("    - session a: x", lines)
        self.assertIn("    - session b: y", lines)

    def test_a_summary_with_braces_is_not_formatted(self) -> None:
        lines = [gateway_facts.tagged(f"profile p{i}: provider x has no credential (set X in ~/{{odd}}/f)",
                                      area="Providers", group=("profiles", "credential", "x"), subjects=(f"p{i}",),
                                      summary="provider x has no credential (set X in ~/{odd}/f); {n} profiles")
                 for i in range(2)]
        text = _text([], [], lines)
        self.assertIn("    - provider x has no credential (set X in ~/{odd}/f); 2 profiles — p0, p1", text)


class ModelIdentityTests(unittest.TestCase):
    """A model row's identity is the model and the finding code, never the
    wording: the real warning builders word some findings without the model
    (an unverified effort) and others per slot (a role recommendation)."""

    ENTRY = {"capabilities": ["agents", "lead"], "roles": ["cm-explorer"], "efforts": ["medium"]}

    def tag(self, finding, *, key, owner, slot):
        return doctor._tag_model(f"profile {owner}: {finding.message}", finding, key=key, owner=owner, slot=slot)

    def test_the_same_wording_on_two_models_is_two_rows_each_naming_its_model(self) -> None:
        from charmer import profile

        lines = [self.tag(finding, key=key, owner=owner, slot="cm-reviewer")
                 for key, owner in (("model-a", "p1"), ("model-b", "p2"))
                 for finding in profile.effort_warnings(self.ENTRY, "high", slot="cm-reviewer")]
        self.assertEqual(lines[0].short, lines[1].short)  # the builder's text names no model
        rows = doctor.present_report([], lines).of(doctor.ADVISORY)
        self.assertEqual([row.subject for row in rows], ["model-a", "model-b"])
        self.assertTrue(rows[0].text.startswith("model-a: effort 'high' unverified for this line (not declared) "
                                                "(1 profile, 1 slot)"), rows[0].text)
        self.assertTrue(rows[1].text.startswith("model-b: effort 'high' unverified"), rows[1].text)

    def test_one_model_worded_per_slot_is_one_row_with_its_distinct_bindings(self) -> None:
        from charmer import profile

        lines = []
        for owner, slot in (("p1", "cm-reviewer"), ("p1", "cm-analyst"), ("p2", "cm-reviewer"),
                            ("p2", "cm-reviewer")):  # the last binding twice: counted once
            lines += [self.tag(finding, key="model-a", owner=owner, slot=slot)
                      for finding in profile.binding_warnings(self.ENTRY, key="model-a", slot=slot, effort="medium",
                                                              family="acme", known_families=("acme",))]
        self.assertEqual({line.group[1] for line in lines}, {"role-recommendation"})
        self.assertEqual(len({line.short for line in lines}), 2)  # one wording per slot
        rows = doctor.present_report([], lines).of(doctor.ADVISORY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text, "model-a: explicit binding overrides the recommendation against … "
                                       "(2 profiles, 3 slots; doctor -v lists them) — a recommendation; the "
                                       "explicit binding stays allowed")
        verbose = _text([], [], lines, verbose=True)
        for slot in ("cm-reviewer", "cm-analyst"):
            self.assertIn(f"  - profile p1: model-a: explicit binding overrides the recommendation against {slot}",
                          verbose)

    def test_a_lineup_wide_finding_is_one_row_per_wording(self) -> None:
        from charmer import profile

        same = profile.Finding("independence-unknown", None, "review independence unknown", "x")
        other = profile.Finding("independence-unknown", None, "review independence unknown for cm-analyst", "x")
        lines = [self.tag(same, key=None, owner="p1", slot=None), self.tag(same, key=None, owner="p2", slot=None),
                 self.tag(other, key=None, owner="p3", slot=None)]
        rows = doctor.present_report([], lines).of(doctor.ADVISORY)
        self.assertEqual([row.text.split(" — ")[0] for row in rows],
                         ["review independence unknown (2 profiles)",
                          "review independence unknown for cm-analyst (1 profile)"])

    def test_variants_that_share_too_little_keep_the_first_wording(self) -> None:
        self.assertEqual(doctor._shared_wording(["k: alpha one", "k: beta two"]), "k: alpha one; and 1 other wording")
        self.assertEqual(doctor._shared_wording(["effort 'high' unverified here", "effort 'max' unverified here"]),
                         "effort … unverified here")


class CompactAdvisoryTests(unittest.TestCase):
    def test_every_advisory_code_has_a_compact_label(self) -> None:
        self.assertEqual(set(doctor._ADVISORY_LABELS), doctor.ADVISORY_CODES)

    def test_plain_doctor_has_one_line_per_model_but_verbose_keeps_each_finding(self) -> None:
        from charmer import profile

        lines = [doctor._tag_model(message, profile.Finding(code, None, message, message), key="custom-x")
                 for code, message in (("admission", "not admitted"), ("qualification", "not qualified"),
                                       ("family-unknown", "family 'local': review independence unknown"))]
        plain = _text([], [], lines)
        self.assertIn("Advisories (optional evidence; does not change the verdict) — doctor -v or H lists each", plain)
        self.assertEqual([line for line in plain if line.startswith("  - ")], [
            "  - custom-x: not admitted, not qualified, family unknown — optional: "
            "charmer models admit custom-x; charmer models qualify custom-x --agents"])
        verbose = _text([], [], lines, verbose=True)
        self.assertEqual([line for line in verbose if line.startswith("  - ")],
                         [f"  - {line}" for line in lines])
        self.assertEqual(len(doctor.present_report([], lines).of(doctor.ADVISORY)), 3)  # H's rows

    def test_compact_qualification_uses_facts_not_message_words(self) -> None:
        from charmer import profile

        facts = profile.AgentFacts(key="custom-x", provider="fixture", admitted=True, route="catalog", d60=False,
                                   route_kind="oauth-pool", family="fixture", t1_families=frozenset(),
                                   evidence="contract-stale")
        finding = profile.Finding("qualification", None, "different wording", "different wording")
        line = doctor._tag_model(finding.message, finding, key="custom-x", facts=facts)
        self.assertIn("  - custom-x: qualification stale — optional: charmer models qualify custom-x --agents",
                      _text([], [], [line]))


class AdvisoryTests(test_operator_agents.AgentCase):
    """3 profiles × 4 slots bound to one model whose qualification predates
    the current pins: one advisory line with the counts, the verdict
    unchanged, and every original with -v."""

    SLOTS = ("cm-reviewer", "cm-analyst", "cm-reviewer-strong", "cm-analyst-strong")

    def setUp(self) -> None:
        super().setUp()
        self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))
        record_passes(self.env, AGENT_KEY, self.digest(), contracts=OTHER_CONTRACTS)
        base = test_operator_agents.agent_document()
        for name in ("stale-a", "stale-b", "stale-c"):
            document = copy.deepcopy(base)
            document["name"] = name
            document["agents"] = {slot: {"model": AGENT_KEY, "effort": "high"} for slot in self.SLOTS}
            self.runtime.profiles.save(document)

    def collected(self):
        return doctor._collect_doctor_reports(self.runtime)

    def test_one_advisory_line_per_model_and_code_with_its_counts(self) -> None:
        problems, info, attention = self.collected()
        stale = [line for line in attention if "qualification predates the current pins" in line]
        self.assertGreaterEqual(len(stale), 12)
        self.assertTrue(all(getattr(line, "level", None) == doctor.ADVISORY for line in stale))
        presentation = doctor.present_report(problems, attention)
        rows = [row for row in presentation.of(doctor.ADVISORY) if "qualification predates" in row.text]
        self.assertEqual(len(rows), 1)
        self.assertIn("(3 profiles, 12 slots)", rows[0].text)
        self.assertIn(f"charmer models qualify {AGENT_KEY} --agents, or M → Q", rows[0].text)
        self.assertEqual((rows[0].action, rows[0].subject), ("advisory-model", AGENT_KEY))
        self.assertFalse([row for row in presentation.of("attention") if "qualification" in row.text])
        lines = _text(problems, info, attention, verbose=True)
        self.assertGreaterEqual(sum("qualification predates the current pins" in line for line in lines), 12)
        self.assertEqual(sum(line.startswith("  - profile stale-a: ") and "predates" in line for line in lines), 4)

    def test_plain_model_summary_counts_distinct_slots_and_drops_duplicate_new_model(self) -> None:
        self.install(self.runtime, self.acme_files(), keys=())
        problems, info, attention = self.collected()
        self.assertTrue(any((getattr(line, "group", None) or ()) == ("model", "admission", AGENT_KEY)
                            for line in attention))
        self.assertFalse(any(str(line).startswith("New model:") and getattr(line, "subject", None) == AGENT_KEY
                             for line in attention))
        lines = _text(problems, info, attention)
        model_rows = [line for line in lines if line.startswith(f"  - {AGENT_KEY}:")]
        self.assertEqual(len(model_rows), 1, model_rows)
        self.assertIn("not admitted, qualification stale", model_rows[0])
        self.assertIn("(3 profiles, 12 slots)", model_rows[0])
        self.assertEqual(model_rows[0].count(f"charmer models qualify {AGENT_KEY} --agents"), 1)
        verbose = _text(problems, info, attention, verbose=True)
        self.assertGreaterEqual(sum("not admitted" in line for line in verbose), 12)

    def test_advisories_never_change_the_verdict(self) -> None:
        problems, info, attention = self.collected()
        without = [line for line in attention if getattr(line, "level", None) != doctor.ADVISORY]
        self.assertEqual(doctor.present_report(problems, attention).status, doctor.present_report(problems, without).status)
        only = doctor.present_report([], [line for line in attention if getattr(line, "level", None) == doctor.ADVISORY])
        self.assertEqual(only.status, "ready")
        lines = _text([], [], only.rows and [line for line in attention
                                             if getattr(line, "level", None) == doctor.ADVISORY])
        self.assertIn("Ready", lines)
        self.assertIn(doctor.COMPACT_ADVISORIES_TITLE, lines)

    def test_a_ready_report_with_advisories_exits_0(self) -> None:
        problems, info, attention = self.collected()
        advisories = [line for line in attention if getattr(line, "level", None) == doctor.ADVISORY]
        out = io.StringIO()
        with mock.patch.object(doctor, "_collect_doctor_reports", return_value=([], info, advisories)):
            code = doctor_command._doctor_command(self.runtime, _args(), input_stream=io.StringIO(),
                                                  output_stream=out, interactive=False, no_color=True)
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn(f"{doctor.COMPACT_ADVISORIES_TITLE}\n", printed)
        self.assertIn("\nReady\n", printed)
        self.assertNotIn("Attention\n", printed)
        self.assertIn(doctor.DOCTOR_SUMMARY["ready"], printed)

    def test_json_gives_advisories_their_level_and_every_finding_its_area(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            doctor_command._doctor_command(self.runtime, _args(json=True), input_stream=io.StringIO(),
                                           output_stream=out, interactive=False, no_color=True)
        document = json.loads(out.getvalue())
        advisories = [item for item in document["diagnostics"] if item["severity"] == doctor.ADVISORY]
        self.assertTrue(advisories)
        self.assertTrue(all(item["area"] == "Models" for item in advisories))
        # The action code of a row the findings list acts on, else the model finding code.
        self.assertLessEqual({"advisory-model", "role-recommendation"}, {item["code"] for item in advisories})
        for item in document["diagnostics"]:
            self.assertEqual(set(item), {"severity", "code", "text", "subject_id", "remedy", "area"})
            self.assertIn(item["area"], (*doctor.AREAS, doctor.OTHER_AREA, None))
            self.assertNotIn(str(self.runtime.home), item["text"])
        levels = [item["severity"] for item in document["diagnostics"]]
        self.assertEqual(document["status"], observations.status_of(levels) if not document["facts"] else
                         observations.status_of([*levels, *(f["classification"] for f in document["facts"])]))
        from charmer import validate
        schema = json.loads((test_operator_agents.FIXTURE_ROOT / "schemas/report.schema.json").read_text())
        report = {key: value for key, value in document.items() if key != "environment"}
        self.assertEqual(validate.validate(report, schema), [])


class DiagnosticTests(unittest.TestCase):
    def test_the_advisory_level_is_checked_like_any_other(self) -> None:
        good = doctor.DoctorDiagnostic(doctor.ADVISORY, "qualification", "Models: advisory", area="Models")
        self.assertEqual(observations.Report("doctor", gateway_facts._doctor_now(), {}, diagnostics=(good,)).status,
                         "ready")
        for kwargs in ({"code": "Not A Code"}, {"area": "Elsewhere"}, {"remedy": "two\nlines"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                doctor.DoctorDiagnostic(**{"severity": doctor.ADVISORY, "code": "a-code", "text": "t", **kwargs})
        with self.assertRaises(ValueError):
            doctor.DoctorDiagnostic("fatal", "a-code", "t")

    def test_an_untyped_line_keeps_the_redacted_text_and_gains_its_codes(self) -> None:
        line = gateway_facts.tagged("session 1234abcd: scope differs (/home/someone/secret path)", area="Sessions",
                                    action="scope-repair")
        diagnostic = doctor._diagnostic("block", line)
        self.assertEqual((diagnostic.severity, diagnostic.code, diagnostic.area), ("block", "scope-repair", "Sessions"))
        self.assertNotIn("someone", diagnostic.text)
        model = gateway_facts.tagged("profile p: k: not qualified", area="Models", level=doctor.ADVISORY,
                                     group=("model", "qualification", "k: not qualified"))
        diagnostic = doctor._diagnostic("attention", model)
        self.assertEqual((diagnostic.severity, diagnostic.code), (doctor.ADVISORY, "qualification"))
        self.assertIn(": advisory;", diagnostic.text)
        self.assertEqual(doctor._diagnostic("attention", "plain line").code, "legacy-line")


def _args(**overrides):
    import argparse

    values = dict(json=False, doctor_verbose=False, doctor_first_run=False, preview=False, doctor_rotate_token=False,
                  doctor_repair=None, doctor_repair_all=False, doctor_include_live=False, doctor_prune=False,
                  doctor_prune_aliases=None)
    values.update(overrides)
    return argparse.Namespace(**values)


if __name__ == "__main__":
    unittest.main()
