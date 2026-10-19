"""Pure controls for seed metadata capture; no native client or listener starts."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import catalog, probe
from tests import test_scope_probe_seeds as seeds


_SENTINELS = {
    key: f"EXCLUDED_{key.upper()}_SENTINEL"
    for key in ("prompt", "tool_result", "client_output", "header", "token", "text", "transcript_path")
}


class SeedDiagnosticMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="charmer-seed-metadata-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environ = {"HOME": str(self.root / "absent-live-home"), "PATH": "/usr/bin:/bin"}
        self.fixture = probe.build_fixture(self.root / "fixture", environ=self.environ)
        self.events = self.fixture.root / "subagent-start.jsonl"
        self.spawns = [rid for rid in catalog.AGENT_ROLE_IDS if rid != "cm-designer"] + [seeds._CANARY]
        self.markers = {rid: f"SEEDPROBE-CONTROL-MARK-{i:02d}" for i, rid in enumerate(self.spawns)}
        self.blocks = [seeds._agent_use(i, rid, self.markers[rid]) for i, rid in enumerate(self.spawns)]
        self.provider = probe.FakeAnthropicProvider(content_markers=self.markers)
        self.responder = seeds._ScriptedResponder(
            [("Agent", lambda: self.blocks)], marker_names=tuple(self.markers)
        )
        daemon = probe.DaemonDomainObservation(
            domain=_SENTINELS["transcript_path"], present_before=False, present_after=False,
            entries_before=(), entries_after=(), unchanged=True,
            expected_fixture_subdomain=_SENTINELS["text"], fixture_subdomain_observed=False,
        )
        self.result = probe.NativeRunResult(
            argv=(_SENTINELS["prompt"], _SENTINELS["token"]), returncode=0, timed_out=False,
            stdout=_SENTINELS["client_output"], stderr=_SENTINELS["client_output"],
            requests=(), daemon=daemon,
        )

    @staticmethod
    def _document(*, tools: tuple[str, ...] = ("Agent",), text: str = "fixture") -> dict:
        return {
            "model": "fixture-model", "max_tokens": 1234,
            "output_config": {"effort": "high"},
            "tools": [{"name": name} for name in tools],
            "system": _SENTINELS["prompt"],
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
            "headers": {"Authorization": _SENTINELS["token"]},
            "text": _SENTINELS["text"],
            "transcript_path": _SENTINELS["transcript_path"],
        }

    def _respond(self, document: dict, *, route: str = "/v1/messages",
                 request_class: str = "main", agent_type: str = "main"):
        headers = {
            "Authorization": _SENTINELS["token"],
            "x-irrelevant-header": _SENTINELS["header"],
            "x-claude-code-request-class": request_class,
            "x-claude-code-agent-type": agent_type,
            "x-claude-code-agent-id": _SENTINELS["header"],
        }
        context = self.provider._record(
            "POST", route, headers, json.dumps(document).encode("utf-8"), document
        )
        return self.responder.respond(document, route, context)

    def _record_start(self, **overrides) -> str:
        event = {
            "hook_event_name": "SubagentStart", "agent_type": "cm-implementer-light",
            "agent_id": "agent_fixture_01", **_SENTINELS, **overrides,
        }
        output = io.StringIO()
        # Execute only this test-local metadata recorder on synthetic stdin,
        # never the product hook shim or a client-generated transcript.
        with (mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))),
              mock.patch.object(sys, "argv", ["record", str(self.events)]),
              contextlib.redirect_stdout(output)):
            exec(seeds._SUBAGENT_RECORDER, {"__name__": "__main__"})
        return output.getvalue()

    def _write(self, *, result=None, provider=None, started_ns=10, finished_ns=20) -> Path:
        planned = seeds._seed_tool_metadata(self.blocks, tuple(self.markers))
        planned[0]["prompt"] = _SENTINELS["prompt"]
        with contextlib.redirect_stdout(io.StringIO()):
            return seeds._write_seed_diagnostics(
                self.root / "evidence", seed="fixture", client="fixture-client",
                fixture=self.fixture, responder=self.responder, events_path=self.events,
                planned_tool_uses=planned, result=result,
                provider=provider or self.provider, started_ns=started_ns, finished_ns=finished_ns,
            )

    def _partial_capture(self) -> dict:
        previous = set((self.root / "evidence").glob("seed-fixture-*/diagnostics.json"))
        with self.assertRaisesRegex(probe.ProbeError, "seed hook metadata capture incomplete"):
            self._write(result=self.result)
        added = set((self.root / "evidence").glob("seed-fixture-*/diagnostics.json")) - previous
        self.assertEqual(len(added), 1)
        serialized = added.pop().read_text(encoding="utf-8")
        self._assert_excluded(serialized)
        return json.loads(serialized)

    def _capture_write_failure(self, *, native_error=False, timed_out=False,
                               routing_failure=False) -> unittest.TestResult:
        outer = self

        def capture(result, provider, started_ns, finished_ns):
            outer._write(result=result, provider=provider,
                         started_ns=started_ns, finished_ns=finished_ns)

        class CaptureCase(seeds._ProbeCase):
            def setUp(self) -> None:
                self.trusted = mock.sentinel.trusted
                self.environ = outer.environ

            def runTest(self) -> None:
                self._run((), fixture=outer.fixture, responder=outer.responder, diagnostics=capture)
                if routing_failure:
                    self.fail("original routing assertion")

        result = unittest.TestResult()
        # TestCase.run invokes cleanup reporting without the class-level native
        # gate. The provider listener and native execution are both mocked.
        with (mock.patch.object(probe.FakeAnthropicProvider, "start", lambda provider: provider),
              mock.patch.object(probe.FakeAnthropicProvider, "stop"),
              mock.patch.object(probe, "run_native", return_value=dataclasses.replace(
                  self.result, timed_out=timed_out, returncode=None if timed_out else 0),
                  side_effect=probe.ProbeError(_SENTINELS["client_output"]) if native_error else None),
              mock.patch.object(seeds.state, "atomic_write", side_effect=OSError(_SENTINELS["token"]))):
            CaptureCase().run(result)
        return result

    def _assert_excluded(self, serialized: str) -> None:
        for sentinel in _SENTINELS.values():
            self.assertNotIn(sentinel, serialized)
        for key in ("argv", "stdout", "stderr", "headers", "hint_header_values",
                    "body_sha256", "message_sha256s", "transcript_path", "content"):
            self.assertNotIn(f'"{key}":', serialized)

    def test_whitelist_persists_correlations_but_no_excluded_payload(self) -> None:
        first = self._document(text=_SENTINELS["prompt"])
        status, reply = self._respond(first)
        self.assertEqual(status, 200)
        self.assertEqual(reply["content"], self.blocks)
        parent = self._document(text=_SENTINELS["text"])
        parent["messages"] += [
            {"role": "assistant", "content": self.blocks},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": self.blocks[0]["id"],
                 "is_error": True, "content": _SENTINELS["tool_result"]},
                {"type": "tool_result", "tool_use_id": self.blocks[1]["id"],
                 "content": [{"type": "text", "text": _SENTINELS["tool_result"]}]},
            ]},
        ]
        status, reply = self._respond(parent)
        # Diagnostics must not make the responder wait, retry or suppress its
        # existing parent completion, even with missing / failed tool results.
        self.assertEqual(status, 200)
        self.assertEqual(reply["content"], [{"type": "text", "text": "PROBE-OK"}])
        self.assertEqual(self._record_start(), "{}\n")
        path = self._write(result=dataclasses.replace(self.result, requests=self.provider.requests))
        serialized = path.read_text(encoding="utf-8")
        self._assert_excluded(serialized)
        record = json.loads(serialized)
        self.assertEqual(set(record), {
            "version", "seed", "client", "planned_tool_uses", "requests", "emitted_tool_uses",
            "remaining_scripted_steps", "step_tools_missing", "provider_recorded_requests",
            "provider_dropped_requests", "subagent_start", "hook_metadata", "run", "layout_bytes",
        })
        self.assertEqual(record["hook_metadata"], {"read_status": "read", "invalid_rows": 0})
        self.assertEqual(len(record["planned_tool_uses"]), 9)
        self.assertEqual(len(record["emitted_tool_uses"]), 9)
        for index, row in enumerate(record["emitted_tool_uses"]):
            self.assertEqual(row, {
                "request_ordinal": 1, "tool_use_id": self.blocks[index]["id"],
                "tool_name": "Agent", "agent_type": self.spawns[index],
                "role_marker": self.spawns[index],
            })
        self.assertEqual(record["remaining_scripted_steps"], [])
        self.assertEqual(record["provider_recorded_requests"], 2)
        self.assertEqual(record["requests"][1]["parent_tool_results"], [
            {"tool_use_id": self.blocks[0]["id"], "is_error": True},
            {"tool_use_id": self.blocks[1]["id"], "is_error": False},
        ])
        for row in record["requests"]:
            self.assertEqual(set(row), {
                "ordinal", "route", "request_class", "agent_type", "markers", "model", "effort",
                "max_tokens", "tool_names", "tool_names_truncated", "is_main", "parent_tool_results",
                "received_monotonic_ns", "response_status", "response_ready_monotonic_ns",
            })
            self.assertEqual(row["route"], "/v1/messages")
            self.assertTrue(row["is_main"])
            self.assertEqual(row["response_status"], 200)
            self.assertGreaterEqual(row["response_ready_monotonic_ns"], row["received_monotonic_ns"])
        start = record["subagent_start"][0]
        self.assertEqual(start["agent_id"], "agent_fixture_01")
        self.assertTrue(start["has_agent_id"])
        self.assertIsInstance(start["observed_monotonic_ns"], int)
        self.assertEqual(record["run"]["process_exit_observed_monotonic_ns"], 20)
        self.assertEqual(record["run"]["returncode"], 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        shutil.rmtree(self.fixture.root)
        self.assertTrue(path.is_file(), "fixture cleanup removed the diagnostic record")

    def test_zero_multiple_and_main_markers_remain_unattributed(self) -> None:
        role = self.spawns[0]
        for text in ("no marker", f"{self.markers[role]} {self.markers[self.spawns[1]]}"):
            self._respond(self._document(text=text), request_class="subagent", agent_type=role)
        self._respond(self._document(text=self.markers[role]))
        self.assertEqual(self.responder.attributed(role), [])
        self._respond(self._document(text=self.markers[role]), request_class="subagent", agent_type=role)
        self.assertEqual(len(self.responder.attributed(role)), 1)
        rows = self.responder.diagnostics()["requests"]
        self.assertEqual([row["is_main"] for row in rows], [False, False, True, False])
        self.assertEqual([len(row["markers"]) for row in rows], [0, 2, 1, 1])
        self.assertEqual([row["ordinal"] for row in rows], [1, 2, 3, 4])
        self.assertEqual({row["model"] for row in rows}, {"fixture-model"})
        self.assertEqual({row["effort"] for row in rows}, {"high"})

    def test_remaining_steps_and_non_message_statuses_are_observed_without_consumption(self) -> None:
        for route, status in (("/v1/messages/count_tokens", 200), ("/unknown", 404), ("/v1/messages", 200)):
            actual, _reply = self._respond(self._document(tools=()), route=route)
            self.assertEqual(actual, status)
        metadata = self.responder.diagnostics()
        self.assertEqual(metadata["remaining_scripted_steps"], ["Agent"])
        self.assertEqual(metadata["emitted_tool_uses"], [])
        self.assertEqual([row["response_status"] for row in metadata["requests"]], [200, 404, 200])
        self.assertEqual(len(self.responder.rows), 1, "non-message routes changed assertion rows")
        # A main-shaped message without the requested Workflow tool still
        # follows the original step-tools-missing and plain completion path.
        self.responder = seeds._ScriptedResponder([("Workflow", lambda: [])])
        _status, reply = self._respond(self._document())
        self.assertEqual(reply["content"], [{"type": "text", "text": "PROBE-OK"}])
        self.assertEqual(self.responder.diagnostics()["remaining_scripted_steps"], ["Workflow"])
        self.assertEqual(self.responder.step_tools_missing, ["Workflow"])

    def test_hook_and_tool_result_identifiers_never_become_arbitrary_text(self) -> None:
        unsafe = "/fixture/" + _SENTINELS["transcript_path"]
        self.assertEqual(self._record_start(agent_id=unsafe), "{}\n")
        event = json.loads(self.events.read_text(encoding="utf-8"))
        self.assertIsNone(event["agent_id"])
        self.assertTrue(event["has_agent_id"])
        document = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": unsafe, "is_error": True,
             "content": _SENTINELS["tool_result"]},
        ]}]}
        self.assertEqual(seeds._seed_parent_results(document), [{"tool_use_id": None, "is_error": True}])
        serialized = self._write().read_text(encoding="utf-8")
        self._assert_excluded(serialized)
        self.assertNotIn(unsafe, serialized)

    def test_two_writes_preserve_first_failure_in_distinct_attempts(self) -> None:
        first = self._write(result=dataclasses.replace(self.result, timed_out=True, returncode=None))
        first_bytes = first.read_bytes()
        second = self._write(result=self.result)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(first.parent.parent, second.parent.parent)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertTrue(json.loads(first_bytes)["run"]["timed_out"])
        self.assertFalse(json.loads(second.read_bytes())["run"]["timed_out"])
        self.assertEqual(second.stat().st_mode & 0o777, 0o600)
        self.assertEqual(second.parent.stat().st_mode & 0o777, 0o700)

    def test_malformed_hook_rows_keep_valid_rows_and_report_only_fixed_metadata(self) -> None:
        self.assertEqual(self._record_start(), "{}\n")
        with self.events.open("ab") as stream:
            stream.write(b'{"text":"' + _SENTINELS["text"].encode() + b'\n')
            stream.write(json.dumps([_SENTINELS["prompt"]]).encode() + b"\n")
            stream.write(b"\xff" + _SENTINELS["token"].encode() + b"\n")
        self.assertEqual(self._record_start(agent_id="agent_fixture_02"), "{}\n")
        with self.events.open("ab") as stream:
            stream.write(b'{"agent_id":"' + _SENTINELS["transcript_path"].encode())
        record = self._partial_capture()
        self.assertEqual(record["hook_metadata"], {"read_status": "read", "invalid_rows": 4})
        self.assertEqual([row["agent_id"] for row in record["subagent_start"]],
                         ["agent_fixture_01", "agent_fixture_02"])
        self.assertEqual(record["run"]["returncode"], 0)

    def test_unreadable_hook_stream_keeps_partial_rows_without_exception_text(self) -> None:
        original_open = Path.open

        def rows():
            yield json.dumps({
                "hook_event_name": "SubagentStart", "agent_type": "cm-implementer-light",
                "agent_id": "agent_fixture_01", "has_agent_id": True,
                "observed_monotonic_ns": 123, **_SENTINELS,
            }).encode() + b"\n"
            raise OSError(_SENTINELS["token"])

        class PartialStream:
            # Like Path.open, the returned object itself must be iterable.
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                return rows()

        for partial in (False, True):
            with self.subTest(partial=partial):
                def open_path(path, *args, **kwargs):
                    if path == self.events:
                        if partial:
                            return PartialStream()
                        raise PermissionError(_SENTINELS["token"])
                    return original_open(path, *args, **kwargs)

                with mock.patch.object(Path, "open", new=open_path):
                    record = self._partial_capture()
                self.assertEqual(record["hook_metadata"], {"read_status": "unreadable", "invalid_rows": 0})
                self.assertEqual([row["agent_id"] for row in record["subagent_start"]],
                                 ["agent_fixture_01"] if partial else [])

    def test_primary_native_error_survives_diagnostic_write_failure(self) -> None:
        result = self._capture_write_failure(native_error=True)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("ProbeError: " + _SENTINELS["client_output"], result.errors[0][1])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("seed diagnostic capture failed", result.failures[0][1])
        self.assertNotIn(_SENTINELS["token"], result.errors[0][1] + result.failures[0][1])
        self.assertFalse(result.wasSuccessful())

    def test_successful_native_run_fails_when_diagnostics_cannot_be_written(self) -> None:
        result = self._capture_write_failure()
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("seed diagnostic capture failed", result.failures[0][1])
        self.assertNotIn(_SENTINELS["token"], result.failures[0][1])
        self.assertFalse(result.wasSuccessful())

    def test_existing_assertions_survive_diagnostic_write_failure(self) -> None:
        for timed_out, routing_failure, expected in (
            (True, False, "the pinned client timed out"),
            (False, True, "original routing assertion"),
        ):
            with self.subTest(assertion=expected):
                result = self._capture_write_failure(timed_out=timed_out, routing_failure=routing_failure)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(result.errors, [])
                self.assertEqual(len(result.failures), 2)
                self.assertIn(expected, result.failures[0][1])
                self.assertIn("seed diagnostic capture failed", result.failures[1][1])
                self.assertNotIn(_SENTINELS["token"], "".join(text for _test, text in result.failures))
                self.assertFalse(result.wasSuccessful())

    def test_capture_precedes_existing_timeout_assertion_and_preserves_failure(self) -> None:
        case = seeds._ProbeCase("runTest")
        case.trusted = mock.sentinel.trusted
        case.environ = self.environ
        captured = []

        def capture(result, provider, started_ns, finished_ns):
            captured.append(self._write(result=result, provider=provider,
                                        started_ns=started_ns, finished_ns=finished_ns))

        with (mock.patch.object(probe.FakeAnthropicProvider, "start", lambda provider: provider),
              mock.patch.object(probe.FakeAnthropicProvider, "stop"),
              mock.patch.object(probe, "run_native", return_value=dataclasses.replace(
                  self.result, timed_out=True, returncode=None)),
              self.assertRaisesRegex(AssertionError, "the pinned client timed out")):
            case._run((), fixture=self.fixture, responder=self.responder, diagnostics=capture)
        self.assertEqual(len(captured), 1)
        record = json.loads(captured[0].read_text(encoding="utf-8"))
        self.assertTrue(record["run"]["timed_out"])
        self.assertEqual(record["run"]["limit_seconds"], 150.0)
        self.assertGreaterEqual(record["run"]["finished_monotonic_ns"], record["run"]["started_monotonic_ns"])

    def test_native_error_still_captures_metadata_without_an_invented_exit(self) -> None:
        case = seeds._ProbeCase("runTest")
        case.trusted = mock.sentinel.trusted
        case.environ = self.environ
        captured = []

        def capture(result, provider, started_ns, finished_ns):
            captured.append(self._write(result=result, provider=provider,
                                        started_ns=started_ns, finished_ns=finished_ns))

        with (mock.patch.object(probe.FakeAnthropicProvider, "start", lambda provider: provider),
              mock.patch.object(probe.FakeAnthropicProvider, "stop"),
              mock.patch.object(probe, "run_native", side_effect=probe.ProbeError(_SENTINELS["client_output"])),
              self.assertRaisesRegex(probe.ProbeError, _SENTINELS["client_output"])):
            case._run((), fixture=self.fixture, responder=self.responder, diagnostics=capture)
        serialized = captured[0].read_text(encoding="utf-8")
        self._assert_excluded(serialized)
        record = json.loads(serialized)
        self.assertFalse(record["run"]["result_available"])
        self.assertIsNone(record["run"]["process_exit_observed_monotonic_ns"])
        self.assertIsNone(record["run"]["returncode"])
        self.assertIsNone(record["run"]["timed_out"])


if __name__ == "__main__":
    unittest.main()
