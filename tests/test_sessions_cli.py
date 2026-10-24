"""Session command reports use complete metadata and truthful outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from charmer import sessions
from charmer.cli import entry
from test_cli import CLITestCase, FIXED_ID, OTHER_ID


class SessionInventoryTests(CLITestCase):
    def broken_record(self) -> Path:
        path = self.runtime.session_store.sessions_dir / f"{OTHER_ID}.json"
        path.write_bytes(b"not json\n")
        path.chmod(0o600)
        return path

    def test_json_refuses_an_incomplete_inventory(self) -> None:
        self.save_v4_session()
        broken = self.broken_record()
        for extra in ([], ["--here"], ["--ended"]):
            with self.subTest(extra=extra):
                code, out, err = self.run_cli_both(["sessions", "list", "--json", *extra], interactive=False)
                self.assertEqual(code, 1, out + err)
                self.assertEqual(json.loads(out), {"schema_version": 1, "list": "sessions",
                                                  "status": "unavailable", "error": entry.LIST_UNAVAILABLE})
                self.assertIn(f"session record {OTHER_ID} is unreadable", err)
                self.assertNotIn(OTHER_ID, out)
                self.assertEqual(broken.read_bytes(), b"not json\n")

    def test_human_listings_keep_readable_rows_but_fail(self) -> None:
        self.save_v4_session()
        self.broken_record()
        for argv in (["sessions", "list"], ["sessions", "list", "--here"], ["-r"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli_both(argv, interactive=False)
                self.assertEqual(code, 1, out + err)
                self.assertIn(FIXED_ID[:8], out)
                self.assertIn(f"session record {OTHER_ID} is unreadable", err)
                self.assertNotIn("(no recorded sessions)", out)

    def test_a_wholly_unreadable_inventory_is_not_reported_empty(self) -> None:
        self.broken_record()
        code, out, err = self.run_cli_both(["sessions", "list"], interactive=False)
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("(no recorded sessions)", out)
        self.assertIn(OTHER_ID, err)

    def test_an_unavailable_directory_fails_in_all_listing_forms(self) -> None:
        directory = self.runtime.session_store.sessions_dir
        iterdir = Path.iterdir

        def scan(path):
            if path == directory:
                raise PermissionError("fixture directory unavailable")
            return iterdir(path)

        for argv in (["sessions", "list", "--json"], ["sessions", "list"], ["-r"]):
            with self.subTest(argv=argv), mock.patch.object(Path, "iterdir", scan):
                code, out, err = self.run_cli_both(argv, interactive=False)
                self.assertEqual(code, 1, out + err)
                self.assertIn("session record directory is unavailable", err)
                if "--json" in argv:
                    self.assertEqual(json.loads(out)["status"], "unavailable")
                else:
                    self.assertNotIn("(no recorded sessions)", out)

    def test_a_readable_empty_inventory_still_succeeds(self) -> None:
        code, out, err = self.run_cli_both(["sessions", "list", "--json"], interactive=False)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["sessions"], [])
        code, out, err = self.run_cli_both(["sessions", "list"], interactive=False)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("(no recorded sessions)", out)
