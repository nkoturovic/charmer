"""Spawn-mod advisories inspect bounded fixture text, never execute a mod."""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from charmer import tui
from charmer.cli import doctor, entry
from charmer.cli.screens import findings
from _v4 import V4Case
from test_tui import DOWN, ENTER, ESC, FakeWindow


class SpawnModAdvisoryTests(V4Case):
    def setUp(self) -> None:
        # Keep fixture path assertions canonical even on platforms with
        # a linked /tmp or /var.
        with mock.patch.object(tempfile, "tempdir", str(Path(tempfile.gettempdir()).resolve())):
            super().setUp()

    def plugin(self, root=None, *, text="export const register = on => on('agent.spawn', () => {});", module="./register.mjs"):
        root = root or self.runtime.home / ".claude" / "plugins" / "store" / "fixture-mod" / "1.0"
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / "hooks").mkdir(exist_ok=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "fixture-mod", "version": "1.0"}))
        (root / "hooks" / "hooks.json").write_text(json.dumps({"modules": [module]}))
        target = root / "hooks" / module
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return root, target

    def report(self):
        problems, info, attention = doctor._collect_doctor_reports(self.runtime)
        rows = [line for line in info if getattr(line, "code", None) == "spawn-mod-advisory"]
        return problems, info, attention, rows

    def test_spawn_hook_is_info_and_findings_shows_the_identical_row(self) -> None:
        root, _module = self.plugin(text="throw new Error('must never run');\non('agent.spawn', () => {});")
        problems, info, attention, rows = self.report()
        self.assertEqual(len(rows), 1)
        self.assertIn("declares an agent.spawn hook", rows[0])
        self.assertIn("may redirect spawns within the lineup fence; the exact per-agent binding is not guaranteed", rows[0])
        self.assertIn("other hook kinds are outside this check", rows[0])
        self.assertIn("not exhaustive", rows[0])
        self.assertIn("not proof it is enabled or active", rows[0])
        self.assertNotIn(rows[0], problems + attention)
        screen = findings.FindingsScreen(self.runtime, palette=tui.MONO_PALETTE,
                                         tty_in=io.StringIO(), tty_out=io.StringIO(),
                                         report=lambda: ([], info, []))
        win = FakeWindow([ENTER, DOWN, ENTER, ESC, ESC], height=40, width=120)
        with mock.patch.object(tui, "TextView") as view:
            screen.run(win)
        self.assertEqual(screen.presentation.status, "ready")
        self.assertIn(str(rows[0]), view.call_args.args[1])
        out = io.StringIO()
        self.assertEqual(entry.main(["doctor", "--json"], runtime=self.runtime,
                                    output_stream=out, interactive=False), 0)
        diagnostic, = [row for row in json.loads(out.getvalue())["diagnostics"] if row["code"] == "spawn-mod-advisory"]
        self.assertEqual(diagnostic["severity"], "info")
        self.assertNotIn(str(root), out.getvalue())

    def test_a_module_without_the_literal_has_no_spawn_advisory(self) -> None:
        self.plugin(text="export const register = on => on('ui.render', () => {});")
        self.assertEqual(self.report()[3], [])

    def test_unreadable_module_gets_an_incomplete_info_row(self) -> None:
        _root, module = self.plugin()
        real = os.open
        def opened(path, *args, **kwargs):
            if Path(path) == module:
                raise PermissionError("fixture denied")
            return real(path, *args, **kwargs)
        with mock.patch("os.open", side_effect=opened):
            rows = self.report()[3]
        self.assertEqual(len(rows), 1)
        self.assertIn("hook module present, not inspected", rows[0])
        self.assertNotIn("declares an agent.spawn hook", rows[0])

    def test_scan_covers_declared_locations_and_does_not_follow_imports(self) -> None:
        from charmer import spawn_mods
        claude = self.runtime.home / ".claude"
        roots = [claude / "dev-mods" / "session" / "example", claude / "skills" / "example",
                 Path(self.runtime.cwd) / ".claude" / "skills" / "example",
                 claude / "plugins" / "synced" / "example", self.root / "env-plugin", self.root / "flag-plugin"]
        for root in roots:
            self.plugin(root)
        (claude / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_PLUGIN_DIRS": str(roots[-2])}}))
        result = spawn_mods.scan(self.runtime.home, self.runtime.cwd, self.runtime.environ, plugin_dirs=(roots[-1],))
        self.assertEqual({row.folder for row in result.mods if row.spawn}, set(roots))
        self.assertEqual(result.incomplete, ())
        self.assertIn("folder marketplaces", result.coverage)
        # Imported code is not read: only the declared module's literal matters.
        root, module = self.plugin(self.root / "import-only", text="import './other.mjs';")
        (module.parent / "other.mjs").write_text("on('agent.spawn', () => {});")
        isolated = spawn_mods.scan(self.root / "empty-home", self.root / "empty-project", {}, plugin_dirs=(root,))
        self.assertEqual(isolated.mods, (spawn_mods.Mod(root, False, True),))

    def test_coverage_excludes_client_command_line_plugin_directories(self) -> None:
        from charmer import spawn_mods
        scanned, not_scanned = spawn_mods.COVERAGE.split("Not scanned:")
        self.assertNotIn("command line", scanned)
        self.assertIn("plugin directories given on a client's command line (managed sessions refuse that flag)",
                      not_scanned)

    def test_session_dev_mod_without_spawn_is_found_at_the_last_allowed_depth(self) -> None:
        from charmer import spawn_mods
        home = self.root / "dev-mod-home"
        root, _module = self.plugin(home / ".claude" / "dev-mods" / "sess" / "example",
                                    module="./register.ts", text="export const register = () => {};")
        result = spawn_mods.scan(home, self.root / "empty-project", {})
        self.assertEqual(result.mods, (spawn_mods.Mod(root, False, True),))
        self.assertEqual(result.incomplete, ())

    def test_invalid_plugin_directory_settings_are_incomplete_not_exceptions(self) -> None:
        from charmer import spawn_mods
        home = self.root / "invalid-setting-home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        invalid = "/tmp/a\x00b"
        for source in ("settings", "environment"):
            with self.subTest(source=source):
                (claude / "settings.json").write_text(json.dumps(
                    {"env": {"CLAUDE_CODE_PLUGIN_DIRS": invalid}} if source == "settings" else {}))
                environ = {"CLAUDE_CODE_PLUGIN_DIRS": invalid} if source == "environment" else {}
                result = spawn_mods.scan(home, self.root / "empty-project", environ)
                self.assertEqual(result.mods, ())
                self.assertEqual(result.incomplete, ("invalid plugin directory setting",))

    def test_a_home_and_project_through_a_linked_parent_have_the_same_coverage(self) -> None:
        from charmer import spawn_mods
        parent = self.root / "real-parent"
        home, cwd = parent / "home", parent / "project"
        self.plugin(home / ".claude" / "skills" / "example")
        self.plugin(cwd / ".claude" / "skills" / "example")
        linked = self.root / "linked-parent"
        linked.symlink_to(parent, target_is_directory=True)
        real = spawn_mods.scan(home, cwd, {})
        result = spawn_mods.scan(linked / "home", linked / "project", {})
        self.assertEqual(len(real.mods), 2)
        self.assertEqual(real.incomplete, ())
        self.assertEqual(result.mods, real.mods)
        self.assertEqual(result.incomplete, ())

    def test_a_linked_settings_directory_keeps_its_projects_excluded(self) -> None:
        from charmer import spawn_mods
        home = self.root / "linked-settings-home"
        home.mkdir()
        claude = self.root / "real-settings"
        root, _module = self.plugin(claude / "skills" / "example")
        (home / ".claude").symlink_to(claude, target_is_directory=True)
        result = spawn_mods.scan(home, self.root / "empty-project", {})
        self.assertEqual(result.mods, (spawn_mods.Mod(root, True, True),))
        self.assertEqual(result.incomplete, ())
        # The excluded directory need not exist; no transcript is made or read.
        for projects in (claude / "projects", home / ".claude" / "projects"):
            with self.subTest(path=projects):
                result = spawn_mods.scan(home, self.root / "empty-project", {}, plugin_dirs=(projects,))
                self.assertIn("transcript location excluded", result.incomplete)

    def test_a_linked_skill_inside_a_scan_root_is_still_not_inspected(self) -> None:
        from charmer import spawn_mods
        root, _module = self.plugin(self.root / "outside-skill")
        skills = self.runtime.home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "linked-skill").symlink_to(root, target_is_directory=True)
        result = spawn_mods.scan(self.runtime.home, self.runtime.cwd, self.runtime.environ)
        self.assertEqual(result.mods, ())
        self.assertEqual(result.incomplete, ("symlinked folder not inspected",))

    def test_oversized_nontext_and_escaped_modules_are_not_inspected(self) -> None:
        from charmer import spawn_mods
        root, module = self.plugin()
        for content in (b"x" * (spawn_mods.MAX_FILE_BYTES + 1), b"\xff\x00agent.spawn"):
            module.write_bytes(content)
            rows = self.report()[3]
            self.assertEqual(len(rows), 1)
            self.assertIn("hook module present, not inspected", rows[0])
        outside = self.root / "outside.mjs"
        outside.write_text("on('agent.spawn', () => {});")
        (root / "hooks" / "hooks.json").write_text(json.dumps({"modules": [str(outside)]}))
        rows = self.report()[3]
        self.assertIn("hook module present, not inspected", rows[0])
        self.assertNotIn("declares an agent.spawn hook", rows[0])

    def test_symlinks_and_transcript_roots_are_not_followed(self) -> None:
        from charmer import spawn_mods
        root, module = self.plugin()
        outside = self.root / "outside.mjs"
        outside.write_text("on('agent.spawn', () => {});")
        module.unlink()
        module.symlink_to(outside)
        rows = self.report()[3]
        self.assertIn("hook module present, not inspected", rows[0])
        self.assertNotIn("declares an agent.spawn hook", rows[0])
        # The denied path need not exist, and no transcript is read or listed.
        with mock.patch.object(os, "scandir", side_effect=AssertionError("transcript directory listed")):
            result = spawn_mods.scan(self.root / "absent-home", self.root / "absent-project", {},
                                     plugin_dirs=(self.root / "absent-home" / ".claude" / "projects",))
        self.assertTrue(result.incomplete)

    def test_folder_and_total_read_bounds_are_reported_not_claimed_exhaustive(self) -> None:
        from charmer import spawn_mods
        self.plugin()
        with mock.patch.object(spawn_mods, "MAX_ENTRIES", 1):
            result = spawn_mods.scan(self.runtime.home, self.runtime.cwd, self.runtime.environ)
        self.assertTrue(result.incomplete)
        with mock.patch.object(spawn_mods, "MAX_TOTAL_BYTES", 1):
            result = spawn_mods.scan(self.runtime.home, self.runtime.cwd, self.runtime.environ)
        self.assertTrue(result.incomplete)
