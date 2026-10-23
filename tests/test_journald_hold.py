"""Restart-risk veto: synthetic manager, journal, process and control seams only.

Timestamp fixtures follow systemctl(1)'s us+utc style (since 248), and
systemd v252/v262.1 systemctl-show.c print_property's timestamp branch plus
basic/time-util.c format_timestamp_style. No installed tool is queried.
"""
from __future__ import annotations

import datetime
import io
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from charmer import gateway_hold, service, tui
from charmer.cli import doctor, gateway_facts
from charmer.cli.commands import gateway as gateway_commands
from charmer.cli.screens import findings, gateway_actions
from charmer.platform import linux_service, observation
from tests import test_credential_saves as saves_fixture
from tests import test_gateway_lifecycle as lifecycle
from test_tui import FakeWindow

UTC = datetime.timezone.utc
DAY = datetime.timedelta(days=1)
MICROSECOND = datetime.timedelta(microseconds=1)
AT = datetime.datetime(2026, 10, 19, 12, 34, 56, 123456, tzinfo=UTC)


def properties(**overrides) -> str:
    values = dict(LoadState="loaded", ActiveState="active", MainPID="1234",
                  ExecMainStartTimestamp="Mon 2026-10-19 12:34:56.123456 UTC")
    values.update(overrides)
    return "".join(f"{key}={value}\n" for key, value in values.items())


class JournaldObservationTests(unittest.TestCase):
    def run_observation(self, output=properties(), *, code=0, error=None):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], code, output, ""), side_effect=error)
        result = service.journald_start(runner=runner)
        runner.assert_called_once()
        return result, runner

    def test_one_bounded_system_manager_query_with_fixed_utc_microseconds(self):
        with mock.patch.dict("os.environ", {"LC_ALL": "de_DE.UTF-8", "TZ": "Pacific/Honolulu"}):
            result, runner = self.run_observation()
        self.assertEqual(result, AT)
        self.assertEqual(runner.call_args.args[0], [
            "systemctl", "--system", "--no-ask-password", "--no-pager", "show", "--timestamp=us+utc",
            "--property=LoadState", "--property=ActiveState", "--property=MainPID",
            "--property=ExecMainStartTimestamp", "systemd-journald.service"])
        kwargs = runner.call_args.kwargs
        self.assertEqual((kwargs["stdin"], kwargs["capture_output"], kwargs["text"], kwargs["timeout"]),
                         (subprocess.DEVNULL, True, True, 10))
        self.assertEqual((kwargs["env"]["LC_ALL"], kwargs["env"]["TZ"]), ("C", "UTC"))

    def test_supported_systemctl_timestamp_style_including_zero_microseconds(self):
        # v252 and v262.1 print_property handle all *Timestamp properties
        # with arg_timestamp_style (not bus_print_property's default pretty).
        # us+utc always prints .%06u, including .000000, and fixed weekdays.
        for weekday, date, micros in (("Mon", "2026-10-19", "123456"),
                                     ("Tue", "2026-10-20", "000001"),
                                     ("Wed", "2026-10-21", "000000"),
                                     ("Thu", "2026-10-22", "999999"),
                                     ("Fri", "2026-10-23", "000000"),
                                     ("Sat", "2026-10-24", "000001"),
                                     ("Sun", "2026-10-25", "123456")):
            with self.subTest(date=date):
                value = f"{weekday} {date} 01:02:03.{micros} UTC"
                result, _runner = self.run_observation(properties(ExecMainStartTimestamp=value))
                self.assertEqual(result, datetime.datetime.fromisoformat(f"{date}T01:02:03.{micros}+00:00"))

    def test_missing_denied_bus_tool_timeout_and_failing_query_are_unknown(self):
        for error in (FileNotFoundError("systemctl"), PermissionError("system bus"),
                      OSError("system bus unavailable"), subprocess.TimeoutExpired("systemctl", 10)):
            with self.subTest(error=type(error).__name__):
                result, _runner = self.run_observation(error=error)
                self.assertIsNone(result)
        self.assertIsNone(self.run_observation(code=1)[0])

    def test_inactive_missing_duplicate_and_invalid_properties_are_unknown(self):
        variants = [properties(LoadState="not-found"), properties(LoadState="masked"),
                    properties(ActiveState="inactive"), properties(ActiveState="failed"),
                    properties(ActiveState="activating"), "", "not properties\n",
                    properties() + "MainPID=5678\n", properties() + "Unexpected=value\n"]
        for name in linux_service.JOURNALD_PROPERTIES:
            variants.append("\n".join(line for line in properties().splitlines() if not line.startswith(name + "=")))
        variants += [properties(MainPID=value) for value in ("", "0", "-1", "unknown", "1.0", "4294967296")]
        variants += [properties(ExecMainStartTimestamp=value) for value in (
            "", "n/a", "0", "Mon 2026-10-19 12:34:56 UTC", "Mon 2026-10-19 12:34:56.123 UTC",
            "Mon 2026-10-19 12:34:56.1234567 UTC", "Mon 2026-10-19 12:34:56.123456 CEST",
            "Mo 2026-10-19 12:34:56.123456 UTC", "Tue 2026-10-19 12:34:56.123456 UTC",
            "Mon 2026-02-30 12:34:56.123456 UTC", "Mon 2026-10-19 25:00:00.123456 UTC",
            "--- XXXX-XX-XX XX:XX:XX")]
        for output in variants:
            with self.subTest(output=output):
                self.assertIsNone(self.run_observation(output)[0])


