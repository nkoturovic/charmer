"""Pre-public record conversion is distinct from a public release rollback."""

from __future__ import annotations

import unittest

from charmer.cli import parser
from _layout import DOCS_DIR


def _flat(text: str) -> str:
    return " ".join(text.replace("`", "").split())


class PrePublicCommandHelpTests(unittest.TestCase):
    def test_help_names_the_prepublic_formats_and_the_release_rollback(self):
        root = parser.build_parser()
        for text in (root.format_help(), parser._SUMMARIES["migrate"]):
            with self.subTest(command="migrate"):
                flat = _flat(text)
                self.assertIn("pre-public record formats v1–3 (written before charmer 1.0.0)", flat)
                self.assertIn("byte-exact backups for restore-2x", flat)
        for text in (root.format_help(), parser._SUMMARIES["restore-2x"]):
            with self.subTest(command="restore-2x"):
                flat = _flat(text)
                self.assertIn("pre-public", flat)
                self.assertIn("not a release rollback", flat)
                self.assertIn("charmer update --rollback", flat)

    def test_docs_explain_prepublic_backups_without_a_retirement_promise(self):
        for name in ("reference/cli.md", "guides/legacy-migration.md"):
            with self.subTest(page=name):
                flat = _flat((DOCS_DIR / name).read_text())
                self.assertIn("pre-public record formats v1–3 (written before charmer 1.0.0)", flat)
                self.assertIn("byte-exact backups", flat)
                self.assertIn("not a release rollback", flat)
                self.assertIn("charmer update --rollback", flat)
        rolling_back = (DOCS_DIR / "update.md").read_text().split("## Rolling back", 1)[1].split("\n## ", 1)[0]
        self.assertIn("restore-2x", rolling_back)
        self.assertIn("pre-public", rolling_back)
        self.assertIn("unrelated to release rollback", _flat(rolling_back))
