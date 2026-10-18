"""Synthetic log fixtures only, never a host journal or live gateway."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import gateway_events as events, gateway_hold, launch, service
from charmer.cli import doctor, gateway_facts
from charmer.platform import linux_service, observation
from _layout import PATCH_DIR, UPSTREAM_JSON
from _v4 import V4Case

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
INSTANCE = "a" * 32 + ":" + "b" * 32
INDEX = "0123456789abcdef"
MESSAGE = (
    "credential_save_v1 operation=refresh result=persisted provider=claude "
    f"auth_index={INDEX} credentials_changed=true stage=none errno=0 category=none "
    "bytes=123 size=123 generation=2 epoch=4"
)


# Patch 8's emitter envelope (2.0.1's journal shape: Info from credential_save.go:217).
ENVELOPE = "[2026-09-30 12:00:00] [--------] [info ] [credential_save.go:217] "


def line(message=MESSAGE, **overrides):
    item = {
        "MESSAGE": ENVELOPE + message,
        "__REALTIME_TIMESTAMP": str(int(NOW.timestamp() * 1_000_000)),
        "_BOOT_ID": "a" * 32, "_SYSTEMD_INVOCATION_ID": "b" * 32,
        "__CURSOR": "s=fixture;i=42",
    }
    item.update(overrides)
    # Cursors are deduplicated. Distinct synthetic journal records need
    # distinct cursors too; the old helper accidentally reused one for all.
    if "__CURSOR" not in overrides and (message != MESSAGE or overrides):
        item["__CURSOR"] = "s=fixture;i=" + hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12]
    return json.dumps(item).encode() + b"\n"


def window(message=MESSAGE):
    return linux_service.journal_records(line(message))


class CollectorTests(unittest.TestCase):
    def collect(self, source):
        return events.collect_credential_saves(source, providers={"claude", "codex"})

    def test_metadata_and_source_identity_only(self):
        batch = self.collect(window())
        self.assertEqual(batch.coverage, "bounded")
        event, = batch.events
        self.assertEqual((event.timestamp, event.gateway_instance, event.source_cursor),
                         (NOW, INSTANCE, "s=fixture;i=42"))
        self.assertEqual((event.provider, event.auth_index, event.bytes, event.errno,
                          event.generation, event.epoch), ("claude", INDEX, 123, 0, 2, 4))
        self.assertNotIn("message", asdict(event))
        self.assertNotIn("credential_save.go", repr(batch))

    def test_source_independent_and_optional_cursor(self):
        source = events.LogWindow((events.LogRecord(ENVELOPE + MESSAGE, NOW, "file-generation-7"),), "bounded")
        event, = self.collect(source).events
        self.assertEqual(event.gateway_instance, "file-generation-7")
        self.assertIsNone(event.source_cursor)
        self.assertEqual(self.collect(events.LogWindow()).coverage, "unavailable")
        missing = self.collect(events.LogWindow((events.LogRecord(ENVELOPE + MESSAGE),), "bounded"))
        self.assertEqual(missing.coverage, "incomplete")
        self.assertIsNone(missing.events[0].timestamp)
        self.assertIsNone(missing.events[0].gateway_instance)

    def test_all_outcomes_and_refresh_only_noops(self):
        for outcome in ("persisted", "failed", "unchanged", "skipped", "unverified"):
            with self.subTest(outcome=outcome):
                result = self.collect(window(MESSAGE.replace("result=persisted", f"result={outcome}")))
                self.assertEqual(result.events[0].result, outcome)
        for outcome in ("unchanged", "skipped"):
            result = self.collect(window(MESSAGE.replace("operation=refresh", "operation=register")
                                        .replace("result=persisted", f"result={outcome}")))
            self.assertEqual(result.events, ())
            self.assertEqual(result.coverage, "incomplete")

    def test_malformed_paths_basenames_and_extra_fields_are_never_facts(self):
        for message in (
            "save failed: /private/account@example.invalid.json",
            MESSAGE + " filename=account@example.invalid.json",
            MESSAGE + " /private/auth.json",
            MESSAGE.replace("provider=claude", "provider=secret.json"),
            MESSAGE.replace(INDEX, "account.json"),
            MESSAGE.replace("stage=none", "stage=/private/auth.json"),
            MESSAGE.replace("category=none", "category=token_secret"),
            MESSAGE.replace("bytes=123", "bytes=-1"),
            MESSAGE.replace("generation=2", "generation=" + "9" * 5000),
            MESSAGE.replace("result=persisted", "result=success"),
            "prefix " + MESSAGE, MESSAGE + "\npath=/private/auth.json",
        ):
            with self.subTest(message=message[:100]):
                self.assertEqual(self.collect(window(message)).events, ())
        raw = line(MESSAGE) + line(MESSAGE + " path=/private/auth.json")
        batch = self.collect(linux_service.journal_records(raw))
        self.assertEqual(len(batch.events), 1)
        self.assertEqual(batch.coverage, "incomplete")
        self.assertNotIn("/private", repr(batch))

    def test_unknown_index_is_explicit_not_qualifying(self):
        batch = self.collect(window(MESSAGE.replace(INDEX, "invalid")))
        self.assertEqual(batch.events[0].auth_index, "invalid")
        self.assertEqual(batch.coverage, "incomplete")

    def test_restarts_preserve_instance_and_generation(self):
        raw = line() + line(_SYSTEMD_INVOCATION_ID="c" * 32)
        batch = self.collect(linux_service.journal_records(raw))
        self.assertNotEqual(batch.events[0].gateway_instance, batch.events[1].gateway_instance)
        self.assertEqual(batch.events[0].generation, batch.events[1].generation)

    def test_event_bound_is_explicit_and_keeps_recent_events(self):
        source = window().records[0]
        records = tuple(replace(source, source_cursor=f"cursor-{n}", message=source.message.replace("generation=2", f"generation={n}"))
                        for n in range(events.MAX_EVENTS + 3))
        batch = self.collect(events.LogWindow(records, "bounded"))
        self.assertEqual(len(batch.events), events.MAX_EVENTS)
        self.assertEqual(batch.events[0].generation, 3)
        self.assertEqual(batch.coverage, "truncated")

    def test_source_normalization_rejects_malformed_or_path_metadata(self):
        for raw in (b"not JSON\n", b"[]\n", b'{"MESSAGE":[1,2]}\n'):
            batch = linux_service.journal_records(raw)
            self.assertEqual(batch.records, ())
            self.assertEqual(batch.coverage, "incomplete")
        # A long record stays a source record (the journal recognizers still count it);
        # only the credential-save collector refuses a long receipt-shaped message.
        long_record = linux_service.journal_records(line("x" * (events.MAX_MESSAGE + 1)))
        self.assertEqual((len(long_record.records), long_record.coverage), (1, "bounded"))
        self.assertEqual(self.collect(long_record), events.CredentialSaves((), "bounded"))
        long_receipt = self.collect(window(MESSAGE + " " * events.MAX_MESSAGE))
        self.assertEqual((long_receipt.events, long_receipt.coverage), ((), "incomplete"))
        record, = linux_service.journal_records(line(__CURSOR="/private/secret", _BOOT_ID="secret.json",
                                                    __REALTIME_TIMESTAMP="invalid")).records
        self.assertIsNone(record.source_cursor)
        self.assertIsNone(record.gateway_instance)
        self.assertIsNone(record.timestamp)

    def test_source_pins_patch8_format(self):
        patch = (PATCH_DIR / "cli-proxy-api-credential-save-report.patch").read_text()
        self.assertIn('credential_save_v1 operation=%s result=%s provider=%s auth_index=%s '
                      'credentials_changed=%t stage=%s errno=%d category=%s bytes=%d size=%d '
                      'generation=%d epoch=%d', patch)


class JournalSourceTests(unittest.TestCase):
    def run_source(self, program, *, max_bytes=65536, timeout=2):
        calls = []
        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.Popen([sys.executable, "-c", program], **kwargs)
        result = linux_service.read_gateway_journal(unit="fixture-unit", max_bytes=max_bytes,
                                                   timeout=timeout, popen=spawn)
        self.assertEqual(calls[0][0][0], "journalctl")
        self.assertIn("json", calls[0][0])
        self.assertIn("fixture-unit", calls[0][0])
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)
        return result

    def test_success_and_empty_are_bounded_not_complete(self):
        result = self.run_source(f"import os; os.write(1, {line()!r})")
        self.assertEqual(result.coverage, "bounded")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(self.run_source("pass").coverage, "bounded")

    def test_reader_runs_newest_first_over_the_whole_day_and_returns_chronological(self):
        calls = []
        def spawn(argv, **kwargs):
            calls.append(argv)
            newest_first = line(_SYSTEMD_INVOCATION_ID="c" * 32) + line()
            return subprocess.Popen([sys.executable, "-c", f"import os; os.write(1, {newest_first!r})"], **kwargs)
        result = linux_service.read_gateway_journal(unit="fixture-unit", max_bytes=65536, popen=spawn)
        self.assertIn("--reverse", calls[0])
        self.assertIn("-24h", calls[0])
        self.assertNotIn("-n", calls[0], "a record count must not cut the 24 h window")
        self.assertEqual([record.gateway_instance[-1] for record in result.records], ["b", "c"])

    def test_caps_keep_the_newest_records(self):
        newest_first = b"".join(line(__CURSOR=f"s=fixture;i={n}") for n in range(5, 0, -1))
        with mock.patch.object(linux_service.observation, "LOG_MAX_RECORDS", 3):
            window = linux_service.journal_records(newest_first, newest_first=True)
        self.assertEqual(window.coverage, "truncated")
        self.assertEqual([record.source_cursor for record in window.records],
                         ["s=fixture;i=3", "s=fixture;i=4", "s=fixture;i=5"])
        cut = self.run_source(f"import os; os.write(1, {newest_first!r})", max_bytes=len(newest_first) * 2 // 5 + 3)
        self.assertEqual(cut.coverage, "truncated")
        self.assertEqual([record.source_cursor for record in cut.records], ["s=fixture;i=4", "s=fixture;i=5"])

    def test_byte_limit_and_timeout_kill_reader_without_unbounded_buffering(self):
        result = self.run_source("import os; os.write(1, b'x' * 1000000)", max_bytes=512)
        self.assertEqual(result.coverage, "truncated")
        self.assertEqual(result.records, ())
        result = self.run_source("import time; time.sleep(30)", timeout=0.05)
        self.assertEqual(result.coverage, "incomplete")

    def test_source_exit_timeout_retains_partial_and_marks_timed_out(self):
        program = "import os,time; os.write(1, " + repr(line()) + "); os.close(1); time.sleep(30)"
        result = self.run_source(program, timeout=0.1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.coverage, "incomplete")
        self.assertEqual(len(result.records), 1)

    def test_failure_and_missing_reader_are_unavailable(self):
        self.assertEqual(self.run_source("raise SystemExit(1)").coverage, "unavailable")
        with mock.patch.object(linux_service, "read_gateway_journal", return_value=events.LogWindow()) as read:
            self.assertEqual(service.read_gateway_journal(max_bytes=1024).coverage, "unavailable")
            read.assert_called_once_with(unit=service.UNIT, max_bytes=1024, since="-24h")
        self.assertEqual(linux_service.read_gateway_journal(
            unit="fixture", max_bytes=512, popen=mock.Mock(side_effect=FileNotFoundError())).coverage,
            "unavailable")


class DoctorSaveTests(V4Case):
    def report(self, source):
        info = []
        attention = doctor._doctor_journal_report(self.runtime, source, info_lines=info)
        return attention, info

    FAILED = (MESSAGE.replace("result=persisted", "result=failed").replace("stage=none", "stage=dirsync")
              .replace("errno=0", "errno=28").replace("category=none", "category=errno"))

    def test_failed_alone_is_attention_with_the_hold_remedy(self):
        attention, info = self.report(linux_service.journal_records(line(self.FAILED)))
        self.assertEqual(len(attention), 1)
        for value in ("failed provider=claude", f"auth_index={INDEX}", "bytes=123/123", "errno=28",
                      "keep the gateway running", "free space", "wait for a persisted save",
                      "re-authenticate with charmer providers sign-in anthropic only if none appears",
                      "before any restart or rollback", "retain auth.pre-*"):
            self.assertIn(value, attention[0])
        self.assertTrue(any("coverage=bounded" in text for text in info))

    def test_later_repair_evidence_changes_the_remedy_but_keeps_the_failure_visible(self):
        unverified = MESSAGE.replace("result=persisted", "result=unverified").replace("category=none", "category=no_sync")
        for label, later in (("persisted", MESSAGE), ("unverified changed", unverified),
                             ("newer generation", MESSAGE.replace("generation=2", "generation=3"))):
            with self.subTest(label):
                attention, info = self.report(linux_service.journal_records(line(self.FAILED) + line(later)))
                self.assertEqual(attention, [], "repair evidence satisfies the hold")
                failure, = [text for text in info if "failed provider=claude" in text]
                self.assertIn("no re-authentication needed", failure)
                self.assertIn("retain auth.pre-*", failure)
                self.assertNotIn("re-authenticate with", failure)
                if "unverified" in label:
                    self.assertIn("credentials_changed=true (repair evidence, not a verified save)", failure)
                    self.assertTrue(any("result=unverified" not in text and "unverified provider=claude" in text
                                        and "credentials_changed=true" in text and "not a verified save" in text
                                        for text in info))

    def test_nonqualifying_later_events_keep_the_hold(self):
        unverified = MESSAGE.replace("result=persisted", "result=unverified")
        cases = {
            "unverified unchanged": [line(unverified.replace("credentials_changed=true", "credentials_changed=false"))],
            "superseded": [line(unverified.replace("category=none", "category=superseded"))],
            "unchanged": [line(MESSAGE.replace("result=persisted", "result=unchanged"))],
            "other instance": [line(_SYSTEMD_INVOCATION_ID="c" * 32)],
            "other credential": [line(MESSAGE.replace(INDEX, "fedcba9876543210"))],
            "older generation": [line(MESSAGE.replace("generation=2", "generation=1"))],
            "failed again": [line(), line(self.FAILED.replace("errno=28", "errno=5"))],
            "unknown instance": [line(_BOOT_ID="unknown")],
        }
        for label, later in cases.items():
            with self.subTest(label):
                attention, _info = self.report(linux_service.journal_records(line(self.FAILED) + b"".join(later)))
                self.assertEqual(len(attention), 1)
                self.assertIn("only if none appears", attention[0])

    def test_partial_windows_qualify_the_journal_counts(self):
        status = '[2026-09-30 11:00:00] [req-1] [info ] [gin.go:1] 403 | 1ms | 127.0.0.1 | POST "/v1/messages"'
        grant = "[2026-09-30 11:00:00] [--------] [warn ] [x.go:1] codex refresh failed: invalid_grant"
        raw = line(MESSAGE=status, __CURSOR="status") + line(MESSAGE=grant, __CURSOR="grant")
        oldest = NOW - timedelta(hours=1)
        records = linux_service.journal_records(raw).records + (events.LogRecord("unrelated", oldest),)
        for coverage, timed_out, qualified in (("bounded", False, False), ("truncated", False, True),
                                               ("incomplete", False, True), ("bounded", True, True)):
            with self.subTest(coverage=coverage, timed_out=timed_out):
                source = events.LogWindow(records, coverage, timed_out=timed_out)
                with mock.patch.object(gateway_facts, "_pool_credential_mtimes", return_value={}):
                    attention, _info = self.report(source)
                quota, = [text for text in attention if "× 403" in text]
                dead, = [text for text in attention if "invalid_grant" in text]
                self.assertIn("in the last 24 h", quota)
                for text in (quota, dead):
                    self.assertEqual("partial journal read" in text, qualified, text)
                    if qualified:
                        self.assertIn(f"since {oldest.astimezone():%Y-%m-%d %H:%M}, coverage={coverage}", text)
                    self.assertEqual("timed out" in text, timed_out)

    def test_log_window_substitutions_keep_live_and_retired_catalog_labels(self):
        docs = self.runtime.ordinary_docs
        for collection, field in ((docs["models"]["models"], "wire_model"),
                                  (docs["retired"]["retired"], "last_wire")):
            key, model = next(iter(collection.items()))
            wire = model[field]
            with self.subTest(key=key):
                message = (f'[2026-09-30 12:00:00] [req-1] [warn ] [fixture.go:1] '
                           f'claude executor: upstream served model "fixture-served" for requested model "{wire}"')
                source = events.LogWindow((events.LogRecord(message, NOW, INSTANCE),), "bounded")
                attention, _info = self.report(source)
                substitution, = [text for text in attention if "gateway substitution:" in text]
                legacy, _info = self.report(message)
                self.assertEqual(substitution, next(text for text in legacy if "gateway substitution:" in text))
                self.assertIn(key, substitution)
                self.assertIn(f"({wire})", substitution)
                self.assertNotIn("partial journal read", substitution)
        unknown = message.replace(wire, "fixture-unknown-wire")
        attention, _info = self.report(events.LogWindow((events.LogRecord(unknown, NOW, INSTANCE),), "bounded"))
        self.assertIn("gateway substitution: wire fixture-unknown-wire", "\n".join(attention))

    def test_nonpersisted_results_are_not_verified_and_raw_paths_never_display(self):
        for outcome in ("unverified", "unchanged", "skipped"):
            attention, info = self.report(window(MESSAGE.replace("result=persisted", f"result={outcome}")))
            self.assertEqual(attention, [])
            self.assertIn("not a verified save", info[-1])
        attention, info = self.report(window(MESSAGE + " auth=account.json path=/private/auth.json invalid_grant"))
        self.assertEqual(attention, [])
        self.assertEqual(len(info), 1)
        self.assertIn("coverage=incomplete", info[0])
        for forbidden in ("account.json", "/private", "invalid_grant", "credential_save.go"):
            self.assertNotIn(forbidden, repr((attention, info)))

    def test_reader_is_suppressed_by_injected_runtime_and_called_once_when_live(self):
        from charmer import endpoint
        from charmer.platform import file_log

        # The on-demand gateway's log source is its instance files; the
        # supervised unit's is its journal.
        with mock.patch.object(file_log, "read_recent", return_value=window()) as files, \
                mock.patch.object(service, "read_gateway_journal", return_value=window()) as read:
            self.assertIsNone(gateway_facts._read_gateway_journal(self.runtime))
            files.assert_not_called()
            self.runtime.health_get = self.runtime.served_models_callback = None
            info = []
            doctor._doctor_journal_report(self.runtime, info_lines=info)
            files.assert_called_once()
            self.assertEqual(files.call_args.kwargs["max_bytes"], gateway_facts._JOURNAL_MAX_BYTES)
            read.assert_not_called()
            self.assertTrue(any("persisted provider=claude" in text for text in info))
            port = endpoint.port_of(self.runtime.catalog.docs["gateway"]["gateway"]["base_url"])
            endpoint.write_config(self.runtime.home, endpoint.EndpointConfig(port=port, backend=endpoint.SYSTEMD))
            with mock.patch.object(endpoint.sys, "platform", "linux"):
                gateway_facts._read_gateway_journal(self.runtime)
            read.assert_called_once_with(max_bytes=gateway_facts._JOURNAL_MAX_BYTES, since="-24h",
                                         unit=endpoint.DEFAULT_UNIT)

    def test_readiness_failure_does_not_hide_the_persistence_hold(self):
        failed = MESSAGE.replace("result=persisted", "result=failed")
        self.runtime.doctor_callback = None
        self.runtime.health_get = mock.Mock(side_effect=launch.LaunchError("fixture unavailable"))
        with mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=window(failed)) as read:
            problems, info, attention = doctor._collect_doctor_reports(self.runtime)
        self.assertTrue(problems)
        self.assertTrue(any("keep the gateway running" in text for text in attention))
        read.assert_called_once_with(self.runtime)


class RecoveryHoldPolicyTests(unittest.TestCase):
    def batch(self, *messages):
        raw = b''.join(line(message, __REALTIME_TIMESTAMP=str(int(NOW.timestamp() * 1000000) + n),
                            __CURSOR=f's=fixture;i={n}') for n, message in enumerate(messages))
        return events.collect_credential_saves(linux_service.journal_records(raw), providers={'claude'})

    def failure(self, operation='update', stage='write'):
        return MESSAGE.replace('operation=refresh', 'operation=' + operation).replace('result=persisted', 'result=failed').replace('stage=none', 'stage=' + stage)

    def test_recovery_repair_requires_same_instance_identity_epoch(self):
        batch = self.batch(self.failure(), MESSAGE)
        self.assertFalse(events.recovery_hold(batch))
        failure, repaired = batch.events
        for other in (replace(repaired, gateway_instance='other'), replace(repaired, auth_index='fedcba9876543210'),
                      replace(repaired, epoch=3), replace(repaired, generation=1), replace(repaired, timestamp=failure.timestamp)):
            self.assertTrue(events.recovery_hold(events.CredentialSaves((failure, other), 'bounded')))
        self.assertTrue(events.recovery_hold(replace(batch, coverage='incomplete')))

    def test_recovery_unverified_changed_is_a_repair(self):
        self.assertFalse(events.recovery_hold(self.batch(self.failure(), MESSAGE.replace('persisted', 'unverified'))))
        self.assertTrue(events.recovery_hold(self.batch(self.failure(), MESSAGE.replace('persisted', 'unverified').replace('credentials_changed=true', 'credentials_changed=false'))))


WARN = "[2026-09-30 12:00:00] [--------] [warn ] [credential_save.go:214] "
FAILED = (MESSAGE.replace("result=persisted", "result=failed").replace("stage=none", "stage=write")
          .replace("errno=0", "errno=28").replace("category=none", "category=errno"))
# An access-log line whose request path a local client chose to look like a save.
FORGED = ('[2026-09-30 12:00:00] [a1b2c3d4] [warn ] [gin_logger.go:99] 404 |  1ms |  127.0.0.1 | GET '
          '"/x] [--------] [warn ] [credential_save.go:214] ' + FAILED + '"')


class EmitterDiscriminatorTests(unittest.TestCase):
    """Credential-save evidence comes only from patch 8's emitter: its
    envelope, then the token. One pattern serves --grep and Python."""

    def collect(self, *messages):
        records = tuple(events.LogRecord(message, NOW + timedelta(seconds=n), INSTANCE, f"c{n}")
                        for n, message in enumerate(messages))
        return events.collect_credential_saves(events.LogWindow(records, "bounded"), providers={"claude"})

    def test_the_pattern_names_the_emitter_the_patch_and_the_recipe_pin(self):
        self.assertTrue(events.SAVE_LINE_PATTERN.startswith("^"))
        self.assertTrue(events.SAVE_LINE_PATTERN.endswith(events.SAVE_TOKEN))
        self.assertIn(events.SAVE_EMITTER.replace(".", r"\."), events.SAVE_LINE_PATTERN)
        # The emitter is the file the recipe pins the event format in ...
        pins = {pin["name"]: pin for pin in json.loads(UPSTREAM_JSON.read_text())["source_pins"]}
        self.assertEqual(pins["credential-save-format"]["path"].rsplit("/", 1)[-1], events.SAVE_EMITTER)
        # ... and it logs the event itself, so logrus reports it as the caller.
        patch = (PATCH_DIR / "cli-proxy-api-credential-save-report.patch").read_text()
        emitter = patch.split(f"+++ b/sdk/cliproxy/auth/{events.SAVE_EMITTER}\n", 1)[1].split("\ndiff --git", 1)[0]
        self.assertIn("log.Warnf(credentialSaveEventFormat, args...)", emitter)
        self.assertIn("log.Infof(credentialSaveEventFormat, args...)", emitter)
        self.assertTrue(linux_service.hold_grep_pattern(events.SAVE_LINE_PATTERN, None))

    def test_the_emitters_lines_match_and_nothing_else_does(self):
        for message in (ENVELOPE + MESSAGE, WARN + FAILED, ENVELOPE + "credential_save_v2 operation=refresh",
                        "[2026-09-30 12:00:00] [a1b2c3d4] [info ] [credential_save.go:9] credential_save_v1 x"):
            with self.subTest(message=message[:60]):
                self.assertTrue(events.is_save_line(message))
        for message in (MESSAGE, FORGED, "prefix " + ENVELOPE + MESSAGE, ENVELOPE + "prefix " + MESSAGE,
                        ENVELOPE.replace("credential_save.go", "auth.go") + MESSAGE,
                        ENVELOPE.replace("[info ] ", "") + MESSAGE,
                        ENVELOPE.replace("2026-09-30", "2026-9-30") + MESSAGE,
                        "[2026-09-30 12:00:00] [credential_save.go:1] credential_save_v1 x"):
            with self.subTest(message=message[:60]):
                self.assertFalse(events.is_save_line(message))

    def test_bytes_and_their_replacement_decoding_agree(self):
        lines = [(ENVELOPE + MESSAGE).encode(), (WARN + FAILED).encode(), FORGED.encode(),
                 b"[2026-09-30 12:00:00] [\xff\xfe] [info ] [credential_save.go:217] credential_save_v1 x",
                 b"[2026-09-30 12:00:00] [--------] [\xc3] [credential_save.go:217] credential_save_v1 x",
                 b"[2026-09-30 12:00:00] [\xe2\x80\x93] [info ] [credential_save.go:217] credential_save_v1",
                 b"[2026-09-30 12:00:00] [--------] [info ] [credential_save.go:2\xff17] credential_save_v1",
                 b"\xef\xbb\xbf" + (ENVELOPE + MESSAGE).encode(), b"[\x00] " + MESSAGE.encode()]
        for raw in lines:
            with self.subTest(raw=raw[:50]):
                self.assertEqual(events.SAVE_LINE_BYTES.search(raw) is not None,
                                 events.is_save_line(raw.decode("utf-8", "replace")))

    def test_a_forged_token_in_an_access_log_line_neither_counts_nor_reduces_coverage(self):
        # Revert-failing: b4d8004f read any line with the token as a save and held.
        saves = self.collect(FORGED, ENVELOPE + MESSAGE)
        self.assertEqual(saves.coverage, "bounded")
        self.assertEqual([event.result for event in saves.events], ["persisted"])
        gathered = events.collect(events.LogWindow((events.LogRecord(FORGED, NOW, INSTANCE, "c1"),), "bounded"),
                                  providers={"claude"})
        self.assertEqual([(request.endpoint, request.status) for request in gathered.requests], [("other", 404)])
        self.assertEqual(gathered.credential_saves.coverage, "bounded")

    def test_an_unknown_save_line_version_holds(self):
        # Revert-failing: b4d8004f skipped a credential_save_v2 line.
        line_v2 = ENVELOPE + MESSAGE.replace("credential_save_v1", "credential_save_v2")
        saves = self.collect(line_v2)
        self.assertEqual((saves.events, saves.coverage), ((), "incomplete"))
        gathered = events.collect(events.LogWindow((events.LogRecord(line_v2, NOW, INSTANCE, "c1"),), "bounded"),
                                  providers={"claude"})
        self.assertEqual(gathered.requests, ())
        self.assertEqual(gathered.credential_saves.coverage, "incomplete")


class _JournalHold(unittest.TestCase):
    """evaluate_journal over injected seams (never the host journal)."""

    STARTED = NOW - timedelta(days=7)
    NONCE = "0123456789abcdef"

    def setUp(self):
        self.state_root = Path(tempfile.mkdtemp(prefix="cm-hold-"))
        os.chmod(self.state_root, 0o700)
        self.addCleanup(shutil.rmtree, self.state_root, True)

    def record(self, message, at, cursor):
        return events.LogRecord(message, at, INSTANCE, cursor)

    def marker(self):
        return self.record(service.start_marker_prefix(self.NONCE) + " (pid 1)", self.STARTED, "c-start")

    def saves(self, count, *, start=1):
        return [self.record(ENVELOPE + MESSAGE.replace("generation=2", f"generation={n}"),
                            self.STARTED + timedelta(seconds=n), f"c{n}") for n in range(start, start + count)]

    def evaluate(self, records, *, coverage="bounded", record=True, **probes):
        window = events.LogWindow(tuple(records), coverage)
        return gateway_hold.evaluate_journal(
            self.state_root, ("claude",), reader=lambda _since: window, now=lambda: NOW.isoformat(),
            running=(self.NONCE, self.STARTED), record=record, **probes)


class HoldEventCapTests(_JournalHold):
    def test_years_of_saves_and_a_repaired_failure_hold_nothing(self):
        # Revert-failing: b4d8004f kept 256 events and held "truncated".
        failure = self.record(ENVELOPE + FAILED.replace("generation=2", "generation=1"),
                              self.STARTED + timedelta(milliseconds=500), "c-failed")
        report = self.evaluate([self.marker(), failure, *self.saves(2000)])
        self.assertFalse(report.held, report.reasons)
        self.assertIn("2001 credential saves", report.evaluated)

    def test_more_saves_than_one_evaluation_keeps_hold(self):
        report = self.evaluate([self.marker(), *self.saves(gateway_hold.HOLD_MAX_EVENTS + 1)])
        self.assertTrue(report.held)
        self.assertTrue(report.coverage_only)
        self.assertIn("(coverage truncated); more credential saves than one evaluation keeps", report.reasons[-1])

    def test_the_doctor_collector_keeps_its_default(self):
        self.assertEqual(events.MAX_EVENTS, 256)
        batch = events.collect_credential_saves(events.LogWindow(tuple(self.saves(300)), "bounded"),
                                                providers={"claude"})
        self.assertEqual((len(batch.events), batch.coverage), (256, "truncated"))


class HoldRecordsIncompleteFailuresTests(_JournalHold):
    def test_a_failure_seen_in_an_incomplete_read_is_recorded_at_once(self):
        # Revert-failing: b4d8004f returned before recording anything.
        failed = self.record(ENVELOPE + FAILED, self.STARTED + timedelta(minutes=1), "c-failed")
        bogus = self.record(ENVELOPE + MESSAGE.replace("operation=refresh", "operation=bogus"),
                            self.STARTED + timedelta(minutes=2), "c-bogus")
        repair = self.record(ENVELOPE + MESSAGE.replace("generation=2", "generation=3"),
                             self.STARTED + timedelta(minutes=3), "c-repair")
        report = self.evaluate([self.marker(), failed, bogus])
        self.assertTrue(report.held)
        self.assertFalse(report.coverage_only)
        holds = gateway_hold.read_document(self.state_root)["holds"]
        self.assertEqual(len(holds), 1, holds)
        (held,) = holds
        self.assertIn("evidence", held)
        self.assertEqual(held["reason"], "a credential save failed and no later save repaired it "
                                         "(claude; service journal)")
        self.assertIn("a credential-save line did not parse", report.reasons[-1])
        # Still incomplete, even with the repair: nothing resolves.
        self.assertTrue(self.evaluate([self.marker(), failed, bogus, repair]).held)
        self.assertEqual(len(gateway_hold.read_document(self.state_root)["holds"]), 1)
        # A complete read that shows the repair resolves it.
        self.assertFalse(self.evaluate([self.marker(), failed, repair], drops=lambda _since: ()).held)
        self.assertEqual(gateway_hold.read_document(self.state_root)["holds"], [])

    def test_without_recording_the_document_keeps_its_bytes(self):
        failed = self.record(ENVELOPE + FAILED, self.STARTED + timedelta(minutes=1), "c-failed")
        report = self.evaluate([self.marker(), failed], coverage="truncated", record=False)
        self.assertTrue(report.held)
        self.assertFalse(gateway_hold.hold_path(self.state_root).exists())


class HoldJournalReaderTests(unittest.TestCase):
    """The hold's journal read, through the popen seam (synthetic programs)."""

    MARKER = "charmer-proxy: gateway instance 0123456789abcdef starting"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-hold-reader-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.spawned = []

    def emit(self, data: bytes = b"", *, code: int = 0, stderr: bytes = b"", sleep: float = 0) -> str:
        path = self.tmp / f"out-{len(list(self.tmp.iterdir()))}"
        path.write_bytes(data)
        return (f"import os, shutil, sys, time; sys.stderr.buffer.write({stderr!r}); "
                f"shutil.copyfileobj(open({str(path)!r}, 'rb'), sys.stdout.buffer); sys.stdout.flush(); "
                f"time.sleep({sleep}); sys.exit({code})")

    def read(self, *programs, marker=MARKER, max_bytes=1 << 20, timeout=10.0):
        calls = []

        def spawn(argv, **kwargs):
            calls.append((list(argv), kwargs))
            process = subprocess.Popen([sys.executable, "-c", programs[min(len(calls), len(programs)) - 1]],
                                       **kwargs)
            self.spawned.append(process)
            return process

        window = linux_service.read_hold_journal(unit="fixture-unit", since="@1000",
                                                 evidence=events.SAVE_LINE_PATTERN, marker=marker,
                                                 max_bytes=max_bytes, timeout=timeout, popen=spawn)
        return window, calls

    def jline(self, message, n, **fields):
        item = {"MESSAGE": message, "__REALTIME_TIMESTAMP": str(int(NOW.timestamp() * 1_000_000) + n),
                "_BOOT_ID": "a" * 32, "_SYSTEMD_INVOCATION_ID": "b" * 32, "__CURSOR": f"s=fixture;i={n}"}
        item.update(fields)
        return json.dumps(item).encode() + b"\n"

    def newest_first(self, *lines):
        return b"".join(reversed(lines))

    def sample(self):
        return self.newest_first(self.jline(self.MARKER + " (pid 1)", 1), self.jline("ordinary line", 2),
                                 self.jline(ENVELOPE + MESSAGE, 3), self.jline(FORGED, 4))

    def test_pass_one_filters_with_the_one_discriminator(self):
        window, calls = self.read(self.emit(self.sample()))
        (argv, kwargs), = calls
        self.assertEqual(argv, [
            "journalctl", "--user", "-u", "fixture-unit", "--since", "@1000", "--no-pager", "--reverse", "--all",
            "-o", "json", "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP,_BOOT_ID,_SYSTEMD_INVOCATION_ID",
            "--case-sensitive=yes", f"--grep={events.SAVE_LINE_PATTERN}|^{self.MARKER}"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(window.coverage, "bounded")
        # The Python predicate applies too: only candidates, chronological.
        self.assertEqual([record.message for record in window.records],
                         [self.MARKER + " (pid 1)", ENVELOPE + MESSAGE])
        self.assertEqual(window.records[1].gateway_instance, INSTANCE)

    def test_an_unsafe_marker_reads_once_without_grep(self):
        data = self.newest_first(self.jline("x(y starts here", 1), self.jline("ordinary", 2),
                                 self.jline(ENVELOPE + MESSAGE, 3))
        window, calls = self.read(self.emit(data), marker="x(y")
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(arg.startswith("--grep") for arg in calls[0][0]))
        self.assertEqual([record.message for record in window.records], ["x(y starts here", ENVELOPE + MESSAGE])
        self.assertEqual(linux_service.hold_grep_pattern(events.SAVE_LINE_PATTERN, None), events.SAVE_LINE_PATTERN)
        _window, calls = self.read(self.emit(data), marker=None)
        self.assertIn(f"--grep={events.SAVE_LINE_PATTERN}", calls[0][0])

    def test_a_failed_first_pass_reads_again_in_python(self):
        expected, _calls = self.read(self.emit(self.sample()))
        for label, first in (("no match", self.emit(code=1)),
                             ("no PCRE2", self.emit(code=1, stderr=b"PCRE2 support is not compiled in.\n")),
                             ("partial then an error", self.emit(self.sample(), code=1))):
            with self.subTest(label):
                window, calls = self.read(first, self.emit(self.sample()))
                self.assertEqual(len(calls), 2)
                self.assertTrue(any(arg.startswith("--grep=") for arg in calls[0][0]))
                self.assertFalse(any(arg.startswith("--grep") or arg.startswith("--case") for arg in calls[1][0]))
                self.assertEqual(window, expected)

    def test_a_failed_second_pass_or_no_journalctl_is_unavailable(self):
        window, calls = self.read(self.emit(code=1), self.emit(self.sample(), code=1))
        self.assertEqual((window.coverage, window.records, len(calls)), ("unavailable", (), 2))
        attempts = []

        def missing(argv, **_kwargs):
            attempts.append(argv)
            raise FileNotFoundError("journalctl")

        window = linux_service.read_hold_journal(unit="u", since="@1", evidence=events.SAVE_LINE_PATTERN,
                                                 marker=None, max_bytes=1024, popen=missing)
        self.assertEqual(window.coverage, "unavailable")
        self.assertEqual(len(attempts), 2)

    def test_a_first_pass_timeout_holds_and_reaps_only_the_reader(self):
        window, calls = self.read(self.emit(self.jline(ENVELOPE + MESSAGE, 1), sleep=30), timeout=0.5)
        self.assertEqual((window.coverage, window.timed_out, len(calls)), ("incomplete", True, 1))
        self.assertEqual(len(window.records), 1)
        self.assertIsNotNone(self.spawned[0].returncode)

    def test_the_kept_bytes_bound_only_the_evidence(self):
        ordinary = b"".join(self.jline(f"[2026-09-30 12:00:00] [a1b2c3d4] [info ] [gin_logger.go:92] 200 | {n}",
                                       n + 10) for n in range(24000))
        self.assertGreater(len(ordinary), 3 << 20)
        saves = [self.jline(ENVELOPE + MESSAGE.replace("generation=2", f"generation={n}"), 100000 + n)
                 for n in range(3)]
        data = saves[2] + ordinary[: len(ordinary) // 2].rsplit(b"\n", 1)[0] + b"\n" + saves[1] \
            + ordinary[len(ordinary[: len(ordinary) // 2].rsplit(b"\n", 1)[0]) + 1:] + saves[0]
        window, calls = self.read(self.emit(code=1), self.emit(data), max_bytes=1 << 20)
        self.assertEqual((window.coverage, len(window.records), len(calls)), ("bounded", 3, 2))
        program = self.emit(data)
        whole = linux_service.read_gateway_journal(
            unit="u", max_bytes=1 << 20,
            popen=lambda _argv, **kw: subprocess.Popen([sys.executable, "-c", program], **kw))
        self.assertEqual(whole.coverage, "truncated")

    def test_kept_caps_keep_the_newest_and_stop_the_reader(self):
        lines = [self.jline(ENVELOPE + MESSAGE.replace("generation=2", f"generation={n}"), n) for n in range(5)]
        data = self.newest_first(*lines)
        with mock.patch.object(observation, "LOG_MAX_RECORDS", 3):
            window, _calls = self.read(self.emit(data, sleep=30))
        self.assertEqual(window.coverage, "truncated")
        self.assertFalse(window.timed_out)
        self.assertEqual([record.source_cursor for record in window.records],
                         ["s=fixture;i=2", "s=fixture;i=3", "s=fixture;i=4"])
        self.assertIsNotNone(self.spawned[-1].returncode)  # killed and reaped, long before its sleep ends
        window, _calls = self.read(self.emit(data, sleep=30), max_bytes=len(lines[4]) + len(lines[3]))
        self.assertEqual(window.coverage, "truncated")
        self.assertEqual([record.source_cursor for record in window.records], ["s=fixture;i=3", "s=fixture;i=4"])

    def test_record_shapes(self):
        save = ENVELOPE + MESSAGE
        kept = self.jline(list(save.encode() + b" \xff"), 1)
        cases = {
            "byte array with the evidence": ([kept], "bounded", 1),
            "byte array without it": ([self.jline(list(b"ordinary \xff"), 1)], "bounded", 0),
            "no MESSAGE": ([json.dumps({"__REALTIME_TIMESTAMP": "1"}).encode() + b"\n"], "bounded", 0),
            "null MESSAGE": ([self.jline(None, 1)], "incomplete", 0),
            "list of strings": ([self.jline(["a", "b"], 1)], "incomplete", 0),
            "invalid JSON": ([b"{not json\n"], "incomplete", 0),
            "not an object": ([b"[1, 2]\n"], "incomplete", 0),
            "a line over 1 MiB": ([self.jline(save + " " + "x" * (1 << 20), 1)], "incomplete", 0),
            "no final newline": ([self.jline(save, 1)[:-1]], "incomplete", 0),
        }
        for label, (lines, coverage, count) in cases.items():
            with self.subTest(label):
                window, _calls = self.read(self.emit(b"".join(lines)))
                self.assertEqual((window.coverage, len(window.records)), (coverage, count))
        window, _calls = self.read(self.emit(kept))
        self.assertEqual(window.records[0].message, save + " �")


class JournalProbeTests(unittest.TestCase):
    """The reach-back probe and journald's drop notices (runner seam)."""

    AT = datetime(2026, 10, 1, 12, 0, 0, 500000, tzinfo=timezone.utc)

    def runner(self, *answers):
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            answer = answers[min(len(calls), len(answers)) - 1]
            if isinstance(answer, BaseException):
                raise answer
            code, out = answer
            return subprocess.CompletedProcess(argv, code, out, b"")
        return run, calls

    def stamp(self, moment):
        return json.dumps({"__REALTIME_TIMESTAMP": str(int(moment.timestamp() * 1_000_000))}).encode() + b"\n"

    def test_reaches(self):
        run, calls = self.runner((0, self.stamp(self.AT - timedelta(hours=1))))
        self.assertIs(linux_service.journal_reaches(unit="charmer-gateway", at=self.AT, runner=run), True)
        (argv, kwargs), = calls
        self.assertEqual(argv, ["journalctl", "--user", "-u", "charmer-gateway", "--until",
                                f"@{int(self.AT.timestamp())}", "-n", "1", "--no-pager", "-o", "json",
                                "--output-fields=__REALTIME_TIMESTAMP"])
        self.assertEqual((kwargs["stdout"], kwargs["stderr"], kwargs["stdin"]),
                         (subprocess.PIPE, subprocess.DEVNULL, subprocess.DEVNULL))
        cases = {
            "at the instant": ((0, self.stamp(self.AT)), True),
            "nothing that old": ((0, b""), False),
            "a failing journalctl": ((1, b""), None),
            "a timeout": (subprocess.TimeoutExpired("journalctl", 10), None),
            "no journalctl": (FileNotFoundError("journalctl"), None),
            "two lines": ((0, self.stamp(self.AT) * 2), None),
            "a non-digit time": ((0, b'{"__REALTIME_TIMESTAMP": "soon"}\n'), None),
            "a later time": ((0, self.stamp(self.AT + timedelta(seconds=1))), None),
            "not JSON": ((0, b"x\n"), None),
        }
        for label, (answer, expected) in cases.items():
            with self.subTest(label):
                run, _calls = self.runner(answer)
                self.assertIs(linux_service.journal_reaches(unit="u", at=self.AT, runner=run), expected)

    def notice(self, moment, dropped, user_unit=None):
        item = {"__REALTIME_TIMESTAMP": str(int(moment.timestamp() * 1_000_000))}
        if dropped is not None:
            item["N_DROPPED"] = dropped
        if user_unit is not None:
            item["OBJECT_SYSTEMD_USER_UNIT"] = user_unit
        return json.dumps(item).encode() + b"\n"

    def test_drops(self):
        probe = (0, self.stamp(self.AT))
        # Revert-failing: a notice naming another user unit stood for gateway lines.
        run, calls = self.runner(probe, (0, self.notice(self.AT, "5", "charmer-gateway.service")
                                         + self.notice(self.AT, None)
                                         + self.notice(self.AT, "9", "app-Alacritty@0123.service")))
        self.assertEqual(linux_service.journal_drops(since=self.AT, uid=1234, unit="charmer-gateway", runner=run),
                         ((self.AT, 5), (self.AT, None)))
        self.assertEqual(calls[0][0], ["journalctl", "--system", "-n", "1", "--no-pager", "-o", "json",
                                       "--output-fields=__REALTIME_TIMESTAMP"])
        self.assertEqual(calls[1][0], [
            "journalctl", "--system", "MESSAGE_ID=a596d6fe7bfa4994828e72309e95d61e", "_UID=0", "OBJECT_UID=1234",
            "OBJECT_SYSTEMD_UNIT=user@1234.service", "--since", f"@{int(self.AT.timestamp())}", "-n", "256",
            "--no-pager", "-o", "json", "--output-fields=__REALTIME_TIMESTAMP,N_DROPPED,OBJECT_SYSTEMD_USER_UNIT"])
        cases = {
            "an unreadable system journal": ((0, b""), None, None),
            "a failing probe": ((1, b""), None, None),
            "no notices": (probe, (0, b""), ()),
            "a failing query": (probe, (1, b""), None),
            "an unparseable notice": (probe, (0, b"x\n"), None),
            "a non-digit time": (probe, (0, b'{"__REALTIME_TIMESTAMP": "x"}\n'), None),
            "a non-digit count": (probe, (0, self.notice(self.AT, "many")), ((self.AT, None),)),
        }
        for label, (first, second, expected) in cases.items():
            with self.subTest(label):
                run, calls = self.runner(first, *(() if second is None else (second,)))
                self.assertEqual(linux_service.journal_drops(since=self.AT, uid=1234, unit="charmer-gateway",
                                                             runner=run), expected)
                if second is None:
                    self.assertEqual(len(calls), 1)

    def test_the_facade_passes_the_users_id(self):
        with mock.patch.object(linux_service, "journal_drops", return_value=()) as drops, \
                mock.patch.object(linux_service, "journal_reaches", return_value=True) as reaches, \
                mock.patch.object(linux_service, "read_hold_journal", return_value=events.LogWindow()) as read:
            service.journal_drops(since=self.AT, unit="u")
            service.journal_reaches(unit="u", at=self.AT)
            service.read_hold_journal(unit="u", since="@1", evidence="e", marker=None, max_bytes=1)
        drops.assert_called_once_with(since=self.AT, uid=os.getuid(), unit="u")
        reaches.assert_called_once_with(unit="u", at=self.AT)
        read.assert_called_once_with(unit="u", since="@1", evidence="e", marker=None, max_bytes=1)
