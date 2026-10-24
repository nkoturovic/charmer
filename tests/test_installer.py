"""``packaging/install.sh`` against a local fake release directory.

Every run uses a temporary HOME, a stub ``uname`` (so the platform checks are
exercised on any host), ``--from-dir`` (no network) and a new session (no
controlling terminal, so nothing is ever asked). Signatures come from a
throwaway ``ssh-keygen`` key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from charmer import install_receipt, service
from _fake_release import SSH_KEYGEN, FakeRelease, make_key, needs_ssh_keygen, signers_line
from _layout import REPO_ROOT
from _release import INSTALL_SH, load_tool, requires_release_tree

UNAME = """#!/bin/sh
case $1 in
-s) echo "${FAKE_UNAME_S:-Linux}" ;;
-m) echo "${FAKE_UNAME_M:-x86_64}" ;;
-r) echo "${FAKE_UNAME_R:-6.1.0-generic}" ;;
*) echo "${FAKE_UNAME_S:-Linux}" ;;
esac
"""
# Every external command the installer may run for a --from-dir install
# (shell builtins and mocked platform commands aside). A new dependency
# must be added here on purpose.
INSTALLER_TOOLS = ("awk", "cat", "chmod", "cp", "cut", "getconf", "grep", "gzip", "id", "ldd", "ln", "ls",
                   "mkdir", "mktemp", "mv", "readlink", "rm", "sed", "sha256sum", "sort", "sync", "tail", "tar", "tr")
SHELLS = [("sh",)] + ([("busybox", "sh")] if shutil.which("busybox") else [])


@requires_release_tree
class InstallerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="charmer-installer-"))
        os.chmod(cls.base, 0o700)
        cls.fakebin = cls.base / "fakebin"
        cls.fakebin.mkdir()
        (cls.fakebin / "uname").write_text(UNAME)
        os.chmod(cls.fakebin / "uname", 0o755)
        cls.keys = cls.base / "keys"
        cls.keys.mkdir()
        if SSH_KEYGEN:
            cls.key = make_key(cls.keys, "release")
            cls.other_key = make_key(cls.keys, "other")
            cls.signers = cls.keys / "allowed_signers"
            cls.signers.write_text(signers_line(cls.key))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=self.base))
        self.home = self.tmp / "home dir"  # a space in HOME on purpose
        self.home.mkdir()
        self.root = self.home / ".local/share/charmer/install"
        self.bin = self.home / ".local/bin"
        self.state = self.home / ".local/state/charmer"

    def release(self, version: str, *, sign: bool = True, **kwargs) -> FakeRelease:
        release = FakeRelease(self.tmp / f"release-{version}", version, **kwargs)
        if sign:
            release.sign(self.key)
        return release

    def run_installer(self, *args: str, shell: tuple[str, ...] = ("sh",), script: Path = INSTALL_SH,
                      path: str | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        environ = {"HOME": str(self.home), "PATH": path or f"{self.fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   "LC_ALL": "C", "TMPDIR": str(self.tmp), "SHELL": "/bin/sh"}
        environ.update(env)
        return subprocess.run([*shell, str(script), *args], env=environ, cwd=self.tmp, capture_output=True, stdin=subprocess.DEVNULL,
                              text=True, timeout=120, start_new_session=True)

    def install(self, release: FakeRelease | Path, *extra: str, **kwargs) -> subprocess.CompletedProcess[str]:
        directory = release if isinstance(release, Path) else release.dir
        return self.run_installer("--from-dir", str(directory), "--allowed-signers", str(self.signers),
                                  "--no-setup", "--no-modify-path", *extra, **kwargs)

    def assertInstalled(self, result: subprocess.CompletedProcess[str], version: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(os.readlink(self.root / "current"), f"versions/{version}")

    def assertNothingInstalled(self) -> None:
        self.assertFalse((self.root / "current").exists())
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()] if (self.root / "versions").exists() else [], [])
        self.assertFalse((self.bin / "charmer").exists())

    def write_marker(self, data: bytes, mode: int = 0o600) -> Path:
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        marker = self.state / "channel"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(marker, mode)
        return marker

    def snapshot(self) -> dict[str, object]:
        """Everything an installer run could change (to prove a refusal changed nothing)."""

        found: dict[str, object] = {}
        for base in (self.home,):
            for path in sorted(base.rglob("*")):
                if path.name == "launcher.log":
                    continue
                key = str(path.relative_to(self.home))
                if path.is_symlink():
                    found[key] = ("link", os.readlink(path))
                elif path.is_file():
                    found[key] = ("file", path.read_bytes(), os.stat(path).st_mode)
                else:
                    found[key] = ("dir", os.stat(path).st_mode)
        return found

    def launcher(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.bin / "charmer"), *args], stdin=subprocess.DEVNULL, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True, timeout=60)


@needs_ssh_keygen()
class InstallTests(InstallerTestCase):
    def test_install_from_a_release_directory(self) -> None:
        for shell in SHELLS:
            with self.subTest(shell=" ".join(shell)):
                self.setUp()
                release = self.release("1.0.0")
                result = self.install(release, shell=shell)
                self.assertInstalled(result, "1.0.0")
                self.assertIn("verified the SHA256SUMS signature", result.stdout)
                self.assertFalse((self.root / "previous").exists())
                for name in ("charmer", "charmer-proxy"):
                    shim = self.bin / name
                    self.assertTrue(shim.is_file() and not shim.is_symlink())
                    self.assertTrue(os.access(shim, os.X_OK))
                    self.assertIn("# charmer installer launcher", shim.read_text())
                self.assertFalse((self.bin / "claude-gateway").exists())
                self.assertFalse((self.bin / "charmer-dev").exists())
                self.assertEqual(self.launcher("--version").stdout.strip(), "charmer 1.0.0")
                self.assertEqual((self.state / "channel").read_text(), "bundle\n")
                self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
                receipt = json.loads((self.root / "installer.json").read_text())
                self.assertEqual(receipt["format"], 2)
                self.assertEqual([entry["path"] for entry in receipt["launchers"]],
                                 sorted(str(self.bin / n) for n in ("charmer", "charmer-proxy")))
                for entry in receipt["launchers"]:
                    self.assertEqual(entry["sha256"], hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest())
                self.assertEqual(receipt["path_lines"], [])
                self.assertEqual(install_receipt.read(self.root).document(), receipt)
                self.assertEqual(os.stat(self.root / "installer.json").st_mode & 0o777, 0o600)
                self.assertIn("is not on your PATH", result.stdout)
                leftovers = [p.name for p in (self.root / "versions").iterdir() if p.name.startswith(".")]
                self.assertEqual(leftovers, [])
                self.assertFalse((self.root / ".lock").exists())

    @unittest.skipUnless(shutil.which("setfacl"), "setfacl is needed for inherited default ACLs")
    def test_inherited_default_acl_cannot_expose_created_state_directories(self) -> None:
        # A default ACL can give mkdir -p's parents group/other access even
        # though the installer sets umask 077. The install leaf is not enough.
        subprocess.run(["setfacl", "-m", "d:u::rwx,d:g::rwx,d:o::rx", str(self.home)],
                       check=True, capture_output=True)
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        for relative in (".local", ".local/share", ".local/share/charmer",
                         ".local/share/charmer/install", ".local/share/charmer/install/versions",
                         ".local/bin", ".local/state", ".local/state/charmer"):
            path = self.home / relative
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, relative)

    def test_setup_hand_off_without_a_terminal_prints_the_next_step(self) -> None:
        release = self.release("1.0.0")
        result = self.run_installer("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                    "--no-modify-path")
        self.assertInstalled(result, "1.0.0")
        self.assertIn("next: run 'charmer setup'", result.stdout)
        self.assertFalse((self.home / "launcher.log").exists())

    def test_an_installation_of_the_earlier_name_hands_off_to_the_migration(self) -> None:
        # Setup is not started (not even without --no-setup): the bundle
        # names the move instead.
        from charmer import legacy

        (self.home / ".local/state" / legacy.LEGACY_DIR).mkdir(parents=True)
        release = self.release("1.0.0")
        result = self.run_installer("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                    "--no-modify-path")
        self.assertInstalled(result, "1.0.0")
        self.assertIn(f"an installation of {legacy.NAME} exists; run `charmer migrate from-legacy` instead of "
                      "`charmer setup`", result.stdout)
        self.assertNotIn("next: run 'charmer setup'", result.stdout)
        self.assertFalse((self.home / "launcher.log").exists())

    def test_updates_keep_one_previous_version(self) -> None:
        for version in ("1.0.0", "1.0.1", "1.1.0"):
            self.assertInstalled(self.install(self.release(version)), version)
        self.assertEqual(os.readlink(self.root / "previous"), "versions/1.0.1")
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), ["1.0.1", "1.1.0"])
        self.assertEqual(self.launcher("--version").stdout.strip(), "charmer 1.1.0")

    def test_incoming_sync_failure_never_switches_the_installation(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        self.assertInstalled(self.install(self.release("1.0.1")), "1.0.1")
        before = tuple(os.readlink(self.root / name) for name in ("current", "previous"))
        source = (REPO_ROOT / "src/charmer/self_update.py").read_bytes()
        failure = b'\ndef sync_bundle(root, version, **kwargs):\n    raise UpdateError("fixture incoming sync failure; links were left as they are")\n'
        release = self.release("1.0.2", overrides={"charmer/self_update.py": source + failure})
        result = self.install(release)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture incoming sync failure", result.stderr)
        self.assertEqual(tuple(os.readlink(self.root / name) for name in ("current", "previous")), before)
        self.assertFalse((self.root / ".switch.json").exists())
        self.assertFalse((self.root / ".lock").exists())

    def older_bundle_sync(self, exit_code: int):
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        self.assertInstalled(self.install(self.release("1.1.0")), "1.1.0")
        before = tuple(os.readlink(self.root / name) for name in ("current", "previous"))
        source = (REPO_ROOT / "src/charmer/install_txn.py").read_bytes()
        older = b'''
_original_parser = _parser
def _parser():
    parser = _original_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action.choices.pop("sync-bundle", None)
    return parser
'''
        release = self.release("0.9.0", overrides={"charmer/install_txn.py": source + older})
        tools = self.tmp / "sync-tools"
        tools.mkdir()
        sync = tools / "sync"
        sync.write_text('#!/bin/sh\nreadlink "$SYNC_ROOT/current" >"$SYNC_LOG"\n'
                        'readlink "$SYNC_ROOT/previous" >>"$SYNC_LOG"\nexit "$SYNC_EXIT"\n')
        sync.chmod(0o755)
        log = self.tmp / "sync.log"
        result = self.install(release, path=f"{tools}:{self.fakebin}:{os.environ['PATH']}",
                              SYNC_ROOT=str(self.root), SYNC_LOG=str(log), SYNC_EXIT=str(exit_code))
        return result, before, log

    def test_older_bundle_uses_system_sync_before_switching(self) -> None:
        result, before, log = self.older_bundle_sync(0)
        self.assertInstalled(result, "0.9.0")
        self.assertEqual(tuple(log.read_text().splitlines()), before)
        self.assertIn("incoming release lacks the durability helper; used system sync", result.stdout)
        self.assertEqual(os.readlink(self.root / "previous"), "versions/1.1.0")

    def test_older_bundle_system_sync_failure_preserves_both_links(self) -> None:
        result, before, log = self.older_bundle_sync(1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("system sync failed; the links were left as they are", result.stderr)
        self.assertEqual(tuple(log.read_text().splitlines()), before)
        self.assertEqual(tuple(os.readlink(self.root / name) for name in ("current", "previous")), before)
        self.assertFalse((self.root / ".switch.json").exists())

    def test_same_version_staging_sync_failure_preserves_the_current_tree(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        launcher = self.root / "current/bin/charmer"
        before = b"the current launcher must stay in place\n"
        launcher.write_bytes(before)
        source = (REPO_ROOT / "src/charmer/self_update.py").read_bytes()
        failure = b'\ndef sync_bundle(root, version, **kwargs):\n    raise UpdateError("fixture staging sync failed")\n'
        release = self.release("1.0.0", overrides={"charmer/self_update.py": source + failure})
        result = self.install(release)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture staging sync failed", result.stderr)
        self.assertEqual(launcher.read_bytes(), before)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        self.assertFalse((self.root / ".switch.json").exists())
        self.assertFalse(list((self.root / "versions").glob(".replaced.*")))

    def test_reinstalling_the_same_version_replaces_it(self) -> None:
        release = self.release("1.0.0")
        self.assertInstalled(self.install(release), "1.0.0")
        (self.root / "versions/1.0.0/bin/charmer").write_text("broken")
        self.assertInstalled(self.install(release), "1.0.0")
        self.assertEqual(self.launcher("--version").stdout.strip(), "charmer 1.0.0")
        self.assertFalse((self.root / "previous").exists())

    def test_rollback_and_the_state_format_refusal(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0", state_format=4)), "1.0.0")
        self.assertInstalled(self.install(self.release("1.1.0", state_format=5)), "1.1.0")
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "state-version").write_text("5\n")
        refused = self.run_installer("--rollback")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("cannot roll back to 1.0.0", refused.stderr)
        self.assertIn("format 5", refused.stderr)
        self.assertIn("stay on 1.1.0", refused.stderr)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.1.0")
        (self.state / "state-version").write_text("4\n")
        rolled = self.run_installer("--rollback")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        self.assertEqual(os.readlink(self.root / "previous"), "versions/1.1.0")
        self.assertEqual(self.launcher("--version").stdout.strip(), "charmer 1.0.0")

    def test_the_install_lock(self) -> None:
        release = self.release("1.0.0")
        lock = self.root / ".lock"
        lock.mkdir(parents=True)
        busy = self.install(release)
        self.assertEqual(busy.returncode, 1)
        self.assertIn("if none is running, remove", busy.stderr)
        self.assertIn(str(lock), busy.stderr)
        (lock / "pid").write_text(f"{os.getpid()}\n")
        running = self.install(release)
        self.assertIn(f"is running (process {os.getpid()})", running.stderr)
        dead = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL).stdout.strip()
        (lock / "pid").write_text(f"{dead}\n")
        self.assertInstalled(self.install(release), "1.0.0")
        self.assertFalse(lock.exists())

    def test_acquisition_honours_the_updaters_stable_guard(self) -> None:
        from charmer.platform import posix_fs

        release = self.release("1.0.0")
        self.root.mkdir(parents=True, mode=0o700)
        (self.root / ".lock").mkdir()
        (self.root / ".lock/pid").write_text(f"{2 ** 22 + 1}\n")
        descriptor = os.open(self.root / ".lock-guard", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            posix_fs.lock_descriptor(descriptor, shared=False, blocking=False)
            result = self.install(release)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("another install or update is running", result.stderr)
            self.assertNothingInstalled()
            self.assertEqual((self.root / ".lock/pid").read_text(), f"{2 ** 22 + 1}\n")
        finally:
            os.close(descriptor)

    def test_cleanup_keeps_another_owners_install_lock(self) -> None:
        source = (REPO_ROOT / "src/charmer/install_txn.py").read_bytes()
        replacement = b'''
_original_main = main
def main(*args, **kwargs):
    if "preflight" in sys.argv and "--token-env" in sys.argv:
        root = Path(sys.argv[sys.argv.index("--install-root") + 1])
        (root / ".lock/pid").write_text(os.environ["REPLACEMENT_PID"] + "\\n")
        return 1
    return _original_main(*args, **kwargs)
'''
        release = self.release("1.0.0", overrides={"charmer/install_txn.py": source + replacement})
        result = self.install(release, REPLACEMENT_PID=str(os.getpid()))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.root / ".lock/pid").read_text(), f"{os.getpid()}\n")
        self.assertNothingInstalled()

    def test_legacy_helper_stale_lock_refuses_repair_and_rollback(self) -> None:
        source = (REPO_ROOT / "src/charmer/install_txn.py").read_bytes()
        legacy = b'''
_original_parser = _parser
def _parser():
    parser = _original_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action.choices.pop("acquire-lock", None)
    return parser
'''
        for version in ("1.0.0", "1.1.0"):
            release = self.release(version, overrides={"charmer/install_txn.py": source + legacy})
            self.assertInstalled(self.install(release), version)
        lock = self.root / ".lock"
        lock.mkdir()
        holder = f"{2 ** 22 + 1}\n"
        (lock / "pid").write_text(holder)
        for mode in ("--repair", "--rollback"):
            with self.subTest(mode=mode):
                result = self.run_installer(mode, "--no-modify-path")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("Confirm no installer or charmer update process is running", result.stderr)
                self.assertIn(str(lock), result.stderr)
                self.assertIn("rm -r", result.stderr)
                self.assertEqual((lock / "pid").read_text(), holder)
                self.assertEqual(os.readlink(self.root / "current"), "versions/1.1.0")
        shutil.rmtree(lock)  # The fixture operator has confirmed neither run acquired it.
        result = self.run_installer("--rollback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")

    def test_rollback_without_a_previous_version(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        result = self.run_installer("--rollback")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no previous version", result.stderr)

    def test_installing_a_version_that_cannot_read_the_state_refuses(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "state-version").write_text("9\n")
        result = self.install(self.release("1.0.0", state_format=4))
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read your state", result.stderr)
        self.assertNothingInstalled()

    def test_repair_restores_launchers_and_the_current_link(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        (self.bin / "charmer").unlink()
        (self.root / "current").unlink()
        result = self.run_installer("--repair", "--no-modify-path")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        self.assertEqual(self.launcher("--version").stdout.strip(), "charmer 1.0.0")
        empty = tempfile.mkdtemp(dir=self.tmp)
        nothing = self.run_installer("--repair", HOME=empty)
        self.assertEqual(nothing.returncode, 1)
        self.assertIn("nothing is installed", nothing.stderr)
        # Given the release, a repair with nothing installed installs it.
        fresh = Path(tempfile.mkdtemp(dir=self.tmp))
        installed = self.install(self.release("1.0.0"), "--repair", HOME=str(fresh))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("installing the release", installed.stdout)
        self.assertEqual(os.readlink(fresh / ".local/share/charmer/install/current"), "versions/1.0.0")

    def test_uninstall_delegates_to_the_launcher(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        result = self.run_installer("--uninstall", "--", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "launcher.log").read_text(), "charmer 1.0.0 uninstall --yes\n")
        empty = tempfile.mkdtemp(dir=self.tmp)
        missing = self.run_installer("--uninstall", HOME=empty)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no installed charmer", missing.stderr)

    def test_modify_path_appends_one_marked_line(self) -> None:
        release = self.release("1.0.0")
        for _ in range(2):
            result = self.run_installer("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                        "--no-setup", "--modify-path", SHELL="/bin/bash")
            self.assertInstalled(result, "1.0.0")
        lines = (self.home / ".bashrc").read_text().splitlines()
        marked = [line for line in lines if "# added by the charmer installer" in line]
        self.assertEqual(marked, ['export PATH="$HOME/.local/bin:$PATH" # added by the charmer installer'])
        receipt = json.loads((self.root / "installer.json").read_text())
        self.assertEqual(receipt["path_lines"], [{
            "file": str(self.home / ".bashrc"), "marker": "# added by the charmer installer",
            "line": 'export PATH="$HOME/.local/bin:$PATH" # added by the charmer installer'}])
        self.assertTrue(install_receipt.read(self.root).path_lines[0].present())
        on_path = self.install(release, PATH=f"{self.fakebin}:{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        self.assertEqual(on_path.returncode, 0, on_path.stderr + on_path.stdout)
        self.assertNotIn("not on your PATH", on_path.stdout)

    def test_xdg_state_home_is_honoured(self) -> None:
        state_home = self.tmp / "state-home"
        result = self.install(self.release("1.0.0"), XDG_STATE_HOME=str(state_home))
        self.assertInstalled(result, "1.0.0")
        self.assertEqual((state_home / "charmer" / "channel").read_text(), "bundle\n")
        self.assertFalse(self.state.exists())


@needs_ssh_keygen()
class VerificationTests(InstallerTestCase):
    def test_tampered_bundle_is_refused(self) -> None:
        release = self.release("1.0.0")
        asset = release.asset()
        data = bytearray(asset.read_bytes())
        data[len(data) // 2] ^= 0xFF
        asset.write_bytes(bytes(data))
        result = self.install(release)
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertNothingInstalled()

    def test_altered_checksums_are_refused(self) -> None:
        release = self.release("1.0.0")
        sums = release.dir / "SHA256SUMS"
        sums.write_text(sums.read_text().replace("MANIFEST.json", "MANIFEST.jsom"))
        result = self.install(release)
        self.assertEqual(result.returncode, 1)
        self.assertIn("signature does not verify", result.stderr)
        self.assertNothingInstalled()

    def test_wrong_namespace_and_wrong_key_are_refused(self) -> None:
        wrong_namespace = self.release("1.0.0", sign=False)
        wrong_namespace.sign(self.key, namespace="file")
        other = self.release("1.0.1", sign=False)
        other.sign(self.other_key)
        for release in (wrong_namespace, other):
            with self.subTest(release.version):
                result = self.install(release)
                self.assertEqual(result.returncode, 1)
                self.assertIn("signature does not verify", result.stderr)
                self.assertNothingInstalled()

    def test_missing_signature_or_key_is_refused(self) -> None:
        unsigned = self.release("1.0.0", sign=False)
        result = self.install(unsigned)
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no SHA256SUMS.sshsig", result.stderr)
        no_key = self.run_installer("--from-dir", str(self.release("1.0.1").dir), "--no-setup")
        self.assertEqual(no_key.returncode, 1)
        self.assertIn("carries no release key", no_key.stderr)
        self.assertNothingInstalled()

    def _versioned_installer(self, version: str, sums: str) -> Path:
        text = INSTALL_SH.read_text()
        self.assertEqual(text.count("RELEASE_VERSION=''"), 1)
        self.assertEqual(text.count("RELEASE_SUMS=''"), 1)
        text = text.replace("RELEASE_VERSION=''", f"RELEASE_VERSION='{version}'")
        text = text.replace("RELEASE_SUMS=''", f"RELEASE_SUMS='{sums}'")
        path = self.tmp / "install-versioned.sh"
        path.write_text(text)
        return path

    def _restricted_path(self) -> str:
        restricted = self.tmp / "restricted-bin"
        restricted.mkdir()
        for tool in INSTALLER_TOOLS + ("sh",):
            found = shutil.which(tool)
            if found:
                (restricted / tool).symlink_to(found)
        shutil.copy2(self.fakebin / "uname", restricted / "uname")
        return str(restricted)

    def test_embedded_checksums_without_ssh_keygen(self) -> None:
        release = self.release("1.0.0", sign=False)
        script = self._versioned_installer("1.0.0", (release.dir / "SHA256SUMS").read_text().strip())
        path = self._restricted_path()
        result = self.run_installer("--from-dir", str(release.dir), "--no-setup", "--no-modify-path",
                                    script=script, path=path, shell=(str(Path(path) / "sh"),))
        self.assertInstalled(result, "1.0.0")
        self.assertIn("ssh-keygen is not installed: verifying against the checksums built into this installer",
                      result.stdout)
        # The embedded list is authoritative: a bundle it does not match is refused.
        tampered = self.release("1.0.0", sign=False, claude="9.9.9")
        refused = self.run_installer("--from-dir", str(tampered.dir), "--no-setup", script=script,
                                     path=path, shell=(str(Path(path) / "sh"),))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("checksum mismatch", refused.stderr)

    def test_a_failed_signature_never_falls_back_to_the_embedded_checksums(self) -> None:
        # The installer carries the right checksums, but the signature is
        # made with another key: with ssh-keygen installed that is fatal.
        release = self.release("1.0.0", sign=False)
        release.sign(self.other_key)
        script = self._versioned_installer("1.0.0", (release.dir / "SHA256SUMS").read_text().strip())
        result = self.run_installer("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                    "--no-setup", script=script)
        self.assertEqual(result.returncode, 1)
        self.assertIn("signature does not verify", result.stderr)
        self.assertNotIn("checksums built into this installer", result.stdout + result.stderr)
        self.assertNothingInstalled()
        # A missing signature is no reason to use them either.
        (release.dir / "SHA256SUMS.sshsig").unlink()
        result = self.run_installer("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                    "--no-setup", script=script)
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no SHA256SUMS.sshsig", result.stderr)
        self.assertNothingInstalled()

    def test_signature_without_ssh_keygen_and_without_embedded_checksums(self) -> None:
        path = self._restricted_path()
        result = self.install(self.release("1.0.0"), path=path, shell=(str(Path(path) / "sh"),))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ssh-keygen", result.stderr)
        self.assertNothingInstalled()

    def test_a_bundle_whose_manifest_disagrees_is_refused(self) -> None:
        # The 1.0.0 bundle published as 1.0.2: the signed checksums match the
        # file, but the bundle inside is not 1.0.2.
        release = self.release("1.0.2", sign=False)
        release.asset().write_bytes(self.release("1.0.0", sign=False).asset().read_bytes())
        release.write_sums()
        release.sign(self.key)
        result = self.install(release)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not hold exactly charmer-1.0.2-linux-x86_64/", result.stderr)
        self.assertNothingInstalled()


@needs_ssh_keygen()
class ChannelTests(InstallerTestCase):
    def test_a_nix_owned_state_root_refuses_without_the_migration_flag(self) -> None:
        self.write_marker(b"nix\n")
        release = self.release("1.0.0")
        refused = self.install(release)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("belongs to the nix installation", refused.stderr)
        self.assertIn("--migrate-from-nix", refused.stderr)
        self.assertNothingInstalled()
        moved = self.install(release, "--migrate-from-nix")
        self.assertInstalled(moved, "1.0.0")
        self.assertEqual((self.state / "channel").read_text(), "bundle\n")
        self.assertIn("it was 'nix'", moved.stdout)

    def test_a_nix_store_link_refuses_without_the_migration_flag(self) -> None:
        # The exec link the earlier name's Nix setups kept, probed by the
        # installer the release build fills in (charmer.legacy), and
        # charmer's own Nix link, probed by every installer.
        from charmer import installs, legacy

        build = load_tool(REPO_ROOT / "tools" / "build.py", "build")
        filled = self.tmp / "install.sh"
        filled.write_text(build.fill_shell_fields(INSTALL_SH.read_text(), {
            "LEGACY_NIX_RELEASE": build.legacy_nix_release()}))
        self.assertEqual(Path(".local/share") / build.legacy_nix_release(), legacy.LEGACY_NIX_RELEASE_LINK)
        for link, target in ((self.home / legacy.LEGACY_NIX_RELEASE_LINK, f"{legacy.NAME}-1.0.0"),
                             (self.home / ".local/share/charmer" / installs.NIX_LINK, "charmer-2.0.0")):
            with self.subTest(link=link.name):
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(f"/nix/store/00000000000000000000000000000000-{target}")
                release = self.release("1.0.0")
                refused = self.install(release, script=filled)
                self.assertEqual(refused.returncode, 1)
                self.assertIn(f"a Nix-managed installation is on this account ({link})", refused.stderr)
                self.assertNothingInstalled()
                self.assertInstalled(self.install(release, "--migrate-from-nix", script=filled), "1.0.0")
                self.assertTrue(link.is_symlink())  # never touched
                link.unlink()
                shutil.rmtree(self.root)
                shutil.rmtree(self.bin, ignore_errors=True)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_a_current_link_into_the_nix_store_always_refuses(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "current").symlink_to("/nix/store/00000000000000000000000000000000-charmer")
        result = self.install(self.release("1.0.0"), "--migrate-from-nix")
        self.assertEqual(result.returncode, 1)
        self.assertIn("points into /nix/store", result.stderr)

    def test_a_foreign_launcher_file_is_never_overwritten(self) -> None:
        self.bin.mkdir(parents=True)
        (self.bin / "charmer-proxy").write_text("#!/bin/sh\necho mine\n")
        result = self.install(self.release("1.0.0"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("was not written by this installer", result.stderr)
        self.assertEqual((self.bin / "charmer-proxy").read_text(), "#!/bin/sh\necho mine\n")
        self.assertFalse((self.root / "current").exists())


@needs_ssh_keygen()
class StrictMarkerTests(InstallerTestCase):
    """The state root's channel marker is validated with the runtime's own
    rules before every mutation: install, repair, rollback and migration."""

    MALFORMED = {
        "empty": (b"", 0o600),
        "no newline": (b"bundle", 0o600),
        "two lines": (b"bundle\nnix\n", 0o600),
        "trailing space": (b"bundle \n", 0o600),
        "unknown channel": (b"pip\n", 0o600),
        "group readable": (b"bundle\n", 0o640),
        "world readable": (b"bundle\n", 0o644),
    }

    def assertRefusedWithoutChange(self, result: subprocess.CompletedProcess[str], before: dict) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("nothing was changed", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_malformed_markers_refuse_every_mutation(self) -> None:
        release_a, release_b = self.release("1.0.0"), self.release("1.1.0")
        self.assertInstalled(self.install(release_a), "1.0.0")
        self.assertInstalled(self.install(release_b), "1.1.0")
        for label, (data, mode) in self.MALFORMED.items():
            for args in ((), ("--migrate-from-nix",)):
                with self.subTest(marker=label, args=args):
                    self.write_marker(data, mode)
                    for run in (lambda: self.install(release_a, *args),
                                lambda: self.run_installer("--rollback", *args),
                                lambda: self.run_installer("--repair", "--no-modify-path", *args)):
                        before = self.snapshot()
                        self.assertRefusedWithoutChange(run(), before)

    def test_a_symlinked_marker_refuses(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        real = self.tmp / "elsewhere"
        real.write_bytes(b"bundle\n")
        os.chmod(real, 0o600)
        (self.state / "channel").unlink()
        (self.state / "channel").symlink_to(real)
        for run in (lambda: self.install(self.release("1.0.1")), lambda: self.run_installer("--repair")):
            before = self.snapshot()
            result = run()
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("symlink", result.stderr)
            self.assertEqual(self.snapshot(), before)

    def test_a_valid_foreign_marker_refuses_rollback_and_repair_until_migrated(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        self.assertInstalled(self.install(self.release("1.1.0")), "1.1.0")
        for channel in (b"nix\n", b"source\n"):
            with self.subTest(channel=channel):
                self.write_marker(channel)
                for args in (("--rollback",), ("--repair", "--no-modify-path")):
                    before = self.snapshot()
                    result = self.run_installer(*args)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("belongs to the", result.stderr)
                    self.assertIn("--migrate-from-nix", result.stderr)
                    self.assertEqual(self.snapshot(), before)
        rolled = self.run_installer("--rollback", "--migrate-from-nix")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        # A rollback switches links only; the marker is claimed by an install or repair.
        repaired = self.run_installer("--repair", "--no-modify-path", "--migrate-from-nix")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual((self.state / "channel").read_bytes(), b"bundle\n")
        self.assertIn("it was 'source'", repaired.stdout)

    def test_the_marker_is_written_like_the_runtime_writes_it(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        marker = self.state / "channel"
        self.assertEqual(marker.read_bytes(), b"bundle\n")
        self.assertEqual(os.stat(marker).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        from charmer import installs

        self.assertEqual(installs.read_marker(self.state, {"HOME": str(self.home)}), "bundle")


@needs_ssh_keygen()
class TransactionTests(InstallerTestCase):
    """A running gateway's installation, gateway hand-offs and the receipt."""

    SLEEP = Path("/usr/bin/sleep")

    def start_gateway(self, version: str) -> subprocess.Popen:
        """A process executing versions/<version>'s gateway file, with its start record."""

        binary = self.root / "versions" / version / "libexec/charmer/cli-proxy-api"
        process = subprocess.Popen([str(binary), "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        workdir = service.ensure_gateway_workdir(self.state)
        service.write_exec_stamp(workdir, service.ExecStamp(
            version, "signature", process.pid, os.path.realpath(binary), service.utc_now(),
            pid_namespace=os.readlink("/proc/self/ns/pid"), instance="0123456789abcdef"))
        return process

    def gateway_release(self, version: str) -> FakeRelease:
        return self.release(version, gateway_binary=self.SLEEP.read_bytes())

    @unittest.skipUnless(SLEEP.is_file() and Path("/proc/self/exe").exists(),
                         "BOUNDARY: needs /usr/bin/sleep and /proc")
    def test_the_running_gateways_installation_survives_two_updates(self) -> None:
        self.assertInstalled(self.install(self.gateway_release("1.0.0")), "1.0.0")
        process = self.start_gateway("1.0.0")
        binary = os.path.realpath(self.root / "versions/1.0.0/libexec/charmer/cli-proxy-api")
        self.assertInstalled(self.install(self.release("1.1.0")), "1.1.0")
        result = self.install(self.release("1.2.0"))
        self.assertInstalled(result, "1.2.0")
        self.assertIn("kept 1.0.0: the running gateway executes it", result.stdout)
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), ["1.0.0", "1.1.0", "1.2.0"])
        # The gateway still runs, still from the recorded binary, so its
        # ownership proof (start record + executable) holds.
        self.assertIsNone(process.poll())
        self.assertTrue(Path(f"/proc/{process.pid}/exe").samefile(binary))
        from charmer.platform import linux_process

        stamp = service.read_exec_stamp(service.gateway_workdir(self.state))
        self.assertEqual(linux_process.gateway_pid(stamp, proc_root=Path("/proc"), readlink=os.readlink,
                                                   main_pid=lambda: None), (process.pid, True))
        process.kill()
        process.wait()
        self.assertInstalled(self.install(self.release("1.3.0")), "1.3.0")
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), ["1.2.0", "1.3.0"])

    @unittest.skipUnless(SLEEP.is_file() and Path("/proc/self/exe").exists(),
                         "BOUNDARY: needs /usr/bin/sleep and /proc")
    def test_the_running_gateways_version_is_never_replaced_in_place(self) -> None:
        release = self.gateway_release("1.0.0")
        self.assertInstalled(self.install(release), "1.0.0")
        self.start_gateway("1.0.0")
        before = self.snapshot()
        result = self.install(release)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not replaced in place", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_an_unreadable_start_record_keeps_every_version(self) -> None:
        for version in ("1.0.0", "1.1.0"):
            self.assertInstalled(self.install(self.release(version)), version)
        workdir = service.ensure_gateway_workdir(self.state)
        fd = os.open(workdir / service.EXEC_STAMP, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"{not json")
        os.close(fd)
        result = self.install(self.release("1.2.0"))
        self.assertInstalled(result, "1.2.0")
        self.assertIn("kept every installed version", result.stdout)
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), ["1.0.0", "1.1.0", "1.2.0"])

    def test_an_earlier_releases_hand_off_record_refuses(self) -> None:
        """A gateway service hand-off recorded by an earlier release (its
        ``service-handoff.json``) still refuses every mode, in progress or
        interrupted, before any change."""

        from test_install_txn import DEAD_PID, legacy_handoff

        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        for pid, label in ((os.getpid(), "in progress"), (DEAD_PID, "interrupted")):
            legacy_handoff(self.state, pid=pid)
            for args in ((), ("--rollback",), ("--repair",)):
                with self.subTest(label, args=args):
                    before = self.snapshot()
                    result = self.install(self.release("1.1.0")) if not args else self.run_installer(*args)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    if args != ("--rollback",):  # the rollback has no previous version to go to first
                        self.assertIn("the gateway is inhibited by service (gateway service install "
                                      "(charmer-gateway)", result.stderr)
                        self.assertIn("fix: ", result.stderr)
                    self.assertEqual(self.snapshot(), before)

    KILLER = """#!/bin/sh
# The bundle's interpreter, killing the installer once it recorded phase $KILL_AT.
'{python}' "$@"
status=$?
case " $* " in
*" advance --phase ${KILL_AT:-none} "*) kill -"${KILL_SIGNAL:-KILL}" "$PPID" ;;
esac
exit $status
"""

    def killing_release(self, version: str) -> FakeRelease:
        import sys

        killer = self.tmp / "killer"
        if not killer.exists():
            killer.write_text(self.KILLER.replace("{python}", sys.executable))
            killer.chmod(0o755)
        return self.release(version, python=str(killer))

    def interrupted(self, version: str, phase: str, signal: str = "KILL") -> subprocess.CompletedProcess[str]:
        return self.install(self.killing_release(version), KILL_AT=phase, KILL_SIGNAL=signal)

    def assertRecord(self, phase: str | None) -> None:
        from charmer import gateway_inhibition

        record = gateway_inhibition.read(self.state)
        if phase is None:
            self.assertIsNone(record)
        else:
            self.assertEqual((record.owner, record.phase, record.remedy), ("installer", phase, "sh install.sh --repair"))

    def test_an_interruption_at_each_step_keeps_the_inhibition_until_repair_finishes_it(self) -> None:
        from charmer import install_txn

        expected_current = {"replace": "1.0.0", "switch": "1.0.0", "prune": "1.1.0", "launchers": "1.1.0"}
        for phase, current in expected_current.items():
            with self.subTest(phase):
                self.setUp()
                self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
                result = self.interrupted("1.1.0", phase)
                self.assertEqual(result.returncode, -9, result.stderr)
                self.assertRecord(phase)
                # Every gateway change of another owner is refused meanwhile,
                # and another install or update is too.
                with self.assertRaisesRegex(install_txn.TransactionError, "inhibited by installer"):
                    install_txn.guard(self.state)
                again = self.install(self.release("1.2.0"))
                self.assertEqual(again.returncode, 1, again.stderr)
                self.assertIn("its owner did not finish (stale)", again.stderr)
                self.assertIn("sh install.sh --repair", again.stderr)
                repaired = self.run_installer("--repair")
                self.assertEqual(repaired.returncode, 0, repaired.stderr + repaired.stdout)
                self.assertIn("recovered installer (install charmer 1.1.0; phase " + phase, repaired.stderr)
                self.assertIn("finishing an interrupted run of the installer", repaired.stdout)
                self.assertRecord(None)
                self.assertEqual(os.readlink(self.root / "current"), f"versions/{current}")
                self.assertEqual(self.launcher().stdout.strip(), f"charmer {current}")
                self.assertFalse([p.name for p in (self.root / "versions").iterdir() if p.name.startswith(".")])
                # Recovery is idempotent: a second repair has nothing to recover.
                repaired = self.run_installer("--repair")
                self.assertEqual(repaired.returncode, 0, repaired.stderr)
                self.assertNotIn("finishing an interrupted run", repaired.stdout)
                self.assertRecord(None)

    def test_an_interrupted_first_installation_is_finished_by_repair(self) -> None:
        """Killed during a first installation (nothing installed before): a
        plain retry still refuses with the repair as its remedy, and the
        repair finishes it, with the release when no version reached
        versions/ yet."""

        for phase in ("replace", "switch", "prune", "launchers"):
            with self.subTest(phase):
                self.setUp()
                result = self.interrupted("1.0.0", phase)
                self.assertEqual(result.returncode, -9, result.stderr)
                self.assertRecord(phase)
                again = self.install(self.release("1.0.0"))
                self.assertEqual(again.returncode, 1, again.stderr)
                self.assertIn("sh install.sh --repair", again.stderr)
                if phase == "replace":  # no version reached versions/: the repair needs the release
                    bare = self.run_installer("--repair")
                    self.assertEqual(bare.returncode, 1, bare.stderr)
                    self.assertIn("nothing is installed", bare.stderr)
                    self.assertIn("sh install.sh --repair --from-dir DIR", bare.stderr)
                    self.assertRecord(phase)
                    repaired = self.install(self.release("1.0.0"), "--repair")
                    self.assertIn("installing the release, which finishes an interrupted first installation",
                                  repaired.stdout)
                else:
                    repaired = self.run_installer("--repair")
                self.assertEqual(repaired.returncode, 0, repaired.stderr + repaired.stdout)
                self.assertIn("finishing an interrupted run of the installer", repaired.stdout)
                self.assertRecord(None)
                self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
                self.assertEqual(self.launcher().stdout.strip(), "charmer 1.0.0")
                self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.0.0"])
                self.assertEqual((self.state / "channel").read_text(), "bundle\n")

    def test_a_terminated_installer_ends_its_inhibition_only_if_nothing_changed(self) -> None:
        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        before = sorted(p.name for p in (self.root / "versions").iterdir())
        # Stopped as it records its first change, before making it: ended.
        result = self.interrupted("1.1.0", "replace", "TERM")
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertRecord(None)
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), before)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        # Stopped after the new version was put in place: kept, with the remedy.
        result = self.interrupted("1.1.0", "switch", "TERM")
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn("gateway changes stay paused until you run: sh install.sh --repair", result.stderr)
        self.assertRecord("switch")
        repaired = self.run_installer("--repair")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertRecord(None)

    def test_a_version_moved_aside_comes_back_on_repair(self) -> None:
        release = self.release("1.0.0")
        self.assertInstalled(self.install(release), "1.0.0")
        versions = self.root / "versions"
        os.rename(versions / "1.0.0", versions / ".replaced.1.0.0.4242")  # a reinstall stopped mid-move
        (versions / ".staging.4242").mkdir()
        from charmer import gateway_inhibition

        gateway_inhibition.begin(self.state, owner="installer", purpose="install charmer 1.0.0",
                                 phase="replace", remedy="sh install.sh --repair",
                                 expiry=gateway_inhibition.owner_process(2 ** 22 + 1))
        repaired = self.run_installer("--repair")
        self.assertEqual(repaired.returncode, 0, repaired.stderr + repaired.stdout)
        self.assertIn("put back 1.0.0", repaired.stdout)
        self.assertEqual(sorted(p.name for p in versions.iterdir()), ["1.0.0"])
        self.assertEqual(self.launcher().stdout.strip(), "charmer 1.0.0")
        self.assertRecord(None)

    def test_a_switch_stopped_between_its_links_is_put_back_on_repair(self) -> None:
        """A rollback that died after ``previous`` changed and before
        ``current`` did (both on 1.1.0) left its switch record: the repair
        puts the recorded pair back, so the rollback target is not lost."""

        from charmer import gateway_inhibition, self_update

        for version in ("1.0.0", "1.1.0"):
            self.assertInstalled(self.install(self.release(version)), version)
        record = self.root / self_update.SWITCH_RECORD
        record.write_text(json.dumps({"format": 1, "before": {"current": "versions/1.1.0", "previous": "versions/1.0.0"},
                                      "after": {"current": "versions/1.0.0", "previous": "versions/1.1.0"}}))
        (self.root / "previous").unlink()
        (self.root / "previous").symlink_to("versions/1.1.0")
        gateway_inhibition.begin(self.state, owner="installer", purpose="roll back charmer to 1.0.0",
                                 phase="switch", remedy="sh install.sh --repair",
                                 expiry=gateway_inhibition.owner_process(2 ** 22 + 1))
        refused = self.run_installer("--rollback")  # the stopped run's inhibition still holds
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("sh install.sh --repair", refused.stderr)
        repaired = self.run_installer("--repair")
        self.assertEqual(repaired.returncode, 0, repaired.stderr + repaired.stdout)
        self.assertIn("put the installed versions back as they were before an interrupted switch (current 1.1.0, "
                      "previous 1.0.0)", repaired.stdout)
        self.assertEqual((os.readlink(self.root / "current"), os.readlink(self.root / "previous")),
                         ("versions/1.1.0", "versions/1.0.0"))
        self.assertFalse(record.exists())
        self.assertRecord(None)
        rolled = self.run_installer("--rollback")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertEqual((os.readlink(self.root / "current"), os.readlink(self.root / "previous")),
                         ("versions/1.0.0", "versions/1.1.0"))
        self.assertFalse(record.exists())
        self.assertEqual(self.launcher().stdout.strip(), "charmer 1.0.0")

    def test_the_installer_prunes_claude_code_copies_and_keeps_one_in_use(self) -> None:
        from charmer import pin

        self.assertInstalled(self.install(self.release("1.0.0")), "1.0.0")
        environ = {"HOME": str(self.home)}
        owned = pin.owned_root(environ)
        owned.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(owned, 0o700)
        for version in ("2.1.100", "2.1.150", "2.1.200"):
            (owned / version).mkdir(mode=0o700)
            (owned / version / "claude").write_bytes(b"#!/bin/sh\n")
            (owned / version / "claude").chmod(0o755)
        session = pin.take_use_lock(environ, "2.1.100", shared=True)  # a session runs this copy
        self.addCleanup(session.release)
        result = self.install(self.release("1.1.0"))
        self.assertInstalled(result, "1.1.0")
        self.assertIn("Removed Claude Code copies no release here needs: 2.1.150.", result.stdout)
        self.assertIn("Kept Claude Code copies in use: 2.1.100", result.stdout)
        # 2.1.200: the newest below the current pin (the previous release's pin).
        self.assertEqual(sorted(p.name for p in owned.iterdir() if not p.name.startswith(".")),
                         ["2.1.100", "2.1.200"])
        session.release()
        result = self.install(self.release("1.2.0"))
        self.assertInstalled(result, "1.2.0")
        self.assertIn("Removed Claude Code copies no release here needs: 2.1.100.", result.stdout)

    def test_receipt_keeps_its_path_line_across_runs(self) -> None:
        release = self.release("1.0.0")
        self.assertInstalled(self.run_installer("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                                "--no-setup", "--modify-path", SHELL="/bin/bash"), "1.0.0")
        bashrc = self.home / ".bashrc"
        bashrc.write_text("# mine\n" + bashrc.read_text() + "alias x=y")  # no final newline
        on_path = f"{self.fakebin}:{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        self.assertInstalled(self.install(self.release("1.1.0"), PATH=on_path), "1.1.0")
        receipt = install_receipt.read(self.root)
        self.assertEqual([item.file for item in receipt.path_lines], [str(bashrc)])
        self.assertTrue(all(item.owned() for item in receipt.launchers))
        (self.bin / "charmer").write_text("#!/bin/sh\n# charmer installer launcher\necho edited\n")
        self.assertFalse(install_receipt.read(self.root).launchers[0].owned())


class PlatformTests(InstallerTestCase):
    def refused(self, **env: str) -> str:
        result = self.run_installer("--from-dir", str(self.tmp), "--no-setup", **env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".local").exists())
        return result.stderr

    def test_unsupported_platforms_are_refused_before_any_change(self) -> None:
        self.assertIn("unsupported operating system: FreeBSD", self.refused(FAKE_UNAME_S="FreeBSD"))
        self.assertIn("unsupported processor: riscv64", self.refused(FAKE_UNAME_M="riscv64"))
        self.assertIn("unsupported processor: ppc", self.refused(FAKE_UNAME_S="Darwin", FAKE_UNAME_M="ppc"))
        self.assertIn("install.ps1", self.refused(FAKE_UNAME_S="MINGW64_NT-10.0"))

    def test_wsl1_and_windows_drive_homes_are_refused(self) -> None:
        self.assertIn("WSL 1 is not supported", self.refused(FAKE_UNAME_R="4.4.0-19041-Microsoft"))
        result = self.run_installer("--no-setup", HOME="/mnt/c/Users/someone",
                                    FAKE_UNAME_R="5.15.167.4-microsoft-standard-WSL2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is on a Windows drive", result.stderr)

    def test_wsl2_with_a_linux_home_passes_the_platform_checks(self) -> None:
        result = self.run_installer("--from-dir", str(self.tmp), "--no-setup",
                                    FAKE_UNAME_R="5.15.167.4-microsoft-standard-WSL2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no charmer bundle for linux-x86_64", result.stderr)

    def test_usage_errors(self) -> None:
        for args, text in ((("--bogus",), "unknown option: --bogus"), (("--version",), "--version needs a value"),
                           (("--from-dir", str(self.tmp), "--version", "1.0"), "not a release version"),
                           (("--version", "1.0.0", "--no-setup"), "names no download location"),
                           (("--version", "1.0.0", "--base-url", "http://example.invalid/x"), "must use https")):
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertIn(text, result.stderr)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.run_installer("--help").returncode, 0)

    def test_sudo_is_refused(self) -> None:
        if os.getuid() == 0:
            self.assertIn("not through sudo", self.refused(SUDO_USER="someone"))
        else:
            self.assertEqual(self.run_installer("--help", SUDO_USER="someone").returncode, 0)