class JournaldHoldTests(saves_fixture._JournalHold):
    def check(self, started, *, records=None, running=True, at=saves_fixture.NOW, coverage="bounded", record=True):
        order = []
        records = [self.marker()] if records is None else records

        def read(_since):
            order.append("evidence")
            return observation.LogWindow(tuple(records), coverage)

        def observe():
            order.append("journald")
            return started

        report = gateway_hold.evaluate_journal(
            self.state_root, ("claude",), reader=read, now=lambda: at.isoformat(),
            running=(self.NONCE, self.STARTED) if running else None, record=record,
            reaches=lambda _at: True, drops=lambda _since: (), journald_start=observe)
        self.assertEqual(order, ["evidence", "journald"] if running else ["evidence"])
        return report

    def test_restart_holds_despite_surviving_marker_reach_back_and_no_drop_notice(self):
        report = self.check(self.STARTED + MICROSECOND)
        self.assertTrue(report.held, "a post-boundary journald start must veto complete-looking evidence")
        self.assertTrue(report.coverage_only)
        self.assertFalse(report.journald_unavailable)
        self.assertEqual(report.reasons, ("service journal: journald restarted; credential-save evidence may be missing",))
        self.assertEqual(report.recorded, ())
        self.assertFalse(gateway_hold.hold_path(self.state_root).exists())

    def test_exact_instance_boundary_not_the_scan_margin_or_display_window(self):
        for started, held in ((self.STARTED - DAY, False),
                              (self.STARTED - gateway_hold.START_MARGIN / 2, False),
                              (self.STARTED, False), (self.STARTED + MICROSECOND, True)):
            with self.subTest(started=started):
                self.assertEqual(self.check(started).held, held)

    def test_latest_clear_and_equality_acknowledge_restart_then_another_holds(self):
        restart = self.STARTED + DAY
        report = self.check(restart)
        self.assertTrue(report.held)
        gateway_hold.clear(self.state_root, report)
        cleared = datetime.datetime.fromisoformat(report.evaluated_at)
        for started in (restart, cleared - MICROSECOND, cleared):
            with self.subTest(started=started):
                self.assertFalse(self.check(started, records=[]).held)  # successful reach-back
        self.assertTrue(self.check(cleared + MICROSECOND, at=cleared + DAY).held)
        document = gateway_hold.read_document(self.state_root)
        self.assertTrue(lifecycle._valid_201(document), "a clear must still write the existing hold document")
        self.assertEqual(set(document), {"version", "cleared", "cleared_through", "holds"})
        self.assertEqual(document["holds"], [])

    def test_successful_reach_back_cannot_hide_a_restart_after_latest_clear(self):
        cleared = self.STARTED + DAY
        gateway_hold.clear(self.state_root, gateway_hold.HoldReport(
            False, (), "journal", evaluated_at=cleared.isoformat()))
        report = self.check(cleared + MICROSECOND, records=[])
        self.assertTrue(report.held)
        self.assertEqual(report.reasons, ("service journal: journald restarted; credential-save evidence may be missing",))

    def test_a_clear_before_the_instance_does_not_move_the_boundary_backwards(self):
        earlier = self.STARTED - DAY
        gateway_hold.clear(self.state_root, gateway_hold.HoldReport(
            False, (), "journal", evaluated_at=earlier.isoformat()))
        self.assertFalse(self.check(self.STARTED - MICROSECOND).held)
        self.assertFalse(self.check(self.STARTED).held)
        self.assertTrue(self.check(self.STARTED + MICROSECOND).held)

    def test_unavailable_observation_can_be_acknowledged_for_the_running_instance(self):
        for started in (None, self.STARTED.replace(tzinfo=None), "not a timestamp"):
            with self.subTest(started=started):
                # A clear predating this instance acknowledges none of its missing evidence.
                gateway_hold.clear(self.state_root, gateway_hold.HoldReport(
                    False, (), "journal", evaluated_at=(self.STARTED - DAY).isoformat()))
                report = self.check(started)
                self.assertTrue(report.held)
                self.assertTrue(report.coverage_only)
                self.assertTrue(report.journald_unavailable)
                self.assertEqual(gateway_hold.remedy(report), gateway_hold.COVERAGE_REMEDY)
                gateway_hold.clear(self.state_root, report)
                after = self.check(started)
                self.assertFalse(after.held)
                self.assertIn("journald's current process start remains unavailable", after.evaluated)
                self.assertTrue(lifecycle._valid_201(gateway_hold.read_document(self.state_root)))
        self.assertFalse(self.check(self.STARTED - DAY).held)
        self.assertTrue(self.check(saves_fixture.NOW + MICROSECOND).held)
        with mock.patch.object(self, "STARTED", saves_fixture.NOW + DAY):
            self.assertTrue(self.check(None, at=saves_fixture.NOW + DAY).held)

    def test_unavailable_acknowledgement_never_hides_a_later_failed_save(self):
        report = self.check(None)
        gateway_hold.clear(self.state_root, report)
        failed = self.record(saves_fixture.ENVELOPE + saves_fixture.FAILED,
                             saves_fixture.NOW + MICROSECOND, "later-failure")
        report = self.check(None, records=[self.marker(), failed], at=saves_fixture.NOW + DAY)
        self.assertTrue(report.held)
        self.assertFalse(report.coverage_only)
        self.assertIn(gateway_hold.REMEDY, gateway_hold.remedy(report))
        self.assertTrue(any("evidence" in item for item in report.recorded))
        lost = self.check(None, at=saves_fixture.NOW + DAY)
        self.assertTrue(lost.held)
        self.assertFalse(lost.coverage_only)

    def test_no_running_instance_does_not_observe_journald(self):
        self.assertFalse(self.check(None, records=[], running=False).held)

    def test_failures_are_recorded_and_never_demoted_by_restart_or_unknown_observation(self):
        failed = self.record(saves_fixture.ENVELOPE + saves_fixture.FAILED,
                             self.STARTED + datetime.timedelta(minutes=1), "failed")
        for coverage in ("bounded", "truncated"):
            for started in (None, self.STARTED + DAY):
                with self.subTest(coverage=coverage, started=started):
                    report = self.check(started, records=[self.marker(), failed], coverage=coverage)
                    self.assertTrue(report.held)
                    self.assertFalse(report.coverage_only)
                    self.assertIn(gateway_hold.REMEDY, gateway_hold.remedy(report))
                    self.assertTrue(any("evidence" in item for item in report.recorded))
        # Even after the journal loses it, the recorded failure remains a failure.
        report = self.check(None)
        self.assertFalse(report.coverage_only)
        self.assertIn("a credential save failed", "; ".join(report.reasons))
        self.assertEqual(gateway_hold.REMEDY, gateway_hold.remedy(report))

    def test_unrecorded_failures_and_record_write_errors_keep_unavailable_flag_and_failure_priority(self):
        failed = self.record(saves_fixture.ENVELOPE + saves_fixture.FAILED, self.STARTED + MICROSECOND, "failed")
        report = self.check(None, records=[self.marker(), failed], record=False)
        self.assertTrue(report.journald_unavailable)
        self.assertFalse(report.coverage_only)
        with mock.patch.object(gateway_hold, "_record_journal_evidence", side_effect=OSError("write failed")):
            report = self.check(None, records=[self.marker(), failed])
        self.assertTrue(report.journald_unavailable)
        self.assertFalse(report.coverage_only)
        self.assertIn("failure evidence could not be recorded", "; ".join(report.reasons))


