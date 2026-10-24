"""The bundle self-update core (``charmer.self_update``), offline.

Releases are fake release directories served by ``DirectoryTransport``;
signatures use the test-side sshsig signer (or a stub verifier), so nothing
here needs the network. One class cross-checks the layout with
``packaging/install.sh``.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import subprocess
import threading
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import self_update as su
from charmer import trust
from _fake_release import FakeRelease, make_key, needs_ssh_keygen, signers_line as keygen_signers_line
from _release import INSTALL_SH, requires_release_tree
from test_trust import SEED_A, SEED_B, signer_line, sshsig

GATEWAY_A = "a" * 64
GATEWAY_B = "b" * 64


def accept(_sums: bytes, _signature: bytes) -> None:
    return None


def refuse(_sums: bytes, _signature: bytes) -> None:
    raise trust.TrustError("signature does not verify")


def clear() -> None:
    return None


def nobody() -> frozenset[str]:
    return frozenset()


def no_copies(_bundle: Path, _release: su.Release) -> list[str]:
    return []


SAFE = {"hold": clear, "protected": nobody, "inhibit": contextlib.nullcontext, "prune_copies": no_copies}


def roll_back(root: Path, **seams) -> su.RolledBack:
    """A rollback confirmed for the installation as it is now."""

    return su.rollback(root, confirmed=su.read_installation(root), **seams)


class SelfUpdateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="charmer-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "install"
        self.state = self.tmp / "state"
        self.state.mkdir(mode=0o700)

    def release(self, version: str, *, seed: bytes = SEED_A, **kwargs) -> FakeRelease:
        release = FakeRelease(self.tmp / f"release-{version}", version, **kwargs)
        sums = (release.dir / "SHA256SUMS").read_bytes()
        (release.dir / "SHA256SUMS.sshsig").write_text(sshsig(seed, sums))
        return release

    def seed(self, release: FakeRelease) -> su.Installation:
        versions = self.root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        with tarfile.open(release.asset()) as tar:
            tar.extractall(versions, filter="data")
        (versions / f"charmer-{release.version}-linux-x86_64").rename(versions / release.version)
        current = self.root / "current"
        if current.is_symlink():
            current.unlink()
        current.symlink_to(f"versions/{release.version}")
        installation = su.read_installation(self.root)
        assert installation is not None
        return installation

    def write_state(self, value: str) -> None:
        marker = self.state / "state-version"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(value)

    def update_to(self, release: FakeRelease, *, verifier=accept, **seams) -> su.Applied:
        installation = su.read_installation(self.root)
        assert installation is not None
        transport = su.DirectoryTransport(release.dir)
        result = su.check(installation, transport, verifier)
        planned = su.plan(installation, result.release, state=su.state_version(self.state))
        return su.apply(installation, result.release, planned, transport, state_root=self.state,
                        **{**SAFE, **seams})

    def versions(self) -> list[str]:
        return sorted(p.name for p in (self.root / "versions").iterdir())


class DirectoryTransportFailureTests(SelfUpdateTestCase):
    def test_destination_failures_keep_the_local_path_and_errno(self) -> None:
        source = self.tmp / "bundle.tar.gz"
        source.write_bytes(b"bundle")
        transport = su.DirectoryTransport(self.tmp)
        destination = self.tmp / "absent" / "download"
        with self.assertRaises(FileNotFoundError) as caught:
            transport.download(source.name, "1.0.0", destination, 6)
        self.assertEqual(caught.exception.filename, str(destination))
        for operation in ("write", "__exit__"):
            with self.subTest(operation=operation):
                target = mock.MagicMock()
                target.__enter__.return_value = target
                getattr(target, operation).side_effect = OSError(28, "No space left on device", str(destination))
                real_open = open
                def open_file(path, mode):
                    return target if path == destination else real_open(path, mode)
                with mock.patch.object(su, "open", side_effect=open_file, create=True), \
                        self.assertRaises(OSError) as caught:
                    transport.download(source.name, "1.0.0", destination, 6)
                self.assertEqual(caught.exception.errno, 28)
                self.assertEqual(caught.exception.filename, str(destination))

    def test_source_open_and_read_failures_keep_the_release_diagnostic(self) -> None:
        transport = su.DirectoryTransport(self.tmp)
        with self.assertRaisesRegex(su.UpdateError, "cannot read bundle.tar.gz from the release"):
            transport.download("bundle.tar.gz", "1.0.0", self.tmp / "out", 6)
        source = mock.MagicMock()
        source.__enter__.return_value = source
        source.read.side_effect = OSError(5, "Input/output error")
        real_open = open
        def open_file(path, mode):
            return source if mode == "rb" else real_open(path, mode)
        with mock.patch.object(su, "open", side_effect=open_file, create=True), \
                self.assertRaisesRegex(su.UpdateError, "cannot read bundle.tar.gz from the release"):
            transport.download("bundle.tar.gz", "1.0.0", self.tmp / "out", 6)


class VersionAndStateTests(SelfUpdateTestCase):
    def test_version_order(self) -> None:
        ordered = ["0.9.9", "1.0.0-dev", "1.0.0", "1.0.1", "1.2.0", "1.10.0", "2.0.0"]
        self.assertEqual(sorted(ordered, key=su.version_key), ordered)
        for bad in ("1.0", "01.0.0", "1.0.0-rc1", "v1.0.0", ""):
            with self.subTest(bad), self.assertRaises(su.UpdateError):
                su.version_key(bad)

    def test_state_version(self) -> None:
        self.assertEqual(su.state_version(self.state), 3)  # unmarked state
        self.write_state("4\n")
        self.assertEqual(su.state_version(self.state), 4)
        self.write_state("4x")
        with self.assertRaisesRegex(su.UpdateError, "damaged"):
            su.state_version(self.state)
        (self.state / "state-version").unlink()
        (self.state / "state-version").symlink_to(self.tmp / "elsewhere")
        with self.assertRaisesRegex(su.UpdateError, "not a regular file"):
            su.state_version(self.state)

    def test_install_root_is_under_the_data_root(self) -> None:
        self.assertEqual(su.install_root({"HOME": "/home/someone"}),
                         Path("/home/someone/.local/share/charmer/install"))


class InstallationTests(SelfUpdateTestCase):
    def test_install_lock_creates_private_ancestors_under_permissive_umask(self) -> None:
        root = self.tmp / ".local/share/charmer/install"
        previous = os.umask(0o002)
        try:
            with su._InstallLock(root):
                for path in (root, *root.parents):
                    if path == self.tmp:
                        break
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700, str(path))
        finally:
            os.umask(previous)

    def test_nothing_installed(self) -> None:
        self.assertIsNone(su.read_installation(self.root))

    def test_reads_current_and_previous(self) -> None:
        self.seed(self.release("1.0.0", gateway=GATEWAY_A))
        self.seed(self.release("1.0.1", gateway=GATEWAY_B))
        (self.root / "previous").symlink_to("versions/1.0.0")
        installation = su.read_installation(self.root)
        self.assertEqual(installation.current.version, "1.0.1")
        self.assertEqual(installation.current.gateway_sha256, GATEWAY_B)
        self.assertEqual(installation.current.target, "linux-x86_64")
        self.assertEqual(installation.previous.version, "1.0.0")

    def test_damaged_layouts(self) -> None:
        self.root.mkdir()
        (self.root / "current").symlink_to("/nix/store/x-charmer")
        with self.assertRaisesRegex(su.UpdateError, "not at an installed version"):
            su.read_installation(self.root)
        (self.root / "current").unlink()
        (self.root / "current").symlink_to("versions/1.0.0")
        with self.assertRaisesRegex(su.UpdateError, "unreadable"):
            su.read_installation(self.root)


class InstallLockRaceTests(SelfUpdateTestCase):
    def test_fresh_acquisition_creates_no_guard_file(self) -> None:
        with su.install_lock(self.root):
            self.assertFalse((self.root / ".lock-guard").exists())
            self.assertEqual((self.root / ".lock/pid").read_text(), f"{os.getpid()}\n")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_holder_released_before_the_pid_read_is_retried(self) -> None:
        first = su.install_lock(self.root)
        first.acquire()
        second = su.install_lock(self.root)
        original = second._holder
        released = []
        def holder():
            if not released:
                first.__exit__(None, None, None)
                released.append(True)
                return None
            return original()
        try:
            with mock.patch.object(second, "_holder", side_effect=holder):
                with second:
                    self.assertEqual(released, [True])
                    self.assertEqual((self.root / ".lock/pid").read_text(), f"{os.getpid()}\n")
        finally:
            if not released:
                first.__exit__(None, None, None)
        self.assertFalse((self.root / ".lock").exists())
        self.assertTrue((self.root / ".lock-guard").exists())
        self.assertIn(".lock-guard", su.__doc__)

    def test_the_guard_is_stable_and_unsafe_guard_files_are_refused(self) -> None:
        lock = self.root / ".lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{2 ** 22 + 1}\n")
        with su.install_lock(self.root):
            guard = self.root / ".lock-guard"
            identity = (guard.stat().st_dev, guard.stat().st_ino)
        with su.install_lock(self.root):
            self.assertEqual((guard.stat().st_dev, guard.stat().st_ino), identity)
        self.assertEqual(guard.stat().st_mode & 0o777, 0o600)
        guard.unlink()
        target = self.tmp / "elsewhere"
        target.write_bytes(b"untouched")
        lock.mkdir()
        (lock / "pid").write_text(f"{2 ** 22 + 1}\n")
        guard.symlink_to(target)
        with self.assertRaises((su.UpdateError, OSError)):
            with su.install_lock(self.root):
                self.fail("symlinked guard accepted")
        self.assertEqual(target.read_bytes(), b"untouched")
        guard.unlink()
        os.mkfifo(guard)
        with self.assertRaises((su.UpdateError, OSError)):
            with su.install_lock(self.root):
                self.fail("special guard accepted")
        self.assertEqual((lock / "pid").read_text(), f"{2 ** 22 + 1}\n")

    def test_release_leaves_a_replacement_owners_lock_alone(self) -> None:
        held = su.install_lock(self.root)
        held.__enter__()
        (self.root / ".lock/pid").write_text(f"{os.getppid()}\n")
        held.__exit__(None, None, None)
        self.assertEqual((self.root / ".lock/pid").read_text(), f"{os.getppid()}\n")

    def test_two_stale_reclaimers_cannot_both_acquire(self) -> None:
        lock = self.root / ".lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{2 ** 22 + 1}\n")
        paused, second_attempted, first_attempted, release = (threading.Event() for _ in range(4))
        acquired, refused, errors = [], [], []
        real_rename = su.os.rename

        def rename(source, destination):
            if Path(source) == lock and threading.current_thread().name == "first":
                paused.set()
                if not second_attempted.wait(5):
                    raise AssertionError("second reclaimer did not attempt the lock")
            return real_rename(source, destination)

        def run(name):
            held = su.install_lock(self.root)
            owns = False
            try:
                try:
                    held.__enter__()
                    owns = True
                    acquired.append(name)
                except su.UpdateError:
                    refused.append(name)
                finally:
                    (first_attempted if name == "first" else second_attempted).set()
                if owns and not release.wait(5):
                    raise AssertionError("test did not release the winner")
            except BaseException as exc:
                errors.append(exc)
            finally:
                if owns:
                    held.__exit__(None, None, None)

        first = threading.Thread(target=run, args=("first",), name="first", daemon=True)
        second = threading.Thread(target=run, args=("second",), name="second", daemon=True)
        with mock.patch.object(su.os, "rename", side_effect=rename):
            try:
                first.start()
                self.assertTrue(paused.wait(5), "first reclaimer did not reach the rename barrier")
                second.start()
                self.assertTrue(first_attempted.wait(5), "first reclaimer did not finish acquiring")
                self.assertEqual(errors, [])
                self.assertEqual(len(acquired), 1, f"both reclaimers acquired: {acquired}")
                self.assertEqual(len(refused), 1)
                self.assertEqual((lock / "pid").read_text(), f"{os.getpid()}\n")
            finally:
                second_attempted.set()
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(lock.exists())

    LEGACY_LOCK = r'''
lock=$1/.lock
locked=no
trap 'if [ "$locked" = yes ]; then rm -rf "$lock"; fi' EXIT
if ! mkdir -m 700 "$lock" 2>/dev/null; then
    holder=''
    IFS= read -r holder <"$lock/pid" 2>/dev/null || true
    case $holder in
    '' | *[!0-9]*) echo held; exit 1 ;;
    *) if kill -0 "$holder" 2>/dev/null; then echo held; exit 1; fi ;;
    esac
    stale=$1/.lock.stale.$$
    if mv "$lock" "$stale" 2>/dev/null; then rm -rf "$stale"; fi
    mkdir -m 700 "$lock" 2>/dev/null || exit 1
fi
printf '%s\n' "$$" >"$lock/pid"
locked=yes
printf 'acquired\n'
IFS= read -r release
'''

    def test_live_new_lock_refuses_the_legacy_mkdir_protocol(self) -> None:
        with su.install_lock(self.root):
            result = subprocess.run(["sh", "-c", self.LEGACY_LOCK, "sh", str(self.root)],
                                    cwd=self.tmp, input="\n", capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, "held\n")
            self.assertEqual((self.root / ".lock/pid").read_text(), f"{os.getpid()}\n")

    def test_live_legacy_mkdir_lock_refuses_the_new_protocol(self) -> None:
        self.root.mkdir(mode=0o700)
        process = subprocess.Popen(["sh", "-c", self.LEGACY_LOCK, "sh", str(self.root)], cwd=self.tmp,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "legacy fixture did not acquire")
            self.assertEqual(process.stdout.readline(), "acquired\n")
            with self.assertRaisesRegex(su.UpdateError, "another install or update is running"):
                with su.install_lock(self.root):
                    self.fail("both protocols acquired")
            self.assertEqual((self.root / ".lock/pid").read_text(), f"{process.pid}\n")
            _out, err = process.communicate("\n", timeout=5)
            self.assertEqual(process.returncode, 0, err)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.assertFalse((self.root / ".lock").exists())


class CheckAndPlanTests(SelfUpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.installation = self.seed(self.release("1.0.0", gateway=GATEWAY_A))

    def test_check_statuses(self) -> None:
        for version, status in (("1.0.1", "available"), ("1.0.0", "current"), ("0.9.0", "older")):
            with self.subTest(version):
                release = self.release(version, gateway=GATEWAY_A)
                result = su.check(self.installation, su.DirectoryTransport(release.dir), accept)
                self.assertEqual((result.status, result.installed, result.release.version), (status, "1.0.0", version))

    def test_check_with_the_signature_verifier(self) -> None:
        release = self.release("1.0.1")
        good = su.signature_verifier(signer_line(SEED_A))
        self.assertEqual(su.check(self.installation, su.DirectoryTransport(release.dir), good).status, "available")
        rotated = su.signature_verifier(signer_line(SEED_B))
        with self.assertRaisesRegex(su.UpdateError, "not trusted"):
            su.check(self.installation, su.DirectoryTransport(release.dir), rotated)

    def test_check_refusals(self) -> None:
        release = self.release("1.0.1")
        transport = su.DirectoryTransport(release.dir)
        with self.assertRaisesRegex(su.UpdateError, "not trusted"):
            su.check(self.installation, transport, refuse)
        manifest = json.loads((release.dir / "MANIFEST.json").read_text())
        (release.dir / "MANIFEST.json").write_text(json.dumps(manifest) + " ")
        with self.assertRaisesRegex(su.UpdateError, "does not match the signed SHA256SUMS"):
            su.check(self.installation, transport, accept)
        other_target = self.release("1.0.2", targets=("linux-aarch64",))
        with self.assertRaisesRegex(su.UpdateError, "no bundle for linux-x86_64"):
            su.check(self.installation, su.DirectoryTransport(other_target.dir), accept)
        dev = self.release("1.0.3-dev")
        with self.assertRaisesRegex(su.UpdateError, "not a release version"):
            su.check(self.installation, su.DirectoryTransport(dev.dir), accept)
        missing = self.tmp / "empty"
        missing.mkdir()
        with self.assertRaisesRegex(su.UpdateError, "has no MANIFEST.json"):
            su.check(self.installation, su.DirectoryTransport(missing), accept)

    def _plan(self, release: FakeRelease, state: int = 4) -> su.Plan:
        result = su.check(self.installation, su.DirectoryTransport(release.dir), accept)
        return su.plan(self.installation, result.release, state=state)

    def test_plan_for_a_launcher_only_update(self) -> None:
        planned = self._plan(self.release("1.0.1", gateway=GATEWAY_A))
        self.assertEqual((planned.from_version, planned.to_version, planned.restart), ("1.0.0", "1.0.1", "launcher"))
        self.assertIsNone(planned.claude_change)
        self.assertFalse(planned.rollback_blocked)
        self.assertEqual(planned.size, (self.tmp / "release-1.0.1" / planned.asset).stat().st_size)
        self.assertIn("only the launcher changes", planned.steps[-1])
        self.assertIn("charmer update --rollback", planned.steps[1])

    def test_plan_for_a_gateway_pin_and_state_change(self) -> None:
        planned = self._plan(self.release("1.1.0", gateway=GATEWAY_B, claude="9.9.9", state_format=5))
        self.assertEqual(planned.restart, "gateway")
        self.assertEqual(planned.claude_change, ("0.0.0-test", "9.9.9"))
        self.assertEqual(planned.state_change, (4, 5))
        self.assertTrue(planned.rollback_blocked)
        text = "\n".join(planned.steps)
        self.assertIn("Claude Code 9.9.9", text)
        self.assertIn("downloaded", text)
        self.assertIn("before anything is switched", text)
        self.assertIn("rolling back to 1.0.0 is refused", text)
        self.assertIn("charmer gateway restart", text)
        # The rollback is refused after the state upgrade, so the plan does not offer it.
        self.assertNotIn("if the new gateway does not start", text)

    def test_plan_for_a_gateway_change_names_the_way_back(self) -> None:
        planned = self._plan(self.release("1.0.1", gateway=GATEWAY_B))
        self.assertEqual(planned.restart, "gateway")
        self.assertIn("charmer gateway restart); if the new gateway does not start: charmer update --rollback",
                      planned.steps[-1])

    def test_plan_refusals(self) -> None:
        older = self.release("0.9.0")
        result = su.check(self.installation, su.DirectoryTransport(older.dir), accept)
        with self.assertRaisesRegex(su.UpdateError, "not newer"):
            su.plan(self.installation, result.release, state=4)
        with self.assertRaisesRegex(su.UpdateError, "reads state formats up to 4"):
            self._plan(self.release("1.0.1"), state=5)


class ApplyTests(SelfUpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed(self.release("1.0.0", gateway=GATEWAY_A))
        self.write_state("4\n")

    def test_apply_keeps_one_previous_version(self) -> None:
        applied = self.update_to(self.release("1.0.1", gateway=GATEWAY_A))
        self.assertEqual((applied.version, applied.previous, applied.restart), ("1.0.1", "1.0.0", "launcher"))
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.1")
        self.assertEqual(os.readlink(self.root / "previous"), "versions/1.0.0")
        applied = self.update_to(self.release("1.1.0", gateway=GATEWAY_B))
        self.assertEqual((applied.restart, applied.removed), ("gateway", ("1.0.0",)))
        self.assertEqual(self.versions(), ["1.0.1", "1.1.0"])
        self.assertFalse((self.root / ".downloads").exists())
        self.assertFalse((self.root / ".lock").exists())
        launcher = self.root / "current" / "bin" / "charmer"
        self.assertIn("1.1.0", launcher.read_text())

    def test_the_persistence_hold_vetoes_a_gateway_change_only(self) -> None:
        held = lambda: "a credential save is pending"  # noqa: E731
        with self.assertRaisesRegex(su.UpdateError, "credential save is pending; nothing was changed"):
            self.update_to(self.release("1.0.1", gateway=GATEWAY_B), hold=held)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        self.assertEqual(self.versions(), ["1.0.0"])
        self.assertEqual(self.update_to(self.release("1.0.2", gateway=GATEWAY_A), hold=held).restart, "launcher")

    def test_a_tampered_download_changes_nothing(self) -> None:
        release = self.release("1.0.1")
        installation = su.read_installation(self.root)
        transport = su.DirectoryTransport(release.dir)
        result = su.check(installation, transport, accept)
        planned = su.plan(installation, result.release, state=4)
        data = bytearray(release.asset().read_bytes())
        data[len(data) // 2] ^= 0xFF
        release.asset().write_bytes(bytes(data))
        with self.assertRaisesRegex(su.UpdateError, "does not match the signed checksum"):
            su.apply(installation, result.release, planned, transport, state_root=self.state, **SAFE)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        self.assertEqual(self.versions(), ["1.0.0"])
        self.assertFalse((self.root / ".downloads").exists())

    def test_a_runtime_that_cannot_run_here_changes_nothing(self) -> None:
        with self.assertRaisesRegex(su.UpdateError, "runtime does not run on this system; nothing was changed"):
            self.update_to(self.release("1.0.1", python="/bin/false"))
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        self.assertEqual(self.versions(), ["1.0.0"])

    def test_a_bundle_that_disagrees_with_the_manifest_is_refused(self) -> None:
        release = self.release("1.0.1", gateway=GATEWAY_A)
        manifest = json.loads((release.dir / "MANIFEST.json").read_text())
        manifest["assets"][release.asset().name]["gateway_sha256"] = GATEWAY_B
        (release.dir / "MANIFEST.json").write_text(json.dumps(manifest))
        release.write_sums()
        with self.assertRaisesRegex(su.UpdateError, "does not match the release manifest"):
            self.update_to(release)
        self.assertEqual(self.versions(), ["1.0.0"])
        manifest["assets"][release.asset().name]["gateway_sha256"] = GATEWAY_A
        manifest["assets"][release.asset().name]["tar_sha256"] = "c" * 64
        (release.dir / "MANIFEST.json").write_text(json.dumps(manifest))
        release.write_sums()
        with self.assertRaisesRegex(su.UpdateError, "content does not match"):
            self.update_to(release)

    def test_a_changed_installation_or_state_refuses(self) -> None:
        release = self.release("1.0.1", state_format=4)
        installation = su.read_installation(self.root)
        transport = su.DirectoryTransport(release.dir)
        result = su.check(installation, transport, accept)
        planned = su.plan(installation, result.release, state=4)
        self.write_state("5\n")
        with self.assertRaisesRegex(su.UpdateError, "cannot read the state"):
            su.apply(installation, result.release, planned, transport, state_root=self.state, **SAFE)
        self.write_state("4\n")
        self.seed(self.release("1.0.5"))
        with self.assertRaisesRegex(su.UpdateError, "changed since the plan"):
            su.apply(installation, result.release, planned, transport, state_root=self.state, **SAFE)

    def test_the_install_lock(self) -> None:
        (self.root / ".lock").mkdir()
        with self.assertRaises(su.UpdateError) as caught:
            self.update_to(self.release("1.0.1"))
        self.assertIn("holds", str(caught.exception))
        self.assertIn("remove", caught.exception.remedy)
        (self.root / ".lock" / "pid").write_text(f"{os.getpid()}\n")
        with self.assertRaisesRegex(su.UpdateError, "another install or update is running"):
            self.update_to(self.release("1.0.1"))
        dead = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
        (self.root / ".lock" / "pid").write_text(f"{dead}\n")
        self.assertEqual(self.update_to(self.release("1.0.1")).version, "1.0.1")
        self.assertFalse((self.root / ".lock").exists())

    def test_unpack_refuses_entries_outside_the_bundle(self) -> None:
        archive = self.tmp / "evil.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("charmer-1.0.1-linux-x86_64/MANIFEST.json", "evil"):
                info = tarfile.TarInfo(name)
                tar.addfile(info)
        with self.assertRaisesRegex(su.UpdateError, "outside charmer-1.0.1-linux-x86_64/"):
            su._unpack(archive, self.tmp / "staging", "charmer-1.0.1-linux-x86_64")
        link = self.tmp / "link.tar.gz"
        with tarfile.open(link, "w:gz") as tar:
            info = tarfile.TarInfo("charmer-1.0.1-linux-x86_64/escape")
            info.type, info.linkname = tarfile.SYMTYPE, "../../etc/passwd"
            tar.addfile(info)
        with self.assertRaisesRegex(su.UpdateError, "points outside"):
            su._unpack(link, self.tmp / "staging2", "charmer-1.0.1-linux-x86_64")

    def test_directory_transport_limits(self) -> None:
        release = self.release("1.0.1")
        transport = su.DirectoryTransport(release.dir)
        with self.assertRaisesRegex(su.UpdateError, "larger than 10 bytes"):
            transport.fetch("MANIFEST.json", None, 10)
        with self.assertRaisesRegex(su.UpdateError, "not a release file name"):
            transport.fetch("../MANIFEST.json", None, 10)
        with self.assertRaisesRegex(su.UpdateError, "larger than the 5 bytes"):
            transport.download(release.asset().name, "1.0.1", self.tmp / "out", 5)


class IncomingDurabilityTests(SelfUpdateTestCase):
    def test_staging_sync_failure_keeps_the_retained_previous_tree_unchanged(self) -> None:
        self.seed(self.release("1.0.0"))
        incoming = self.release("1.1.0")
        self.update_to(incoming)
        roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        previous_launcher = self.root / "previous/bin/charmer"
        previous_launcher.write_bytes(b"the retained launcher must stay in place\n")
        before = previous_launcher.read_bytes()
        links = tuple(os.readlink(self.root / name) for name in ("current", "previous"))
        observed = []
        def fail_staging(root, version, **kwargs):
            observed.append((root, version, kwargs))
            self.assertEqual(previous_launcher.read_bytes(), before)
            raise su.UpdateError("fixture staging sync failed")
        with mock.patch.object(su, "sync_bundle", side_effect=fail_staging):
            with self.assertRaisesRegex(su.UpdateError, "fixture staging sync failed"):
                self.update_to(incoming)
        self.assertEqual(observed, [(self.root, "1.1.0", {"staging": f".staging.{os.getpid()}"})])
        self.assertEqual(previous_launcher.read_bytes(), before)
        self.assertEqual(tuple(os.readlink(self.root / name) for name in ("current", "previous")), links)
        self.assertFalse(list((self.root / "versions").glob(".replaced.*")))

    def test_update_syncs_staging_before_replacement_and_version_afterwards(self) -> None:
        self.seed(self.release("1.0.0"))
        observed = []
        real = su.sync_bundle
        def barrier(root, version, **kwargs):
            staged = kwargs.get("staging")
            observed.append(staged)
            self.assertEqual((root / "versions" / version).exists(), staged is None)
            self.assertEqual(os.readlink(root / "current"), "versions/1.0.0")
            return real(root, version, **kwargs)
        with mock.patch.object(su, "sync_bundle", side_effect=barrier):
            self.update_to(self.release("1.1.0"))
        self.assertEqual(observed, [f".staging.{os.getpid()}", None])

    def test_update_sync_failure_leaves_both_links_and_switch_record_untouched(self) -> None:
        self.seed(self.release("1.0.0"))
        self.update_to(self.release("1.0.1"))
        before = tuple(os.readlink(self.root / name) for name in ("current", "previous"))
        with mock.patch.object(su, "sync_bundle", create=True,
                               side_effect=[None, su.UpdateError("incoming bundle could not be made durable")]) as barrier:
            with self.assertRaisesRegex(su.UpdateError, "incoming bundle could not be made durable"):
                self.update_to(self.release("1.0.2"))
        self.assertEqual(barrier.call_args_list, [
            mock.call(self.root, "1.0.2", staging=f".staging.{os.getpid()}"), mock.call(self.root, "1.0.2")])
        self.assertEqual(tuple(os.readlink(self.root / name) for name in ("current", "previous")), before)
        self.assertFalse((self.root / su.SWITCH_RECORD).exists())

    def test_rollback_skips_the_incoming_bundle_barrier(self) -> None:
        self.seed(self.release("1.0.0"))
        self.update_to(self.release("1.0.1"))
        with mock.patch.object(su, "sync_bundle", create=True, side_effect=AssertionError("rollback never syncs")):
            result = roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        self.assertEqual(result.version, "1.0.0")

    def test_linux_uses_one_sync_and_reports_its_failure(self) -> None:
        self.seed(self.release("1.0.0"))
        with mock.patch.object(su.sys, "platform", "linux"), mock.patch.object(su.os, "sync", create=True) as sync:
            su.sync_bundle(self.root, "1.0.0")
            sync.assert_called_once_with()
            sync.side_effect = OSError(5, "Input/output error")
            with self.assertRaisesRegex(su.UpdateError, "links were left as they are"):
                su.sync_bundle(self.root, "1.0.0")

    def test_file_walk_syncs_files_then_directories_without_following_links(self) -> None:
        import stat

        versions = self.root / "versions"
        incoming = versions / "1.0.0"
        nested = incoming / "nested"
        nested.mkdir(parents=True)
        payload = nested / "payload"
        payload.write_bytes(b"new bundle")
        outside = self.tmp / "outside"
        outside.write_bytes(b"never opened")
        (incoming / "link").symlink_to(outside)
        seen = []
        def fsync(fd):
            info = os.fstat(fd)
            seen.append((info.st_ino, stat.S_IFMT(info.st_mode)))
        with mock.patch.object(su.sys, "platform", "darwin"), mock.patch.object(su.os, "fsync", side_effect=fsync):
            su.sync_bundle(self.root, "1.0.0")
        self.assertEqual(seen, [(p.stat().st_ino, stat.S_IFMT(p.stat().st_mode))
                                for p in (payload, nested, incoming, versions)])

    def test_file_walk_refuses_special_files_and_symlinked_roots(self) -> None:
        incoming = self.root / "versions/1.0.0"
        incoming.mkdir(parents=True)
        os.mkfifo(incoming / "fifo")
        with mock.patch.object(su.sys, "platform", "darwin"):
            with self.assertRaisesRegex(su.UpdateError, "links were left as they are"):
                su.sync_bundle(self.root, "1.0.0")
            (incoming / "fifo").unlink()
            incoming.rmdir()
            incoming.symlink_to(self.tmp, target_is_directory=True)
            with self.assertRaisesRegex(su.UpdateError, "links were left as they are"):
                su.sync_bundle(self.root, "1.0.0")


class SafetyTests(SelfUpdateTestCase):
    """The running gateway's installation, the inhibition and the incoming pin."""

    def setUp(self) -> None:
        super().setUp()
        self.seed(self.release("1.0.0", gateway=GATEWAY_A))
        self.write_state("4\n")

    def test_the_running_gateways_version_is_kept_and_unknown_keeps_everything(self) -> None:
        running = lambda: frozenset({"1.0.0"})  # noqa: E731
        self.update_to(self.release("1.0.1"), protected=running)
        applied = self.update_to(self.release("1.0.2"), protected=running)
        self.assertEqual((applied.removed, applied.kept, applied.kept_unknown), ((), ("1.0.0",), False))
        self.assertEqual(self.versions(), ["1.0.0", "1.0.1", "1.0.2"])
        applied = self.update_to(self.release("1.0.3"), protected=lambda: None)
        self.assertEqual((applied.removed, applied.kept_unknown), ((), True))
        self.assertEqual(self.versions(), ["1.0.0", "1.0.1", "1.0.2", "1.0.3"])
        applied = self.update_to(self.release("1.0.4"))
        self.assertEqual(applied.removed, ("1.0.0", "1.0.1", "1.0.2"))

    def test_a_protected_version_is_never_replaced_in_place(self) -> None:
        stale = self.release("1.0.1")
        self.update_to(stale)
        (self.root / "current").unlink()
        (self.root / "current").symlink_to("versions/1.0.0")  # 1.0.1 left over beside current
        for protected in (lambda: frozenset({"1.0.1"}), lambda: None):
            with self.subTest(protected=protected):
                with self.assertRaisesRegex(su.UpdateError, "not replaced in place; nothing was changed"):
                    self.update_to(stale, protected=protected)
                self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        self.assertEqual(self.update_to(stale).version, "1.0.1")

    def test_the_inhibition_wraps_the_switch(self) -> None:
        events = []

        @contextlib.contextmanager
        def recorded():
            events.append("begin")
            yield
            events.append("end")

        self.update_to(self.release("1.0.1"), inhibit=recorded)
        self.assertEqual(events, ["begin", "end"])

        class Phases:
            def __init__(self) -> None:
                self.phases: list[tuple[str, str]] = []

            def advance(self, phase: str) -> None:
                current = os.readlink(root / "current")
                self.phases.append((phase, current))

        root = self.root
        held = Phases()

        @contextlib.contextmanager
        def phased():
            yield held

        self.update_to(self.release("1.0.9", claude="9.9.9"), inhibit=phased, acquire_pin=lambda *_a: None)
        # Every step is a phase, recorded before it changes anything.
        self.assertEqual(held.phases, [("download", "versions/1.0.1"), ("pin", "versions/1.0.1"),
                                       ("replace", "versions/1.0.1"), ("switch", "versions/1.0.1"),
                                       ("prune", "versions/1.0.9")])
        held.phases.clear()
        roll_back(self.root, state_root=self.state, hold=clear, inhibit=phased)
        self.assertEqual(held.phases, [("switch", "versions/1.0.9")])
        roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)

        @contextlib.contextmanager
        def refused():
            raise su.UpdateError("the gateway is inhibited by installer; nothing was changed")
            yield  # pragma: no cover

        with self.assertRaisesRegex(su.UpdateError, "inhibited"):
            self.update_to(self.release("1.1.0"), inhibit=refused)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.9")
        with self.assertRaisesRegex(su.UpdateError, "inhibited"):
            roll_back(self.root, state_root=self.state, hold=clear, inhibit=refused)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.9")

    def test_the_copies_are_pruned_for_the_new_current_after_the_switch(self) -> None:
        calls = []

        def prune(bundle: Path, release: su.Release) -> list[str]:
            calls.append((bundle, release.version, os.readlink(self.root / "current")))
            return ["Kept Claude Code copies in use: 2.1.100"]

        applied = self.update_to(self.release("1.0.1"), prune_copies=prune)
        self.assertEqual(calls, [(self.root / "versions/1.0.1", "1.0.1", "versions/1.0.1")])
        self.assertEqual(applied.copies, ("Kept Claude Code copies in use: 2.1.100",))

    def test_tidy_finishes_what_an_interrupted_run_left(self) -> None:
        stale = self.release("1.0.1")
        self.update_to(stale)
        (self.root / "current").unlink()
        (self.root / "current").symlink_to("versions/1.0.0")

        # The switch fails after 1.0.1 was moved aside: an interrupted run.
        real_rename = os.rename

        def failing(src, dst):
            if Path(dst).name == "1.0.1" and ".staging." in str(src):
                raise OSError("disk full")
            return real_rename(src, dst)

        with mock.patch.object(su.os, "rename", side_effect=failing), self.assertRaises(OSError):
            self.update_to(stale)
        versions = self.root / "versions"
        self.assertFalse((versions / "1.0.1").exists())
        aside = [p.name for p in versions.iterdir() if p.name.startswith(".replaced.1.0.1.")]
        self.assertEqual(len(aside), 1)
        (self.root / ".downloads").mkdir()
        (versions / ".staging.4242").mkdir()
        os.symlink("versions/1.0.0", self.root / ".current.tmp.4242")
        self.assertEqual(su.tidy(self.root), ("1.0.1",))
        self.assertEqual(self.versions(), ["1.0.0", "1.0.1"])
        self.assertFalse((self.root / ".downloads").exists())
        self.assertFalse(os.path.lexists(self.root / ".current.tmp.4242"))
        self.assertEqual(su.tidy(self.root), ())  # idempotent
        self.assertEqual(su.read_installation(self.root).current.version, "1.0.0")

    def test_the_incoming_pin_is_acquired_before_the_switch(self) -> None:
        calls = []

        def acquire(bundle: Path, release: su.Release) -> None:
            calls.append((bundle.name, release.version, release.claude_code))
            self.assertNotEqual(os.readlink(self.root / "current"), f"versions/{release.version}")  # not switched yet
            self.assertTrue((bundle / "MANIFEST.json").is_file())

        self.update_to(self.release("1.0.1"), acquire_pin=acquire)  # the same pin: nothing to acquire
        self.assertEqual(calls, [])
        self.update_to(self.release("1.1.0", claude="9.9.9"), acquire_pin=acquire)
        self.assertEqual(calls, [("charmer-1.1.0-linux-x86_64", "1.1.0", "9.9.9")])

        def unavailable(_bundle: Path, _release: su.Release) -> None:
            raise su.UpdateError("Claude Code 9.9.10 is not in place; nothing was changed")

        with self.assertRaisesRegex(su.UpdateError, "not in place"):
            self.update_to(self.release("1.2.0", claude="9.9.10"), acquire_pin=unavailable)
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.1.0")
        self.assertEqual(self.versions(), ["1.0.1", "1.1.0"])

    def test_the_plan_sizes_the_incoming_pin(self) -> None:
        release = self.release("1.1.0", claude="9.9.9",
                               claude_platforms={"linux-x64": {"sha256": "d" * 64, "size": 3 * 1024 * 1024}})
        installation = su.read_installation(self.root)
        result = su.check(installation, su.DirectoryTransport(release.dir), accept)
        planned = su.plan(installation, result.release, state=4, platform="linux-x64")
        self.assertEqual(planned.claude_size, 3 * 1024 * 1024)
        self.assertIn("downloaded (3.0 MiB)", "\n".join(planned.steps))

    def test_installed_pins_follow_the_links_not_the_version_order(self) -> None:
        home = self.tmp / "home"
        root = su.install_root({"HOME": str(home)})
        self.root = root
        self.seed(self.release("1.0.0", claude="2.0.0"))
        self.update_to(self.release("1.1.0", claude="2.1.0"))
        roll_back(root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        pins = su.installed_pins({"HOME": str(home)})
        self.assertEqual(set(pins), {"2.0.0", "2.1.0"})  # current 1.0.0 and previous 1.1.0 (newer)
        self.update_to(self.release("1.2.0", claude="2.2.0"), protected=lambda: frozenset({"1.0.0"}))
        pins = su.installed_pins({"HOME": str(home)}, protected=lambda _root: frozenset({"1.0.0"}))
        self.assertEqual(set(pins), {"2.2.0", "2.0.0"})
        with self.assertRaisesRegex(su.UpdateError, "cannot be checked"):
            su.installed_pins({"HOME": str(home)}, protected=lambda _root: None)
        self.assertEqual(su.installed_pins({"HOME": str(self.tmp / "nobody")}), {})


class RollbackTests(SelfUpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed(self.release("1.0.0", gateway=GATEWAY_A))
        self.write_state("4\n")

    def test_rollback_and_forward_again(self) -> None:
        self.update_to(self.release("1.0.1", gateway=GATEWAY_B))
        back = roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        self.assertEqual((back.version, back.previous, back.restart), ("1.0.0", "1.0.1", "gateway"))
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.0.0")
        # Explicit order: previous is the version current before the switch
        # (the newer one here), so the next rollback undoes this one.
        self.assertEqual(os.readlink(self.root / "previous"), "versions/1.0.1")
        again = roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        self.assertEqual(again.version, "1.0.1")

    def test_rollback_refuses_after_a_state_migration(self) -> None:
        self.update_to(self.release("1.1.0", state_format=5))
        self.write_state("5\n")
        with self.assertRaises(su.UpdateError) as caught:
            roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        self.assertIn("upgraded to format 5", str(caught.exception))
        self.assertEqual(caught.exception.remedy, "stay on 1.1.0; state is only ever migrated forward")
        self.assertEqual(os.readlink(self.root / "current"), "versions/1.1.0")

    def links(self) -> tuple[str, str | None]:
        previous = self.root / "previous"
        return os.readlink(self.root / "current"), os.readlink(previous) if previous.is_symlink() else None

    def stop_between_the_links(self):
        """A switch that dies after ``previous`` changed, before ``current`` does."""

        real = su._flip

        def flip(root: Path, name: str, version: str) -> None:
            if name == su.CURRENT:
                raise OSError(5, "Input/output error")
            real(root, name, version)

        return mock.patch.object(su, "_flip", side_effect=flip)

    def test_a_switch_stopped_between_its_links_is_put_back(self) -> None:
        self.update_to(self.release("1.0.1", gateway=GATEWAY_B))
        before = self.links()
        self.assertEqual(before, ("versions/1.0.1", "versions/1.0.0"))
        with self.stop_between_the_links(), self.assertRaises(OSError):
            roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        # Both links on one version, as the stopped run left them, but recorded.
        self.assertEqual(self.links(), ("versions/1.0.1", "versions/1.0.1"))
        record = json.loads((self.root / su.SWITCH_RECORD).read_text())
        self.assertEqual(record["before"], {"current": "versions/1.0.1", "previous": "versions/1.0.0"})
        self.assertEqual(record["after"], {"current": "versions/1.0.0", "previous": "versions/1.0.1"})
        with self.assertRaisesRegex(su.UpdateError, "interrupted switch .* is not finished"):
            roll_back(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext)
        self.assertIn("as they were before an interrupted switch (current 1.0.1, previous 1.0.0)",
                      su.finish_switch(self.root))
        self.assertEqual(self.links(), before)
        self.assertFalse(os.path.lexists(self.root / su.SWITCH_RECORD))
        self.assertIsNone(su.finish_switch(self.root))  # idempotent
        self.assertEqual(roll_back(self.root, state_root=self.state, hold=clear,
                                   inhibit=contextlib.nullcontext).version, "1.0.0")
        # An update stopped the same way comes back the same way.
        with self.stop_between_the_links(), self.assertRaises(OSError):
            self.update_to(self.release("1.0.2"))
        self.assertEqual(self.links(), ("versions/1.0.0", "versions/1.0.0"))
        su.finish_switch(self.root)
        self.assertEqual(self.links(), ("versions/1.0.0", "versions/1.0.1"))
        self.assertEqual(self.update_to(self.release("1.0.2")).previous, "1.0.0")

    def test_an_unreadable_switch_record_changes_nothing(self) -> None:
        self.update_to(self.release("1.0.1", gateway=GATEWAY_B))
        record = self.root / su.SWITCH_RECORD
        for text in ("{not json", json.dumps({"format": 1, "before": {"current": "versions/1.0.0"}})):
            with self.subTest(text=text):
                record.write_text(text)
                with self.assertRaises(su.UpdateError) as caught:
                    su.finish_switch(self.root)
                self.assertIn("the links were left as they are", str(caught.exception))
                self.assertIn(str(record), caught.exception.remedy)
                self.assertEqual(self.links(), ("versions/1.0.1", "versions/1.0.0"))
                self.assertTrue(record.is_file())

    def test_a_rollback_is_the_one_confirmed(self) -> None:
        self.update_to(self.release("1.0.1", gateway=GATEWAY_B))
        confirmed = su.read_installation(self.root)  # 1.0.1, previous 1.0.0
        self.update_to(self.release("1.0.2"))  # meanwhile: 1.0.2, previous 1.0.1
        for shown in (confirmed, None):
            with self.subTest(confirmed=shown):
                with self.assertRaises(su.UpdateError) as caught:
                    su.rollback(self.root, state_root=self.state, hold=clear, inhibit=contextlib.nullcontext,
                                confirmed=shown)
                self.assertIn("the installation changed since the rollback was confirmed", str(caught.exception))
                self.assertIn("it is now 1.0.2 -> 1.0.1", str(caught.exception))
                self.assertIn("update --rollback again", caught.exception.remedy)
                self.assertEqual(self.links(), ("versions/1.0.2", "versions/1.0.1"))

    def test_the_previous_gateway_is_named_only_when_a_rollback_restores_another(self) -> None:
        self.assertIsNone(su.rollback_restores_gateway(self.root, self.state))  # no previous version
        self.update_to(self.release("1.0.1", gateway=GATEWAY_B))
        self.assertEqual(su.rollback_restores_gateway(self.root, self.state), "1.0.0")
        self.update_to(self.release("1.0.2", gateway=GATEWAY_B))  # previous 1.0.1: the same gateway
        self.assertIsNone(su.rollback_restores_gateway(self.root, self.state))
        self.assertIsNone(su.rollback_restores_gateway(self.tmp / "nowhere", self.state))

    def test_no_previous_gateway_once_the_state_moved_past_it(self) -> None:
        self.update_to(self.release("1.1.0", gateway=GATEWAY_B, state_format=5))
        self.assertEqual(su.rollback_restores_gateway(self.root, self.state), "1.0.0")
        self.write_state("5\n")
        self.assertIsNone(su.rollback_restores_gateway(self.root, self.state))
        self.write_state("damaged")
        self.assertIsNone(su.rollback_restores_gateway(self.root, self.state))

    def test_rollback_refusals(self) -> None:
        safe = {"hold": clear, "inhibit": contextlib.nullcontext}
        with self.assertRaisesRegex(su.UpdateError, "no previous version"):
            roll_back(self.root, state_root=self.state, **safe)
        self.update_to(self.release("1.0.1", gateway=GATEWAY_B))
        with self.assertRaisesRegex(su.UpdateError, "flush pending; nothing was changed"):
            roll_back(self.root, state_root=self.state, hold=lambda: "flush pending", inhibit=contextlib.nullcontext)
        with self.assertRaisesRegex(su.UpdateError, "nothing is installed"):
            roll_back(self.tmp / "nowhere", state_root=self.state, **safe)


@requires_release_tree
@needs_ssh_keygen()
class InstallerLayoutTests(SelfUpdateTestCase):
    """install.sh and the core read and write one layout."""

    def test_installer_then_update_then_installer_rollback(self) -> None:
        keys = self.tmp / "keys"
        keys.mkdir()
        key = make_key(keys)
        allowed = keys / "allowed_signers"
        allowed.write_text(keygen_signers_line(key))
        home = self.tmp / "home"
        home.mkdir()
        first = FakeRelease(self.tmp / "r1", "1.0.0", gateway=GATEWAY_A)
        first.sign(key)
        fakebin = self.tmp / "fakebin"
        fakebin.mkdir()
        (fakebin / "uname").write_text('#!/bin/sh\ncase $1 in -m) echo x86_64;; -r) echo 6.1.0;; *) echo Linux;; esac\n')
        os.chmod(fakebin / "uname", 0o755)
        env = {"HOME": str(home), "PATH": f"{fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "LC_ALL": "C",
               "TMPDIR": str(self.tmp)}
        run = lambda *args: subprocess.run(["sh", str(INSTALL_SH), *args], env=env, capture_output=True, stdin=subprocess.DEVNULL,  # noqa: E731
                                           text=True, timeout=120, start_new_session=True, cwd=self.tmp)
        installed = run("--from-dir", str(first.dir), "--allowed-signers", str(allowed), "--no-setup", "--no-modify-path")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        root = su.install_root({"HOME": str(home)})
        state_root = home / ".local/state/charmer"
        installation = su.read_installation(root)
        self.assertEqual(installation.current.version, "1.0.0")
        second = FakeRelease(self.tmp / "r2", "1.0.1", gateway=GATEWAY_B)
        second.sign(key)
        transport = su.DirectoryTransport(second.dir)
        result = su.check(installation, transport, su.signature_verifier(allowed.read_text()))
        planned = su.plan(installation, result.release, state=su.state_version(state_root))
        su.apply(installation, result.release, planned, transport, state_root=state_root, **SAFE)
        launcher = home / ".local/bin/charmer"
        version = subprocess.run([str(launcher)], env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout.strip()
        self.assertEqual(version, "charmer 1.0.1")
        rolled = run("--rollback")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        after = su.read_installation(root)
        self.assertEqual((after.current.version, after.previous.version), ("1.0.0", "1.0.1"))
        self.assertEqual(roll_back(root, state_root=state_root, hold=clear,
                                     inhibit=contextlib.nullcontext).version, "1.0.1")


if __name__ == "__main__":
    unittest.main()