class MacOSVersionTests(InstallerTestCase):
    """Synthetic platform commands only; no native macOS probe or gateway."""

    PLATFORM_COMMANDS = ("uname", "mktemp", "sw_vers", "sysctl", "cp", "curl", "wget")

    def setUp(self) -> None:
        super().setUp()
        self.tools = self.tmp / "platform-bin"
        self.tools.mkdir()
        # No host PATH fallback: even a missing sw_vers is synthetic on macOS.
        for tool in INSTALLER_TOOLS + ("sh", "ssh-keygen"):
            if found := shutil.which(tool):
                (self.tools / tool).symlink_to(found)
        shutil.copy2(self.fakebin / "uname", self.tools / "uname")
        self.command("sysctl", "exit 1\n")
        self.command("sw_vers", 'printf "%s\\n" "$*" >>"$VERSION_LOG"\n'
                     'if [ "${SYSTEM_VERSION_COMPAT:-}" = 1 ]; then printf "10.16\\n"; '
                     'else printf "%s\\n" "$FAKE_SW_VERSION"; fi\n'
                     'printf "%s\\n" "fixture-version-detail" >&2\nexit "$FAKE_SW_STATUS"\n')
        self.version_log = self.tmp / "version.log"
        self.effect_log = self.tmp / "effects.log"
        (self.home / ".profile").write_text("# user profile\n")

    def command(self, name: str, body: str) -> None:
        path = self.tools / name
        if path.is_symlink():
            path.unlink()
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def block_effects(self) -> None:
        for tool in ("mktemp", "cp", "curl", "wget"):
            self.command(tool, 'printf "%s\\n" "$0" >>"$EFFECT_LOG"\nexit 99\n')

    def run_platform(self, *args: str, shell: tuple[str, ...] = ("sh",), script: Path = INSTALL_SH,
                     **env: str) -> subprocess.CompletedProcess[str]:
        environ = {"FAKE_UNAME_S": "Darwin", "FAKE_UNAME_M": "arm64", "FAKE_SW_VERSION": "12.7.6",
                   "FAKE_SW_STATUS": "0", "VERSION_LOG": str(self.version_log), "EFFECT_LOG": str(self.effect_log)}
        environ.update(env)
        executable = shutil.which(shell[0])
        self.assertIsNotNone(executable)
        # BusyBox standalone shells can resolve applets before PATH. Functions
        # win first, and an absent fixture must fail rather than reach the host.
        functions = "\n".join(f'{name}() {{ {shlex.quote(str(self.tools / name))} "$@"; }}'
                              for name in self.PLATFORM_COMMANDS)
        # -c sets $0 to the script path run_installer appends and keeps its argv.
        # Source it in this selected shell: execing another would lose functions.
        trampoline = (executable, *shell[1:], "-c", functions + '\n. "$0"\n')
        return self.run_installer(*args, shell=trampoline, script=script, path=str(self.tools), **environ)

    def fallback_path(self) -> str:
        directory = self.tmp / "fallback-bin"
        directory.mkdir()
        for name in self.PLATFORM_COMMANDS:
            path = directory / name
            path.write_text('#!/bin/sh\nprintf "ambient command must not run\\n"\nexit 98\n', encoding="utf-8")
            path.chmod(0o755)
        return f"{directory}{os.pathsep}{self.tools}"

    def test_platform_functions_run_every_fixture_and_preserve_script_argv(self) -> None:
        # Both fixture paths and arguments need shell quoting, independently of
        # whether this host's BusyBox actually bypasses PATH for bare commands.
        tools = self.tmp / "platform bin's"
        self.tools.rename(tools)
        self.tools = tools
        for name in self.PLATFORM_COMMANDS:
            self.command(name, 'printf "<%s>\\n" "$0" "$@"\n')
        script = self.tmp / "dispatch script's.sh"
        script.write_text('set -eu\nprintf "<%s>\\n" "$0" "$@"\n' +
                          "\n".join(f'{name} "$@"' for name in self.PLATFORM_COMMANDS) + "\n", encoding="utf-8")
        args = ("argument with spaces", "literal'quote", "")
        expected = [f"<{script}>", *(f"<{arg}>" for arg in args)]
        for name in self.PLATFORM_COMMANDS:
            expected.extend([f"<{self.tools / name}>", *(f"<{arg}>" for arg in args)])
        path = self.fallback_path()
        if busybox := shutil.which("busybox"):
            version = subprocess.run([busybox, "--help"], cwd=self.tmp, capture_output=True, text=True,
                                     timeout=10, stdin=subprocess.DEVNULL,
                                     env={"HOME": str(self.home), "PATH": str(self.tools), "LC_ALL": "C"})
            self.assertEqual(version.returncode, 0, version.stdout + version.stderr)
            banner = (version.stdout + version.stderr).splitlines()[0]
            print(f"installer BusyBox: path={os.path.realpath(busybox)} version={banner}", flush=True)
        else:
            print("installer BusyBox: unavailable on PATH", flush=True)
        for shell in SHELLS:
            with self.subTest(shell=shell):
                executable = shutil.which(shell[0])
                self.assertIsNotNone(executable)
                # Record actual bare dispatch, not an inference about CI's
                # BusyBox compile flags from an emulation on another host.
                for name in ("uname", "mktemp"):
                    bare = self.run_installer("--charmer-fixture-dispatch", script=script, path=str(self.tools),
                                              shell=(executable, *shell[1:], "-c", f'{name} "$@"'))
                    fixture = bare.stdout == f"<{self.tools / name}>\n<--charmer-fixture-dispatch>\n"
                    print(f"installer bare dispatch: shell={' '.join(shell)} path={executable} "
                          f"command={name} fixture={fixture} status={bare.returncode}", flush=True)
                result = self.run_platform(*args, shell=shell, script=script, PATH=path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)
                self.assertEqual(result.stderr, "")

    def test_absent_platform_fixtures_never_fall_through_to_path_or_applets(self) -> None:
        path = self.fallback_path()
        script = self.tmp / "missing-fixture.sh"
        for name in self.PLATFORM_COMMANDS:
            (self.tools / name).unlink(missing_ok=True)
            script.write_text(f'set -eu\n{name} "$@"\n', encoding="utf-8")
            for shell in SHELLS:
                with self.subTest(shell=shell, command=name):
                    result = self.run_platform("--charmer-fixture-dispatch", shell=shell, script=script, PATH=path)
                    self.assertEqual(result.returncode, 127, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(str(self.tools / name), result.stderr)

    def install_platform(self, release: FakeRelease, *args: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return self.run_platform("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                 "--no-setup", "--no-modify-path", *args, **kwargs)

    def versioned_installer(self) -> Path:
        script = self.tmp / "install-versioned.sh"
        script.write_text(INSTALL_SH.read_text().replace("RELEASE_VERSION=''", "RELEASE_VERSION='2.1.0'"))
        return script

    def assertFloorRefused(self, result: subprocess.CompletedProcess[str], before: dict) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("macOS 13 or newer", result.stderr)
        self.assertIn("compatible older release", result.stderr)
        self.assertIn("nothing was installed", result.stderr)
        self.assertNotIn("fixture-version-detail", result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.effect_log.exists(), "refusal must precede temporary files and downloads")

    def test_unsupported_macos_refuses_before_effects(self) -> None:
        self.block_effects()
        before = self.snapshot()
        for shell in SHELLS:
            for version in ("2.1.0", "2.1.0-dev", "2.1.1", "2.10.0", "3.0.0"):
                for macos in ("9.9.9", "10.15.7", "12.7.6"):
                    with self.subTest(shell=shell, version=version, macos=macos):
                        result = self.run_platform("--version", version, "--from-dir", str(self.tmp),
                                                   "--modify-path", shell=shell, FAKE_SW_VERSION=macos)
                        self.assertFloorRefused(result, before)
        self.assertTrue(self.version_log.read_text().splitlines())
        self.assertEqual(set(self.version_log.read_text().splitlines()), {"-productVersion"})

    def test_unreadable_or_malformed_macos_refuses_before_effects(self) -> None:
        self.block_effects()
        before = self.snapshot()
        for shell in SHELLS:
            for version, status in (("13.0", "1"), ("", "0"), ("fixture-version-detail", "0"), ("13x", "0"),
                                    ("13.x", "0"), ("+13.0", "0"), ("13.-1", "0"), ("13..0", "0"),
                                    (".13", "0"), ("13.", "0"), ("13.0.0.1", "0"), (" 13.0", "0"),
                                    ("13.0\n12.0", "0"), ("13.0\nunknown", "0"), ("9" * 80, "0")):
                with self.subTest(shell=shell, version=version, status=status):
                    result = self.run_platform("--version", "2.1.0", "--from-dir", str(self.tmp), shell=shell,
                                               FAKE_SW_VERSION=version, FAKE_SW_STATUS=status)
                    self.assertFloorRefused(result, before)

    def test_missing_sw_vers_refuses_before_effects(self) -> None:
        (self.tools / "sw_vers").unlink()
        self.block_effects()
        before = self.snapshot()
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_platform("--version", "2.1.0", "--base-url", "https://example.invalid/releases",
                                           shell=shell)
                self.assertFloorRefused(result, before)
                self.assertIn("cannot read the macOS version", result.stderr)

    def test_default_and_inferred_new_releases_are_guarded(self) -> None:
        self.block_effects()
        before = self.snapshot()
        # A published installer's default and a source installer's local filename inference.
        (self.tmp / "charmer-2.1.0-darwin-arm64.tar.gz").touch()
        for script in (self.versioned_installer(), INSTALL_SH):
            with self.subTest(script=script.name):
                self.assertFloorRefused(self.run_platform("--from-dir", str(self.tmp), script=script), before)

    def test_supported_macos_and_linux_reach_the_existing_download_checks(self) -> None:
        before = self.snapshot()
        for shell in SHELLS:
            for arch in ("arm64", "x86_64"):
                for version in ("13", "13.0", "13.6.9", "14.0", "26.1", "100.0"):
                    with self.subTest(shell=shell, arch=arch, version=version):
                        result = self.run_platform("--version", "2.1.0", "--from-dir", str(self.tmp), shell=shell,
                                                   FAKE_UNAME_M=arch, FAKE_SW_VERSION=version)
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn("the release has no SHA256SUMS", result.stderr)
        self.version_log.unlink()
        result = self.run_platform("--version", "2.1.0", "--from-dir", str(self.tmp),
                                   FAKE_UNAME_S="Linux", FAKE_UNAME_M="x86_64", FAKE_SW_STATUS="1")
        self.assertIn("the release has no SHA256SUMS", result.stderr)
        self.assertFalse(self.version_log.exists())
        self.assertEqual(self.snapshot(), before)

    def test_system_version_compat_is_disabled_only_for_the_version_query(self) -> None:
        after_query = self.tmp / "after-query.log"
        self.command("mktemp", 'printf "%s\\n" "${SYSTEM_VERSION_COMPAT:-unset}" >>"$AFTER_QUERY_LOG"\n'
                     'exec "$REAL_MKTEMP" "$@"\n')
        before = self.snapshot()
        for shell in SHELLS:
            for version in ("13.0", "14.6.1", "26.1"):
                with self.subTest(shell=shell, version=version):
                    result = self.run_platform("--version", "2.1.0", "--from-dir", str(self.tmp), shell=shell,
                                               FAKE_SW_VERSION=version, SYSTEM_VERSION_COMPAT="1",
                                               AFTER_QUERY_LOG=str(after_query), REAL_MKTEMP=shutil.which("mktemp"))
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("the release has no SHA256SUMS", result.stderr)
        self.assertEqual(after_query.read_text().splitlines(), ["1"] * (len(SHELLS) * 3))
        self.assertEqual(self.snapshot(), before)
        self.block_effects()
        refused = self.run_platform("--version", "2.1.0", "--from-dir", str(self.tmp), SYSTEM_VERSION_COMPAT="1")
        self.assertFloorRefused(refused, before)

    @needs_ssh_keygen()
    def test_supported_macos_and_linux_install_verified_fake_bundles(self) -> None:
        for system, arch, version, target in (("Darwin", "arm64", "13.0", "darwin-arm64"),
                                               ("Darwin", "x86_64", "14.6.1", "darwin-x86_64"),
                                               ("Linux", "x86_64", "unknown", "linux-x86_64")):
            with self.subTest(system=system, arch=arch):
                self.setUp()
                release = self.release("2.1.0", targets=(target,))
                result = self.install_platform(release, FAKE_UNAME_S=system, FAKE_UNAME_M=arch, FAKE_SW_VERSION=version)
                self.assertInstalled(result, "2.1.0")
                self.assertIn("verified the SHA256SUMS signature", result.stdout)
                self.assertEqual(self.version_log.exists(), system == "Darwin")

    @needs_ssh_keygen()
    def test_older_release_override_can_be_installed_and_reinstalled_on_macos12(self) -> None:
        script = self.versioned_installer()
        release = self.release("2.0.1", targets=("darwin-arm64",))
        for _ in range(2):
            result = self.install_platform(release, "--version", "2.0.1", script=script, FAKE_SW_STATUS="1")
            self.assertInstalled(result, "2.0.1")
            self.assertEqual(self.launcher("--version").stdout.strip(), "charmer 2.0.1")
            (self.root / "versions/2.0.1/bin/charmer").write_text("broken")
        self.assertFalse(self.version_log.exists())

    @needs_ssh_keygen()
    def test_existing_older_repair_and_moved_aside_recovery_do_not_probe_macos(self) -> None:
        from charmer import gateway_inhibition

        self.assertInstalled(self.install_platform(self.release("2.0.1", targets=("darwin-arm64",))), "2.0.1")
        script = self.versioned_installer()
        (self.bin / "charmer").unlink()
        (self.root / "current").unlink()
        self.assertInstalled(self.run_platform("--repair", "--no-modify-path", script=script, FAKE_SW_STATUS="1"), "2.0.1")
        versions = self.root / "versions"
        os.rename(versions / "2.0.1", versions / ".replaced.2.0.1.4242")
        gateway_inhibition.begin(self.state, owner="installer", purpose="install charmer 2.0.1", phase="replace",
                                 remedy="sh install.sh --repair", expiry=gateway_inhibition.owner_process(2 ** 22 + 1))
        repaired = self.run_platform("--repair", "--no-modify-path", script=script, FAKE_SW_STATUS="1")
        self.assertInstalled(repaired, "2.0.1")
        self.assertIn("put back 2.0.1", repaired.stdout)
        self.assertIsNone(gateway_inhibition.read(self.state))
        self.assertFalse(self.version_log.exists())

    @needs_ssh_keygen()
    def test_first_install_repair_guards_new_bytes_and_can_recover_an_older_release(self) -> None:
        from charmer import gateway_inhibition

        gateway_inhibition.begin(self.state, owner="installer", purpose="install charmer 2.0.1", phase="replace",
                                 remedy="sh install.sh --repair", expiry=gateway_inhibition.owner_process(2 ** 22 + 1))
        release = self.release("2.1.0", targets=("darwin-arm64",))
        before = self.snapshot()
        self.block_effects()
        refused = self.install_platform(release, "--repair", script=self.versioned_installer())
        self.assertFloorRefused(refused, before)
        for tool in ("mktemp", "cp"):
            (self.tools / tool).unlink()
            (self.tools / tool).symlink_to(shutil.which(tool))
        older = self.release("2.0.1", targets=("darwin-arm64",))
        self.assertInstalled(self.install_platform(older, "--repair"), "2.0.1")
        self.assertIsNone(gateway_inhibition.read(self.state))

    @needs_ssh_keygen()
    def test_rollback_does_not_probe_macos_or_apply_the_new_floor(self) -> None:
        for version in ("2.0.1", "2.1.0"):
            release = self.release(version, targets=("darwin-arm64",))
            self.assertInstalled(self.install_platform(release, FAKE_SW_VERSION="13.0"), version)
        (self.tools / "sw_vers").unlink()
        self.version_log.unlink()
        for version in ("2.0.1", "2.1.0"):
            self.assertInstalled(self.run_platform("--rollback", script=self.versioned_installer()), version)
        self.assertFalse(self.version_log.exists())


class StaticTests(InstallerTestCase):
    def test_syntax_under_every_available_shell(self) -> None:
        for shell in [("sh",), ("bash", "--posix")] + [s for s in SHELLS if s[0] == "busybox"]:
            if not shutil.which(shell[0]):
                continue
            with self.subTest(shell=" ".join(shell)):
                result = subprocess.run([*shell, "-n", str(INSTALL_SH)], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "BOUNDARY: shellcheck is not installed")
    def test_shellcheck(self) -> None:
        result = subprocess.run(["shellcheck", "-s", "sh", str(INSTALL_SH)], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_release_placeholders_are_empty_in_the_tree(self) -> None:
        text = INSTALL_SH.read_text()
        for name in ("RELEASE_VERSION", "RELEASE_BASE_URL", "RELEASE_SIGNERS", "RELEASE_SUMS", "LEGACY_NIX_RELEASE"):
            self.assertEqual(text.count(f"\n{name}=''\n"), 1, name)
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("umask 077", text)
        self.assertNotIn("claude-gateway", text.split("SHIMS='", 1)[1].split("'", 1)[0])

    def test_the_nix_links_it_refuses_are_the_ones_doctor_reports(self) -> None:
        """One definition of the other channel's links: the installer's
        refusal and doctor's installation list (``installs``) name the same
        paths below HOME."""

        from charmer import installs

        text = INSTALL_SH.read_text()
        self.assertIn("\ndata_home=$HOME/.local/share\n", text)
        links = {name: Path(".local/share") / path for name, path in
                 re.findall(r"(?m)^(nix_link\w*)=\$data_home/(\S+)$", text)}
        self.assertEqual(links, {"nix_link": Path(".local/share/charmer") / installs.NIX_LINK})
        # The earlier name's link: the field the release build fills in.
        self.assertIn('\n[ -n "$LEGACY_NIX_RELEASE" ] && nix_link_legacy=$data_home/$LEGACY_NIX_RELEASE\n', text)
        build = load_tool(REPO_ROOT / "tools" / "build.py", "build")
        self.assertEqual(Path(".local/share") / build.legacy_nix_release(), installs.NIX_RELEASE_LINK)


if __name__ == "__main__":
    unittest.main()
