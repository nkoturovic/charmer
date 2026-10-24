"""Parser-limit failures remain unreadable session records, never tracebacks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from charmer import sessions, state, strict_json
from charmer.cli import session_facts
from _catalog import FIXTURE_ROOT


class SessionJSONTests(unittest.TestCase):
    def test_parser_limits_are_reported_by_load_resolve_and_inventory(self):
        managed_id = "11111111-1111-4111-8111-111111111111"
        schema = strict_json.load(FIXTURE_ROOT / "schemas/session.schema.json")
        for payload, reason in (
            (b'{"integer":' + b"9" * 5000 + b"}", "JSON exceeds parser limits"),
            (b"[" * 200_000 + b"0" + b"]" * 200_000, "nesting too deep"),
        ):
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as tmp:
                store = sessions.SessionStore(Path(tmp), schema)
                path = store.sessions_dir / f"{managed_id}.json"
                state.atomic_write(path, payload)
                for operation in (store.load, store.resolve):
                    with self.assertRaisesRegex(sessions.SessionError, "corrupt session record"):
                        operation(managed_id)
                records, problems, ids = session_facts._read_session_records(
                    SimpleNamespace(session_store=store)
                )
                self.assertEqual(records, [])
                self.assertEqual(ids, {managed_id})
                self.assertEqual(len(problems), 1)
                self.assertIn("is unreadable", problems[0])
                self.assertIn(reason, problems[0])
                self.assertEqual(path.read_bytes(), payload)
