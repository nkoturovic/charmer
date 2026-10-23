"""Management key: file-only lifecycle, installation boundary and secret hygiene."""

import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from charmer import management as m, proxy, state


class KeySlotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.directory = m.key_dir(self.home)
        state.ensure_private_dir(self.directory)
        self.active = self.directory / m.KEY_FILE
        self.staged = self.directory / m.STAGED_FILE
        self.marker = self.directory / m.DISABLED_FILE
        self.selection = self.directory / m.PREPARED_FILE

    def install(self):
        self.assertEqual(m.prepare_start(self.home, stopped=True), ("promoted",))
        key = m.read_active(self.home)
        self.assertIsNotNone(key)
        return key

    def test_truth_table_and_every_command_transition(self):
        self.assertEqual(m.key_dir(self.home), proxy.config_dir(self.home))
        self.assertEqual(m.key_state(self.home), m.KeyState("absent", None, False, None, False))
        self.assertIsNone(m.read_active(self.home))
        self.assertEqual(m.ensure(self.home), "created")
        first = self.staged.read_bytes()
        self.assertRegex(first.decode(), r"^[0-9a-f]{64}\n$")
        self.assertEqual(len(first), 65)
        self.assertEqual(stat.S_IMODE(self.staged.stat().st_mode), 0o600)
        self.assertTrue(m.key_state(self.home).pending)
        self.assertEqual(m.ensure(self.home), "pending")
        self.assertEqual(self.staged.read_bytes(), first)
        self.assertIsNone(m.read_active(self.home))
        a = self.install()
        self.assertEqual((a + "\n").encode(), first)
        self.assertEqual(m.ensure(self.home), "present")
        self.assertFalse(m.key_state(self.home).pending)
        self.assertEqual(m.stage_rotation(self.home), "staged")
        second = self.staged.read_bytes()
        self.assertEqual(m.read_active(self.home), a)
        self.assertEqual(m.ensure(self.home), "present")
        self.assertEqual(m.stage_rotation(self.home), "staged")
        self.assertNotEqual(self.staged.read_bytes(), second)
        b = self.install()
        self.assertNotEqual(a, b)
        self.assertFalse(self.staged.exists())
        m.disable(self.home)
        self.assertTrue(m.key_state(self.home).disabled)
        self.assertEqual(m.ensure(self.home), "disabled")
        self.assertEqual(m.prepare_start(self.home, stopped=True), ("disabled",))
        self.assertIsNone(m.read_active(self.home))
        m.disable(self.home)
        self.assertFalse(self.active.exists())
        self.assertFalse(self.staged.exists())
        self.assertEqual(self.marker.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        self.assertEqual(m.stage_rotation(self.home), "created")
        self.assertFalse(self.marker.exists())
        self.assertTrue(m.key_state(self.home).pending)
        self.assertEqual(m.ensure(self.home), "pending")
        self.assertIsNone(m.read_active(self.home))
        self.install()

    def test_absent_rotation_and_disable_pending(self):
        self.assertEqual(m.stage_rotation(self.home), "created")
        pending = self.staged.read_bytes()
        self.assertEqual(m.stage_rotation(self.home), "created")
        self.assertNotEqual(self.staged.read_bytes(), pending)
        m.disable(self.home)
        self.assertFalse(self.staged.exists())
        self.assertIsNone(m.read_active(self.home))

    def test_disable_reenable_deleted_active_and_repeated_independent_reads(self):
        a = self.install()
        m.disable(self.home)
        m.stage_rotation(self.home)
        # Separate invocations have no shared cache: no wrong key is returned.
        for _ in range(8):
            self.assertIsNone(m.read_active(self.home))
            self.assertIsNone(m.prepare_for_exec(self.home)[0])
        self.assertFalse(self.active.exists())
        b = self.install()
        self.assertNotEqual(a, b)
        self.active.unlink()
        m.ensure(self.home)
        for _ in range(8):
            self.assertIsNone(m.read_active(self.home))
        self.assertNotEqual(self.install(), b)

    def test_unconfirmed_boundary_invalidates_selection_without_promoting(self):
        a = self.install()
        m.stage_rotation(self.home)
        b = self.staged.read_bytes()
        self.assertEqual(m.prepare_start(self.home, stopped=False), ("start-unconfirmed",))
        self.assertEqual(self.active.read_text().strip(), a)
        self.assertEqual(self.staged.read_bytes(), b)
        for _ in range(8):
            self.assertIsNone(m.read_active(self.home))
        self.assertEqual((self.install() + "\n").encode(), b)

    def test_marker_wins_even_over_slots_left_by_interrupted_disable(self):
        self.install()
        m.stage_rotation(self.home)
        state.atomic_write(self.marker, b"")
        before = self.active.read_bytes(), self.staged.read_bytes()
        self.assertIsNone(m.read_active(self.home))
        self.assertEqual(m.prepare_start(self.home, stopped=True), ("disabled",))
        self.assertEqual((self.active.read_bytes(), self.staged.read_bytes()), before)
        m.stage_rotation(self.home)
        self.assertFalse(self.active.exists())
        self.assertIsNone(m.read_active(self.home))
        self.install()

    def test_invalid_active_and_staged_never_overwritten_by_ensure_or_start(self):
        a = self.install()
        state.atomic_write(self.staged, b"private-not-a-key")
        self.assertEqual(m.prepare_start(self.home, stopped=True), ("staged-unusable",))
        self.assertEqual(m.read_active(self.home), a)
        self.assertEqual(self.staged.read_bytes(), b"private-not-a-key")
        state.atomic_write(self.active, b"private-invalid-active")
        with self.assertRaisesRegex(m.ManagementKeyError, "invalid shape"):
            m.ensure(self.home)
        with self.assertRaisesRegex(m.ManagementKeyError, "invalid shape"):
            m.stage_rotation(self.home)
        self.assertEqual(m.prepare_start(self.home, stopped=True), ("unusable:invalid shape",))
        self.assertEqual(self.active.read_bytes(), b"private-invalid-active")

    def test_private_reads_reject_unsafe_paths_modes_and_shape_without_values(self):
        target = self.home / "target"
        target.write_text("DO-NOT-PRINT")
        for kind in ("symlink", "mode", "directory", "shape", "non-ascii"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.active.symlink_to(target)
                elif kind == "directory":
                    self.active.mkdir()
                else:
                    state.atomic_write(self.active, b"\xff" if kind == "non-ascii" else b"DO-NOT-PRINT")
                    if kind == "mode":
                        self.active.chmod(0o644)
                with self.assertRaises(m.ManagementKeyError) as caught:
                    m.read_active(self.home)
                self.assertNotIn("DO-NOT-PRINT", str(caught.exception))
                self.assertNotIn("DO-NOT-PRINT", repr(m.key_state(self.home)))
                self.assertEqual(m.key_state(self.home).active, "unusable")
                if kind == "directory":
                    self.active.rmdir()
                else:
                    self.active.unlink()
        self.assertEqual(target.read_text(), "DO-NOT-PRINT")

    def test_selection_is_optional_private_and_content_bound(self):
        key = self.install()
        self.selection.unlink()
        self.assertIsNone(m.read_active(self.home))
        self.assertTrue(m.key_state(self.home).pending)
        m.prepare_start(self.home, stopped=True)
        state.atomic_write(self.active, ("b" * 64 + "\n").encode())
        self.assertIsNone(m.read_active(self.home))
        self.assertNotIn(key, self.selection.read_text())
        self.selection.chmod(0o644)
        with self.assertRaises(m.ManagementKeyError):
            m.read_active(self.home)
        self.assertIsNone(m.prepare_for_exec(self.home)[0])

    def test_no_leaks_in_state_notes_or_errors(self):
        key = self.install()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertNotIn(key, repr(m.key_state(self.home)))
            self.assertNotIn(key, repr(m.prepare_start(self.home, stopped=True)))
            m.ensure(self.home)
            m.stage_rotation(self.home)
            m.disable(self.home)
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_lock_blocks_rotation_and_readers_take_no_locks(self):
        self.install()
        started, finished = threading.Event(), threading.Event()
        failures = []
        def rotate():
            started.set()
            try:
                m.stage_rotation(self.home)
            except BaseException as exc:
                failures.append(type(exc).__name__)
            finally:
                finished.set()
        with state.FileLock(self.active):
            thread = threading.Thread(target=rotate)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.05))
            with mock.patch.object(state.FileLock, "acquire", side_effect=AssertionError("lock")), \
                 mock.patch.object(os, "chmod", side_effect=AssertionError("chmod")), \
                 mock.patch.object(state, "atomic_write", side_effect=AssertionError("write")):
                self.assertIsNotNone(m.prepare_for_exec(self.home)[0])
                m.key_state(self.home)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_interrupted_promotion_is_idempotent_at_every_write_boundary(self):
        class Crash(BaseException):
            pass
        write, remove = state.atomic_write, state.remove_private
        # Invalidate selection, write A, remove N, publish selection.
        for boundary in range(1, 5):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    m.disable(self.home)
                    m.stage_rotation(self.home)
                    candidate = self.staged.read_bytes()
                    counter = 0
                    def step(operation):
                        def wrapped(*args):
                            nonlocal counter
                            counter += 1
                            if counter == boundary and not after:
                                raise Crash()
                            result = operation(*args)
                            if counter == boundary and after:
                                raise Crash()
                            return result
                        return wrapped
                    with mock.patch.object(state, "atomic_write", side_effect=step(write)), \
                         mock.patch.object(state, "remove_private", side_effect=step(remove)):
                        with self.assertRaises(Crash):
                            m.prepare_start(self.home, stopped=True)
                    # At most the selected candidate can escape; never a new
                    # random key from a retry, even if A == N after the crash.
                    value = m.read_active(self.home)
                    self.assertIn(value, (None, candidate.decode().strip()))
                    m.prepare_start(self.home, stopped=True)
                    self.assertEqual(self.active.read_bytes(), candidate)
                    self.assertFalse(self.staged.exists())
                    self.assertEqual(m.read_active(self.home), candidate.decode().strip())


