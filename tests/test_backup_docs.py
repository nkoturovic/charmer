"""Cold-backup documentation, not a production backup/restore acceptance.

Run the guide's generic file-copy examples only, with synthetic credentials,
private tool homes and no service/client/provider invocation. Health and signed
installation verification belong to the installed-candidate recovery journey.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from _layout import DOCS_DIR, RESOURCES_ROOT, SERVICE_SPEC, pyproject
from charmer import installs, paths, pin, proxy
from charmer.platform import linux_service


GUIDE = DOCS_DIR / "guides" / "backup.md"


def example(name: str) -> str:
    match = re.search(rf"<!-- example: {re.escape(name)} -->\n```bash\n(.*?)\n```", GUIDE.read_text(), re.S)
    if match is None:
        raise AssertionError(f"missing documented example {name}")
    return match[1]


class BackupGuideTests(unittest.TestCase):
    def test_exclusions_precede_the_first_command(self) -> None:
        intro = GUIDE.read_text().split("## 1.", 1)[0]
        for text in ("charmer-owned files only", "user-owned", "~/.claude/projects/",
                     "alternative Claude Code", "not conversation recovery",
                     "does not restore the transcripts"):
            self.assertIn(text, intro)
        self.assertNotIn("```", intro)

    def test_export_recovery_and_move_are_different_tasks(self) -> None:
        text = " ".join(GUIDE.read_text().split())
        for part in ("Export is not a cold backup", "update rollback is not disk-loss recovery",
                     "no automated backup command or TUI backup action",
                     "not a clone of live OAuth records", "fresh sign-in"):
            self.assertIn(part, text)
        for name, link in (("USAGE.md", "guides/backup.md"), ("privacy.md", "guides/backup.md"),
                           ("guides/move-machines.md", "backup.md")):
            with self.subTest(page=name):
                self.assertIn(f"]({link})", (DOCS_DIR / name).read_text())

    def test_backup_page_is_an_explicit_installed_document(self) -> None:
        documents = pyproject()["tool"]["setuptools"]["data-files"]["share/charmer/guides"]
        self.assertIn("docs/guides/backup.md", documents)

    def test_safe_guidance_covers_the_recovery_refusals(self) -> None:
        text = " ".join(GUIDE.read_text().split())
        # These are assertions about instructions, not simulated product verdicts.
        requirements = {
            "running sessions": ("all managed sessions", "background sessions", "Unknown liveness means"),
            "failed save": ("failed save", "confirmed durable save", "not merely clearing a flag"),
            "journald visibility": ("known journald restart", "unavailable journald observation",
                                    "clear cannot waive it", "not proof of complete journal history"),
            "incompatible state": ("never lower `state-version`", "uncertain compatibility means stop"),
            "destination": ("outside every source tree", "not a synced or publicly shared folder"),
            "OAuth clone": ("Never run a restored OAuth sign-in on two hosts at once", "invalid_grant"),
            "ownership": ("Never hand-edit or delete stale exec stamps", "failed-save evidence"),
            "modes": ("including executable bits", "Do not recursively chmod files to 0600",
                      "credential and record files remain 0600"),
            "acceptance boundary": ("not a production backup or full restore acceptance",
                                    "Ready** health report is not a signature or full restore test"),
        }
        for condition, parts in requirements.items():
            for part in parts:
                with self.subTest(condition=condition, text=part):
                    self.assertIn(part, text)
        self.assertNotRegex(text, r"\brm\s+-(?:rf|fr)\b|kill\s+-9|chmod\s+-R\s+600")

    def test_path_exceptions_are_explicit_and_match_packaged_gateway_paths(self) -> None:
        text = " ".join(GUIDE.read_text().split())
        gateway = json.loads((RESOURCES_ROOT / "catalog" / "gateway.json").read_text())["gateway"]
        spec = json.loads(SERVICE_SPEC.read_text())
        home = Path("/home/user")
        env = {"HOME": str(home), "XDG_DATA_HOME": "/somewhere/data"}
        self.assertEqual(home / gateway["token_file"], paths.gateway_config_dir(env) / "api-key")
        self.assertEqual(home / gateway["auth_dir"], paths.data_root(env) / "auth")
        self.assertEqual(paths.data_root(env), home / ".local/share/charmer")
        self.assertEqual(installs.bundle_root(env), paths.data_root(env) / "install")
        self.assertEqual(pin.owned_root(env), paths.data_root(env) / "claude")
        self.assertEqual(home / spec["state_root"], home / ".local/state/charmer")
        for fragment in ("`XDG_DATA_HOME` does not move", "`CHARMER_SECRET_ENV`", "`secret-file.json`",
                         "**as well as** an override", "custom state root", "wheel/virtual environment",
                         "not a backup of the store", "enable link", "does not follow links"):
            self.assertIn(fragment, text)


class BackupShellTests(unittest.TestCase):
    """Synthetic archive/encryption/mode/path proof; no doctor or gateway mocks."""

    def setUp(self) -> None:
        tools = {name: shutil.which(name) for name in ("bash", "tar", "realpath", "gpg", "gpgconf")}
        if not all(tools.values()):
            self.skipTest("BOUNDARY: backup examples need Bash, GNU tar, realpath and GnuPG 2")
        version = subprocess.run([tools["tar"], "--version"], capture_output=True, timeout=10)
        if b"GNU tar" not in version.stdout:
            self.skipTest("BOUNDARY: backup examples need GNU tar")
        self.tools = tools
        self.temp = tempfile.TemporaryDirectory(prefix="charmer-backup-docs-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        old_mask = os.umask(0o077)
        self.addCleanup(os.umask, old_mask)
        self.home = self.root / "home"
        self.scripts = self.root / "scripts"
        self.backups = self.root / "encrypted backups"
        self.restores = self.root / "restore staging"
        for directory in (self.home, self.scripts, self.backups, self.restores, self.root / "tmp"):
            directory.mkdir(mode=0o700)
        passphrase = self.scripts / "fixture-passphrase"
        passphrase.write_text("dummy fixture passphrase\n")
        # Only the non-interactive test adapter adds a fake passphrase FD.
        # The actual encryption/decryption is GnuPG, with the doc's fresh home.
        wrapper = self.scripts / "gpg"
        wrapper.write_text('#!/bin/sh\nexec "$FIXTURE_GPG" --batch --passphrase-fd 3 "$@" '
                           '3< "$FIXTURE_PASSPHRASE"\n')
        wrapper.chmod(0o700)
        self.env = {
            "HOME": str(self.home), "PATH": str(self.scripts) + os.pathsep + os.environ["PATH"],
            "LC_ALL": "C", "TMPDIR": str(self.root / "tmp"), "TAR": tools["tar"],
            "FIXTURE_GPG": tools["gpg"], "FIXTURE_PASSPHRASE": str(passphrase),
            "backup_parent": str(self.backups), "restore_parent": str(self.restores),
        }
        # No inherited GNUPGHOME, agent socket, keys, configuration or credentials.
        self.crypto_homes: list[Path] = []
        self.addCleanup(self.close_agents)
        self.script_number = 0

    def close_agents(self) -> None:
        homes = self.crypto_homes + list(self.backups.glob("charmer-cold.*/gnupg"))
        for home in set(homes):
            subprocess.run([self.tools["gpgconf"], "--homedir", str(home), "--kill", "gpg-agent"],
                           env=self.env, cwd=self.scripts, capture_output=True, timeout=10, check=True)

    def run_shell(self, text: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        self.script_number += 1
        script = self.scripts / f"example-{self.script_number}.sh"
        script.write_text("set -euo pipefail\numask 077\n" + text + "\n")
        return subprocess.run([self.tools["bash"], str(script)], env=env or self.env, cwd=self.scripts,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=45)

    def assert_ok(self, result: subprocess.CompletedProcess) -> None:
        # Captured diagnostics contain only this test's synthetic paths/data.
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"", "examples must not print listings or raw content")

    def write(self, path: Path, content: bytes, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(content)
        path.chmod(mode)

    def test_documented_path_selection_matches_the_root_constructors(self) -> None:
        cases = ({}, {"XDG_CONFIG_HOME": "relative", "XDG_STATE_HOME": "relative"},
                 {"XDG_CONFIG_HOME": str(self.root / "config space"),
                  "XDG_STATE_HOME": str(self.root / "state space"),
                  "XDG_DATA_HOME": str(self.root / "not managed data")})
        for extra in cases:
            env = {**self.env, **extra}
            result = self.run_shell(example("backup-paths") +
                                    '\nprintf "%s\\0" "$operator_config" "$session_state" '
                                    '"$gateway_config" "$data_root" "$unit_dir"', env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = result.stdout.decode().rstrip("\0").split("\0")
            expected = [paths.config_root(env), paths.state_root(env),
                        proxy.config_dir(self.home), paths.data_root(env), Path(linux_service.unit_dir(env))]
            self.assertEqual(actual, [str(path) for path in expected])

    def test_archive_refuses_a_destination_inside_a_source_even_through_a_link(self) -> None:
        source = self.home / "private source"
        source.mkdir(mode=0o700)
        nested = source / "backup"
        nested.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        for destination in (nested, alias):
            text = f"sources=({shlex.quote(str(source))})\n" + example("backup-archive")
            result = self.run_shell(text, env={**self.env, "backup_parent": str(destination)})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"backup destination is inside a source tree", result.stderr)
            self.assertEqual(list(nested.iterdir()), [])

    def test_archive_refuses_overbroad_and_symlinked_directory_sources(self) -> None:
        private = self.home / "private"
        private.mkdir(mode=0o700)
        link = self.home / "linked root"
        link.symlink_to(private, target_is_directory=True)
        claude = self.home / ".claude"
        claude.mkdir(mode=0o700)
        for source in (Path("/"), self.home, claude, link):
            text = f"sources=({shlex.quote(str(source))})\n" + example("backup-archive")
            result = self.run_shell(text)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(self.backups.iterdir()), [])

    def round_trip(self, *, xdg: bool) -> None:
        if xdg:
            self.env.update(XDG_CONFIG_HOME=str(self.root / "config space"),
                            XDG_STATE_HOME=str(self.root / "state space"),
                            XDG_DATA_HOME=str(self.root / "upstream data"))
        roots = list(dict.fromkeys((paths.gateway_config_dir(self.env), paths.config_root(self.env),
                                    paths.data_root(self.env), paths.state_root(self.env),
                                    self.home / ".local/state/charmer")))
        for root in roots:
            root.mkdir(mode=0o700, parents=True)
        self.write(roots[0] / "api-key", b"dummy-fixture-local-key\n")
        self.write(paths.config_root(self.env) / "settings.json", b"{}\n")
        self.write(paths.data_root(self.env) / "auth/fixture-account.json",
                   b'{"refresh_token":"dummy-fixture-refresh"}\n')
        program = paths.data_root(self.env) / "install/versions/fixture/bin/charmer"
        self.write(program, b"#!/bin/sh\nexit 0\n", 0o755)
        current = program.parents[3] / "current"
        current.symlink_to("versions/fixture", target_is_directory=True)
        helper = paths.state_root(self.env) / "bin/charmer-hook"
        self.write(helper, b"#!/bin/sh\nexit 0\n", 0o700)
        self.write(paths.state_root(self.env) / "gateway/persistence-hold.json", b'{"fixture":"kept"}\n')
        launchers = [self.home / ".local/bin" / name for name in ("charmer", "charmer-proxy")]
        for launcher in launchers:
            self.write(launcher, b"#!/bin/sh\nexit 0\n", 0o755)
        external = self.root / "external keys/provider-keys.env"
        self.write(external, b"EXAMPLE_API_KEY=dummy-fixture\n")
        unit = Path(linux_service.unit_dir(self.env)) / "fixture-gateway.service"
        self.write(unit, b"# fixture unit; never started\n")
        enabled = unit.parent / "default.target.wants" / unit.name
        enabled.parent.mkdir(mode=0o700)
        enabled.symlink_to("../" + unit.name)
        extras = [external, unit, enabled]
        selected = roots + launchers + extras
        before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                  for root in roots for path in root.rglob("*") if path.is_file() and not path.is_symlink()}
        for path in launchers + [external, unit]:
            before[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        script = example("backup-paths") + "\nsources+=(" + " ".join(shlex.quote(str(p)) for p in extras) + ")\n"
        self.assert_ok(self.run_shell(script + example("backup-archive")))
        work, = self.backups.glob("charmer-cold.*")
        archive = work / "charmer.tar.gpg"
        crypto_home = work / "gnupg"
        self.crypto_homes.append(crypto_home)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
        self.assertFalse((work / "charmer.tar").exists())
        self.env.update(archive=str(archive), crypto_home=str(crypto_home))
        self.assert_ok(self.run_shell(example("backup-unpack")))
        restored, = self.restores.glob("charmer-restore.*")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o700)
        # Inspect only the disposable extraction, never native transcripts.
        self.assertFalse(any(path.name == ".claude" for path in restored.rglob("*")))
        if xdg:
            self.assertFalse((restored / self.env["XDG_DATA_HOME"].lstrip("/")).exists())
        # Copy into absent original locations. Retain every pre-restore source.
        for destination in selected:
            destination.rename(destination.with_name(destination.name + ".before-restore"))
        for destination in selected:
            self.assert_ok(self.run_shell(example("backup-copy"), env={
                **self.env, "destination": str(destination), "restore_root": str(restored)}))
        for path, (data, mode) in before.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, str(path))
        for root in roots:
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for link in (current, enabled):
            self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(current), "versions/fixture")
        self.assertEqual(os.readlink(enabled), "../" + unit.name)
        # A second copy must refuse rather than merge/overwrite a destination.
        result = self.run_shell(example("backup-copy"), env={
            **self.env, "destination": str(roots[0]), "restore_root": str(restored)})
        self.assertNotEqual(result.returncode, 0)
        self.assert_ok(self.run_shell(example("backup-crypto-close")))

    def test_default_home_archive_encrypt_restore_round_trip(self) -> None:
        self.round_trip(xdg=False)

    def test_xdg_external_key_service_and_mode_round_trip(self) -> None:
        self.round_trip(xdg=True)


if __name__ == "__main__":
    unittest.main()