class JournaldLifecycleTests(lifecycle._ServiceBackend):
    def running_gateway(self, started=None):
        self.running()
        self.active = "active"
        self.world.now = lifecycle.NOW + datetime.timedelta(hours=2)
        probe = mock.Mock(return_value=started)
        seams = self.world.seams(runner=self.runner, journal=self.journal(), journald_start=probe)
        return self.gateway(seams=seams), probe

    def test_stop_and_restart_veto_before_any_service_control_then_clear_recovers(self):
        gateway, probe = self.running_gateway(lifecycle.NOW + datetime.timedelta(hours=1))
        for action in (gateway.stop, gateway.restart):
            outcome = action()
            self.assertEqual(outcome.status, "held", outcome.lines())
            self.assertIn("journald restarted; credential-save evidence may be missing", outcome.message)
        self.assertEqual(probe.call_count, 2)
        self.assertFalse(any("stop" in call or "start" in call or "restart" in call for call in self.calls))
        gateway.clear_hold(gateway.persistence_hold())
        self.assertFalse(gateway.persistence_hold().held)
        self.assertEqual(gateway.stop().status, "stopped")
        self.assertIn(["systemctl", "--user", "--no-ask-password", "stop", "charmer-gateway"], self.calls)

    def test_absent_or_unproven_instance_and_on_demand_make_zero_probe_queries(self):
        probe = mock.Mock(side_effect=AssertionError("journald query"))
        seams = self.world.seams(runner=self.runner, journal=self.journal(), journald_start=probe)
        self.assertFalse(self.gateway(seams=seams).persistence_hold().held)
        self.running()
        self.world.alive = None
        self.gateway(seams=seams).persistence_hold()
        probe.assert_not_called()
        lifecycle.endpoint.set_backend(self.home, "on-demand", packaged_port=self.port)
        self.world.alive = True
        self.gateway(seams=seams).persistence_hold()
        probe.assert_not_called()

    def test_missing_injected_probe_never_falls_through_to_the_host(self):
        self.running()
        gateway = self.gateway(seams=self.world.seams(
            journal=self.journal(), journald_start=None,
            runner=mock.Mock(side_effect=AssertionError("manager fallback"))))
        with mock.patch.object(service, "journald_start", side_effect=AssertionError("host observation")):
            report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertTrue(report.journald_unavailable)

    def test_default_observation_uses_the_injected_runner_after_evidence(self):
        self.running()
        order = []

        def read(_since):
            order.append("evidence")
            return self.journal()("charmer-gateway", _since)

        def runner(argv, **kwargs):
            order.append("manager")
            self.assertEqual(argv[-1], "systemd-journald.service")
            return subprocess.CompletedProcess(argv, 0, properties(), "")

        gateway = self.gateway(seams=self.world.seams(runner=runner, journald_start=None))
        with mock.patch.object(gateway_hold, "journal_sources", return_value=(read, lambda _at: True,
                                                                            lambda _since: ())):
            report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("journald restarted", "; ".join(report.reasons))
        self.assertEqual(order, ["evidence", "manager"])

    def test_unavailable_invalid_and_inactive_system_observations_hold_without_control(self):
        self.running()
        answers = [properties(ActiveState="inactive"), properties(MainPID="0"),
                   properties(ExecMainStartTimestamp="n/a"), "", properties().replace(" UTC", " CET")]
        for output in answers:
            with self.subTest(output=output):
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, output, ""))
                gateway = self.gateway(seams=self.world.seams(runner=runner, journald_start=None))
                with mock.patch.object(gateway_hold, "journal_sources", return_value=(
                        lambda since: self.journal()("charmer-gateway", since), lambda _at: True, lambda _since: ())):
                    outcome = gateway.stop()
                self.assertEqual(outcome.status, "held", outcome.lines())
                self.assertEqual(gateway_hold.COVERAGE_REMEDY, outcome.remedy)
                runner.assert_called_once()
                self.assertEqual(runner.call_args.args[0][-1], "systemd-journald.service")

    def test_unavailable_clear_requires_typed_confirmation_then_allows_guarded_stop(self):
        gateway, _probe = self.running_gateway()
        prompt, out = io.StringIO(), io.StringIO()
        with mock.patch.object(gateway_commands.consent, "require_human") as human, \
                mock.patch.object(gateway_commands.consent, "prompt_stream", return_value=prompt), \
                mock.patch.object(gateway_commands.consent, "_answer_stream", side_effect=lambda stream: stream):
            self.assertEqual(gateway_commands._clear_hold(
                gateway, SimpleNamespace(environ={}), io.StringIO("yes\n"), out), 3)
            self.assertFalse(gateway_hold.hold_path(self.state_root).exists())
            self.assertTrue(gateway.persistence_hold().held)
            self.assertEqual(gateway_commands._clear_hold(
                gateway, SimpleNamespace(environ={}), io.StringIO("clear-hold\n"), out), 0)
        human.assert_called_with("charmer gateway clear-hold", {})
        self.assertIn("Type clear-hold to confirm", prompt.getvalue())
        self.assertFalse(gateway.persistence_hold().held)
        self.assertEqual(gateway.observe().state, lifecycle.gl.OURS)
        self.assertEqual(gateway.stop().status, "stopped")

    def test_old_systemd_observation_can_clear_without_restoring_unsupported_timestamp_style(self):
        gateway, _probe = self.running_gateway()
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "unsupported timestamp style"))
        gateway.seams.journald_start = lambda: service.journald_start(runner=runner)
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertTrue(report.coverage_only)
        gateway.clear_hold(report)
        self.assertFalse(gateway.persistence_hold().held)
        self.assertTrue(all("--timestamp=us+utc" in call.args[0] for call in runner.call_args_list))

    def test_known_restart_keeps_the_typed_confirmation_and_a_later_restart_holds_again(self):
        gateway, probe = self.running_gateway(lifecycle.NOW + datetime.timedelta(hours=1))
        prompt, out = io.StringIO(), io.StringIO()
        with mock.patch.object(gateway_commands.consent, "require_human"), \
                mock.patch.object(gateway_commands.consent, "prompt_stream", return_value=prompt), \
                mock.patch.object(gateway_commands.consent, "_answer_stream", side_effect=lambda stream: stream):
            self.assertEqual(gateway_commands._clear_hold(
                gateway, SimpleNamespace(environ={}), io.StringIO("yes\n"), out), 3)
            self.assertTrue(gateway.persistence_hold().held)
            self.assertEqual(gateway_commands._clear_hold(
                gateway, SimpleNamespace(environ={}), io.StringIO("clear-hold\n"), out), 0)
        self.assertIn("Type clear-hold to confirm", prompt.getvalue())
        self.assertFalse(gateway.persistence_hold().held)
        probe.return_value = self.world.now + MICROSECOND
        self.world.now += DAY
        self.assertTrue(gateway.persistence_hold().held)

    def test_doctor_and_findings_offer_coverage_acknowledgement_without_reobservation(self):
        gateway, probe = self.running_gateway()
        runtime = SimpleNamespace(endpoint_error=None, gateway=lambda: gateway, gateway_seams=gateway.seams,
                                  environ={}, home=self.home, health_get=None, served_models_callback=None)
        # The readiness collector contributes pending-restart findings
        # before the gateway facts collector. Local insertion at the front
        # of the latter's list must not be mistaken for global priority.
        pending = service.pending_restart_lines(
            gateway.workdir, signature="changed", launcher_version="2.1.0", reload_failed=lambda: False)
        self.assertTrue(pending)
        with mock.patch.object(gateway, "binary_drift", return_value="gateway binary changed"):
            facts_attention, info = gateway_facts.gateway_runtime_report(runtime)
        attention = [*gateway_facts.in_area(pending, "Gateway"), *facts_attention]
        self.assertIn("gateway restart pending", attention[0])
        row = doctor.present_report([], attention).rows[0]
        self.assertIn("gateway persistence hold", row.text)
        self.assertEqual(row.action, "hold-coverage")
        self.assertIn("after checking the credentials", row.text)
        self.assertIn("charmer gateway clear-hold", row.text)
        self.assertEqual(probe.call_count, 1)
        screen = findings.FindingsScreen(runtime, palette=tui.MONO_PALETTE, tty_in=io.StringIO(),
                                         tty_out=io.StringIO(), report=lambda: ([], info, attention))
        shown = []
        real = tui.TextView

        class Recording(real):
            def run(self, win):
                shown.append((self.title, list(self.lines)))
                return super().run(win)

        with mock.patch.object(tui, "TextView", Recording):
            screen.run(FakeWindow(["\n", "\x1b", "\x1b"], height=24, width=100))
        self.assertEqual(len(shown), 1)
        text = "\n".join(shown[0][1])
        self.assertIn(gateway_actions.HOLD_COVERAGE_ROUTE, text)
        self.assertNotIn(gateway_actions.HOLD_FAILURE_ROUTE, text)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(self.calls, [])

    def test_doctor_unavailable_observation_keeps_failed_save_guidance(self):
        gateway, _probe = self.running_gateway()
        gateway.seams.journal = self.journal(observation.LogRecord(
            lifecycle.save_line("failed"), lifecycle.NOW, "boot:invocation", "failed"))
        runtime = SimpleNamespace(endpoint_error=None, gateway=lambda: gateway, gateway_seams=gateway.seams,
                                  environ={}, home=self.home, health_get=None, served_models_callback=None)
        attention, _info = gateway_facts.gateway_runtime_report(runtime)
        row = doctor.present_report([], attention).rows[0]
        self.assertEqual(row.action, "hold")
        self.assertIn("a credential save failed", row.text)
        self.assertIn(gateway_hold.REMEDY, row.text)
        self.assertNotIn("no failed credential save", row.text)

    def test_gateway_view_preserves_failure_priority_with_unavailable_observation(self):
        gateway, _probe = self.running_gateway()
        for failed in (False, True):
            report = gateway.persistence_hold()
            if failed:
                report = lifecycle.gateway_hold.HoldReport(True, ("a credential save failed", *report.reasons),
                                                           "journal", coverage_only=False,
                                                           journald_unavailable=True)
            view = gateway_actions.GatewayView(gateway, gateway.observe(), hold=report)
            text = "\n".join(view.hold_lines())
            self.assertFalse(view.can_restart)
            if failed:
                self.assertIn(gateway_actions.HOLD_FAILURE_ROUTE, text)
                self.assertNotIn("no failed credential save", text)
            else:
                self.assertIn(gateway_actions.HOLD_COVERAGE_ROUTE, text)