class GateTests(unittest.TestCase):
    def test_exact_build_gate(self):
        nix = {m.CHANNEL_ENV: "nix"}
        for binary, patches, expected in (
            ("", m.ALLOWLIST_PATCH, False), ("/fixture/bin", "", False),
            ("/fixture/bin", m.ALLOWLIST_PATCH + ".other", False),
            ("/fixture/bin", "other," + m.ALLOWLIST_PATCH, True),
            ("/fixture/bin", " other, " + m.ALLOWLIST_PATCH + " ", True),
        ):
            with self.subTest(binary=binary, patches=patches):
                self.assertEqual(m.allowlist_build({**nix, m.PINNED_BIN_ENV: binary, m.PATCHES_ENV: patches}), expected)
        with mock.patch.dict(os.environ, {**nix, m.PINNED_BIN_ENV: "/fixture/bin", m.PATCHES_ENV: m.ALLOWLIST_PATCH}):
            self.assertFalse(m.allowlist_build({}))

    def test_bundle_requires_the_exact_packaged_contract_not_selected_assets(self):
        from _layout import GATEWAY_CONTRACT

        contract = json.loads(GATEWAY_CONTRACT.read_bytes())
        patches = contract["patches"]
        attested = {m.CHANNEL_ENV: "bundle", m.PINNED_BIN_ENV: "/fixture/bin",
                    m.PATCHES_ENV: ",".join(patches), "CHARMER_ASSETS": "/nonexistent/assets"}
        self.assertTrue(m.allowlist_build(attested))
        self.assertTrue(m.management_channel(attested))
        for value in ("", m.ALLOWLIST_PATCH, ",".join(item.split("@")[0] for item in patches),
                      ",".join(reversed(patches)), ",".join(patches[:-1]),
                      ",".join(patches) + ",extra.patch@" + "0" * 64,
                      ",".join(patches).replace("@", "@0", 1)):
            with self.subTest(attestation=value):
                self.assertFalse(m.allowlist_build({**attested, m.PATCHES_ENV: value}))
        for channel in ("source", "", "Bundle", " bundle", "bundle "):
            self.assertFalse(m.allowlist_build({**attested, m.CHANNEL_ENV: channel}))
        for missing in (m.CHANNEL_ENV, m.PINNED_BIN_ENV, m.PATCHES_ENV):
            self.assertFalse(m.allowlist_build({k: v for k, v in attested.items() if k != missing}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(m, "resources_root", return_value=root):
                self.assertFalse(m.allowlist_build(attested))
                path = root / "gateway-contract.json"
                for data in (b"{}", b"not-json", b'{"version": 1, "patches": []}'):
                    path.write_bytes(data)
                    self.assertFalse(m.allowlist_build(attested))
                with self.subTest(contract="matching but without read-only allowlist"):
                    without_allowlist = [item for item in patches
                                         if item.split("@", 1)[0] != m.ALLOWLIST_PATCH]
                    path.write_text(json.dumps({**contract, "patches": without_allowlist}))
                    environ = {**attested, m.PATCHES_ENV: ",".join(without_allowlist)}
                    self.assertFalse(m.allowlist_build(environ))
                    self.assertFalse(m.management_channel(environ))
                # Even a well-formed but different packaged hash withdraws authority.
                contract["patches"][0] = patches[0].split("@")[0] + "@" + "0" * 64
                path.write_text(json.dumps(contract))
                self.assertFalse(m.allowlist_build(attested))

    def test_only_the_exact_packaged_channels_enable_management(self):
        attested = {m.PINNED_BIN_ENV: "/fixture/bin", m.PATCHES_ENV: m.ALLOWLIST_PATCH}
        for channel, expected in ((None, False), ("nix", True), ("bundle", False), ("source", False),
                                  ("", False), ("NIX", False), (" nix", False), ("nix ", False)):
            with self.subTest(channel=channel):
                environ = dict(attested) if channel is None else {**attested, m.CHANNEL_ENV: channel}
                self.assertEqual(m.management_channel(environ), expected)
                self.assertEqual(m.allowlist_build(environ), expected)
        # The channel alone never enables it: the attested patch list is also required.
        self.assertFalse(m.allowlist_build({m.CHANNEL_ENV: "nix", m.PINNED_BIN_ENV: "/fixture/bin"}))
