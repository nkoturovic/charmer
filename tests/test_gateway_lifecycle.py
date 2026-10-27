"""The on-demand gateway lifecycle, its persistence hold and the gateway verbs.

Every observation and effect is a seam: a fake world (lock, stamp, process,
listener, health, spawn, signal), a fixture ``/proc`` tree, stub service-manager
runners and temporary HOMEs. The one real spawn starts a fake gateway on a
test-allocated loopback port; nothing here reaches the developer's gateway,
service manager or state.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _fake_journalctl as fake
import _log_secrets
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from _layout import REPO_ROOT
from charmer import (catalog, cli, endpoint, gateway_events, gateway_hold, gateway_lifecycle as gl,
                          launch, proxy, secret_store, service, state)
import charmer.cli.consent as consent
from charmer.platform import darwin_process, file_log, observation, posix_fs, posix_process

SENTINEL = "charmer-render-0123abcd"
NOW = datetime.datetime(2026, 10, 2, 12, 0, tzinfo=datetime.timezone.utc)
SAVE = ("credential_save_v1 operation=refresh result={result} provider=claude auth_index=0123456789abcdef "
        "credentials_changed=true stage={stage} errno={errno} category={category} bytes=10 size=10 "
        "generation=1 epoch=1")


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def save_line(result: str, *, at: str = "2026-10-02 12:00:00") -> str:
    """One credential-save line as patch 8's emitter writes it (2.0.1's
    journal shape: a failure at Warn from credential_save.go:214, any other
    outcome at Info from :217)."""

    stage, errno_, category = ("write", 28, "errno") if result == "failed" else ("none", 0, "none")
    body = SAVE.format(result=result, stage=stage, errno=errno_, category=category)
    caller = "[warn ] [credential_save.go:214]" if result == "failed" else "[info ] [credential_save.go:217]"
    return f"[{at}] [--------] {caller} {body}"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)


class World:
    """One fake gateway: what the seams observe and what spawn/terminate change."""

    def __init__(self, case: "_Lifecycle") -> None:
        self.case = case
        self.lock: bool | None = False
        self.alive: bool | None = None
        self.listener = "none"
        self.health = 200
        self.models = (200, {SENTINEL})
        self.spawn_code: int | None = 0
        self.spawned: list[dict] = []
        self.terminated: list[int] = []
        self.pid = 4242
        self.exits_on_term = True
        self.on_spawn = self.become_ready
        self.clock = FakeClock()
        self.now = NOW

    def seams(self, **overrides) -> gl.Seams:
        values = dict(
            health_get=lambda _base, _path: self.health,
            listener=self.verdict,
            process=lambda stamp: (stamp.pid, self.alive),
            lock_held=lambda _path: self.lock,
            spawn=self.spawn,
            terminate=self.terminate,
            models_get=lambda _base, _token, _verdict: self.models,
            runner=mock.Mock(side_effect=AssertionError("service manager")),
            journald_start=lambda: NOW - datetime.timedelta(days=30),
            port_probe=lambda _port: True,
            clock=self.clock, sleep=self.clock.sleep, now=lambda: self.now,
            proc_root=self.case.root / "no-proc",
        )
        values.update(overrides)
        return gl.Seams(**values)

    def verdict(self, _base: str, pid: int | None) -> service.OwnerVerdict:
        if self.listener == "foreign":
            return service.OwnerVerdict("foreign", "another user (uid 1234)", uid=1234)
        if self.listener == "ours":
            return (service.OwnerVerdict("ours", "the charmer gateway service", os.getuid(), pid)
                    if pid == self.pid else service.OwnerVerdict("unknown", "unconfirmed"))
        return service.OwnerVerdict(self.listener, "fixture listener")

    def write_stamp(self, instance: str | None) -> None:
        service.write_exec_stamp(self.case.workdir, service.ExecStamp(
            "1.0.0", "signature", self.pid, "/fixture/gateway", NOW.isoformat(),
            "pid:[101]", instance=instance))

    def become_ready(self, instance: str) -> None:
        self.write_stamp(instance)
        self.lock, self.alive, self.listener = True, True, "ours"

    def spawn(self, argv, *, cwd, env, output, timeout=None):
        instance = argv[argv.index("--instance") + 1]
        self.spawned.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "instance": instance,
                             "log": os.readlink(f"/proc/self/fd/{output}")})
        os.write(output, f"charmer-proxy: gateway instance {instance} starting\n".encode())
        if self.spawn_code == 0 and self.on_spawn is not None:
            self.on_spawn(instance)
        return self.spawn_code

    def terminate(self, pid: int) -> bool:
        self.terminated.append(pid)
        if self.exits_on_term:
            self.lock, self.alive, self.listener = False, False, "none"
        return True


class _Lifecycle(unittest.TestCase):
    PUBLISHED_SENTINEL = True  # the fake world publishes no real config.yaml

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-lifecycle-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.state_root = self.root / "state" / "charmer"
        self.workdir = service.ensure_gateway_workdir(self.state_root)
        self.logs = self.workdir / service.GATEWAY_LOGS
        self.port = _free_port()
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port))
        self.document = catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]
        self.world = World(self)
        if self.PUBLISHED_SENTINEL:
            patcher = mock.patch.object(gl, "published_sentinel", return_value=SENTINEL)
            patcher.start()
            self.addCleanup(patcher.stop)
        token_dir = state.ensure_private_dir(self.home / ".config" / "charmer")
        state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii"))

    def gateway(self, *, environ=None, seams=None, platform="linux", **kwargs) -> gl.Gateway:
        env = {"HOME": str(self.home)} if environ is None else environ
        return gl.Gateway(home=self.home, state_root=self.state_root, environ=env,
                          gateway_document=self.document, installation=REPO_ROOT,
                          providers=("claude", "codex"), seams=seams or self.world.seams(),
                          platform=platform, **kwargs)

    def wrapper_env(self, **extra: str) -> dict[str, str]:
        """A runtime environ whose launcher has an installed proxy wrapper beside it."""

        bindir = self.root / "wrapper-bin"
        bindir.mkdir(exist_ok=True)
        for name in ("charmer", "charmer-proxy"):
            (bindir / name).write_text("#!/bin/sh\nexit 1\n")
            (bindir / name).chmod(0o755)
        return {"HOME": str(self.home), "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_CONFIG_HOME": str(self.root / "config"), "TERM": "dumb",
                "CHARMER_HOOK_COMMAND": str(bindir / "charmer"), **extra}

    def running(self, instance: str = "aaaaaaaaaaaaaaaa") -> None:
        self.world.become_ready(instance)

    def history(self, *starts: datetime.datetime) -> None:
        state.atomic_write(self.workdir / gl.START_HISTORY, json.dumps(
            {"version": 1, "starts": [item.isoformat() for item in starts]}).encode())

    def journal(self, *records: observation.LogRecord, coverage: str = "bounded"):
        """A unit-journal seam: the recorded instance's start marker, then ``records``."""

        def read(_unit: str, _since: datetime.datetime) -> observation.LogWindow:
            stamp = service.read_exec_stamp(self.workdir)
            marker = () if stamp is None or stamp.instance is None else (observation.LogRecord(
                service.start_marker_prefix(stamp.instance), datetime.datetime.fromisoformat(stamp.started_at),
                "boot:invocation", "marker"),)
            return observation.LogWindow((*marker, *records), coverage)

        return read

    def log_file(self, nonce: str, lines: list[str], *, started: datetime.datetime = NOW) -> Path:
        descriptor, path = file_log.create_instance_log(self.logs, started, nonce)
        os.write(descriptor, "".join(line + "\n" for line in lines).encode())
        os.close(descriptor)
        return path


class OwnershipTests(_Lifecycle):
    def observe(self) -> gl.Observation:
        return self.gateway().observe()

    def test_the_proof_needs_lock_stamp_process_and_listener(self) -> None:
        self.assertEqual(self.observe().state, gl.STOPPED)  # free lock, no listener
        self.running()
        self.assertEqual(self.observe().state, gl.OURS)
        cases = {
            "listener of another user": (dict(listener="foreign"), gl.FOREIGN),
            "no lock (another release)": (dict(lock=False), gl.UNKNOWN),
            "unobservable lock": (dict(lock=None, listener="none"), gl.UNKNOWN),
            "unprovable process": (dict(alive=None), gl.UNKNOWN),
            "not listening yet": (dict(listener="none"), gl.STARTING),
            "held lock, process gone": (dict(alive=False, listener="none"), gl.UNKNOWN),
            "unknown listener": (dict(listener="unknown"), gl.UNKNOWN),
        }
        for label, (changes, expected) in cases.items():
            self.running()
            for key, value in changes.items():
                setattr(self.world, key, value)
            with self.subTest(label):
                self.assertEqual(self.observe().state, expected)

    def test_a_listener_with_a_free_lock_is_never_stopped(self) -> None:
        self.world.listener = "unknown"
        self.assertEqual(self.observe().state, gl.UNKNOWN)

    def test_a_replaced_start_record_makes_the_sample_unknown(self) -> None:
        self.running()
        original = self.world.verdict

        def replace(base, pid):
            self.world.write_stamp("bbbbbbbbbbbbbbbb")
            return original(base, pid)

        gateway = self.gateway(seams=self.world.seams(listener=replace))
        self.assertEqual(gateway.observe().state, gl.UNKNOWN)

    def test_default_seams_prove_ownership_on_a_fixture_proc_tree(self) -> None:
        proc = self.root / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/pid").symlink_to("pid:[101]")
        binary = self.root / "gateway-binary"
        binary.write_text("fixture gateway")
        pid = 4343
        (proc / str(pid) / "fd").mkdir(parents=True)
        (proc / str(pid) / "exe").symlink_to(binary)
        (proc / str(pid) / "fd" / "3").symlink_to("socket:[777]")
        (proc / "net").mkdir()
        row = f" 0: 0100007F:{self.port:04X} 00000000:0000 0A 0:0 00:0 0 {os.geteuid()} 0 777\n"
        for name in ("tcp", "tcp6"):
            (proc / "net" / name).write_text("sl local_address ...\n" + (row if name == "tcp" else ""))
        service.write_exec_stamp(self.workdir, service.ExecStamp(
            "1.0.0", "signature", pid, str(binary), NOW.isoformat(), "pid:[101]", instance="cccccccccccccccc"))
        held = posix_fs.hold_exclusive(service.instance_lock_path(self.state_root))
        seams = self.world.seams(listener=None, process=None, lock_held=posix_fs.lock_held, proc_root=proc)
        seen = self.gateway(seams=seams).observe()
        self.assertEqual((seen.state, seen.pid, seen.stamp.instance), (gl.OURS, pid, "cccccccccccccccc"))
        (proc / str(pid) / "exe").unlink()
        (proc / str(pid) / "exe").symlink_to(self.root)  # PID reused by another program
        self.assertEqual(self.gateway(seams=seams).observe().state, gl.UNKNOWN)
        os.close(held)
        for name in ("tcp", "tcp6"):
            (proc / "net" / name).write_text("sl local_address ...\n")
        self.assertEqual(self.gateway(seams=seams).observe().state, gl.STOPPED)

    def test_the_lock_probe_sees_another_holder_and_never_keeps_the_lock(self) -> None:
        path = service.instance_lock_path(self.state_root)
        self.assertFalse(posix_fs.lock_held(path))  # absent file: free
        held = posix_fs.hold_exclusive(path)
        self.assertTrue(os.get_inheritable(held))
        self.assertIsNone(posix_fs.hold_exclusive(path))
        self.assertTrue(posix_fs.lock_held(path))
        os.close(held)
        self.assertFalse(posix_fs.lock_held(path))
        again = posix_fs.hold_exclusive(path)
        self.assertIsNotNone(again)
        os.close(again)


class EnsureTests(_Lifecycle):
    def test_a_stopped_gateway_is_spawned_once_and_waited_for(self) -> None:
        outcome = self.gateway().ensure()
        self.assertEqual(outcome.status, "ready", outcome.lines())
        self.assertEqual(len(self.world.spawned), 1)
        spawn = self.world.spawned[0]
        instance = spawn["instance"]
        self.assertRegex(instance, r"^[0-9a-f]{16}$")
        self.assertIn(instance, outcome.message)
        self.assertEqual(spawn["argv"][-7:], ["run", "--prepare-and-exec", "--detach", "--instance", instance,
                                             "--state-root", str(self.state_root)])
        self.assertEqual(spawn["cwd"], str(self.workdir))
        log = Path(spawn["log"])
        self.assertEqual(log.parent, self.logs)
        self.assertEqual(log.name, f"gateway-20261002T120000Z-{instance}.log")
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.gateway().recent_starts()), 1)

    def test_a_gateway_proven_ours_is_reused_without_a_spawn(self) -> None:
        self.running()
        models = mock.Mock(side_effect=AssertionError("reuse needs no models call"))
        outcome = self.gateway(seams=self.world.seams(models_get=models)).ensure()
        self.assertEqual(outcome.status, "ready")
        self.assertEqual(self.world.spawned, [])

    def test_anything_not_proven_ours_is_refused_before_any_token(self) -> None:
        models = mock.Mock(side_effect=AssertionError("token sent"))
        for listener, lock in (("foreign", False), ("unknown", False), ("ours", False)):
            self.world.listener, self.world.lock, self.world.alive = listener, lock, True
            self.world.write_stamp("dddddddddddddddd")
            with self.subTest(listener=listener):
                outcome = self.gateway(seams=self.world.seams(models_get=models)).ensure()
                self.assertEqual(outcome.status, "refused")
                self.assertEqual(outcome.exit_code, 1)
                self.assertIn("no token was sent", outcome.message)
        self.assertEqual(self.world.spawned, [])
        self.world.listener = "foreign"
        self.assertIn(f"free port {self.port}", self.gateway().ensure().remedy)

    def test_a_starting_instance_is_awaited_not_duplicated(self) -> None:
        self.running()
        self.world.listener = "none"
        ticks = []

        def sleep(seconds):
            ticks.append(seconds)
            self.world.clock.sleep(seconds)
            if len(ticks) == 3:
                self.world.listener = "ours"

        outcome = self.gateway(seams=self.world.seams(sleep=sleep)).ensure()
        self.assertEqual(outcome.status, "ready")
        self.assertEqual(self.world.spawned, [])

    def test_a_blocked_start_shows_its_log_tail_redacted(self) -> None:
        pairs = _log_secrets.secrets_and_lines()
        self.log_file("eeeeeeeeeeeeeeee", [line for _secret, line in pairs])
        minute = datetime.timedelta(minutes=1)
        self.history(NOW - 9 * minute, NOW - 5 * minute, NOW - minute)
        outcome = self.gateway().ensure()
        self.assertEqual(outcome.status, "blocked")
        shown = "\n".join(outcome.lines())
        self.assertIn(secret_store.REDACTED, shown)
        for secret, _line in pairs:
            self.assertNotIn(secret, shown)
            self.assertFalse(any(secret in line for line in outcome.log_tail))

    def test_three_starts_in_ten_minutes_block_automatic_starts_with_the_log_tail(self) -> None:
        self.log_file("eeeeeeeeeeeeeeee", ["fatal: bind: address already in use"])
        minute = datetime.timedelta(minutes=1)
        self.history(NOW - 9 * minute, NOW - 5 * minute, NOW - minute)
        outcome = self.gateway().ensure()
        self.assertEqual((outcome.status, outcome.exit_code), ("blocked", 1))
        self.assertIn("3 times in the last 10 minutes", outcome.message)
        self.assertIn("fatal: bind: address already in use", outcome.log_tail)
        self.assertIn("charmer gateway start", outcome.remedy)
        self.assertEqual(self.world.spawned, [])
        # Older starts fall out of the window; an explicit start clears the counter.
        self.history(NOW - 30 * minute, NOW - 5 * minute, NOW - minute)
        self.assertEqual(self.gateway().ensure().status, "ready")
        self.world.lock, self.world.listener, self.world.alive = False, "none", False
        self.history(NOW - 3 * minute, NOW - 2 * minute, NOW - minute)
        self.assertEqual(self.gateway().ensure(explicit=True).status, "ready")
        self.assertEqual(len(self.gateway().recent_starts()), 1)

    def test_a_failed_start_reports_once_and_never_respawns(self) -> None:
        self.world.spawn_code = 1
        outcome = self.gateway().ensure()
        self.assertEqual((outcome.status, outcome.exit_code), ("failed", 1))
        self.assertIn("exited with status 1", outcome.message)
        self.assertTrue(any("starting" in line for line in outcome.log_tail))
        self.world.spawn_code, self.world.on_spawn = 0, None  # detached, then died before exec
        outcome = self.gateway().ensure(explicit=True)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("exited before it started", outcome.message)
        self.world.spawn_code = None
        self.assertIn("did not detach", self.gateway().ensure(explicit=True).message)
        self.assertEqual(len(self.world.spawned), 3)

    def test_readiness_is_bounded_and_names_what_is_missing(self) -> None:
        self.world.on_spawn = lambda instance: (self.world.become_ready(instance),
                                                setattr(self.world, "listener", "none"))
        outcome = self.gateway().ensure(max_wait=3)
        self.assertEqual((outcome.status, outcome.exit_code), ("timeout", 1))
        self.assertIn("not ready after 3 s: the gateway is not listening yet", outcome.message)
        self.world.lock, self.world.listener, self.world.alive = False, "none", False
        self.world.on_spawn = self.world.become_ready
        self.world.models = (200, {"other-alias"})
        outcome = self.gateway().ensure(max_wait=2, explicit=True)
        self.assertIn(f"the render sentinel {SENTINEL} is not served yet", outcome.message)
        self.world.lock, self.world.listener, self.world.alive = False, "none", False
        self.world.health = 503
        self.assertIn("/healthz answered HTTP 503", self.gateway().ensure(max_wait=1, explicit=True).message)
        self.assertEqual(len(self.world.spawned), 3)

    def test_a_busy_instance_lock_reuses_the_other_instance(self) -> None:
        def busy(instance):
            self.world.become_ready("ffffffffffffffff")

        self.world.spawn_code, self.world.on_spawn = gl.BUSY_EXIT, None
        original = self.world.spawn

        def spawn(argv, **kwargs):
            code = original(argv, **kwargs)
            busy(None)
            return code

        outcome = self.gateway(seams=self.world.seams(spawn=spawn)).ensure()
        self.assertEqual(outcome.status, "ready")
        self.assertIn("ffffffffffffffff", outcome.message)

    def test_starters_are_serialized_by_the_start_lock(self) -> None:
        other = state.FileLock(self.workdir / "gateway-start")
        self.assertTrue(other.acquire(blocking=False))
        self.addCleanup(other.release)
        outcome = self.gateway().ensure(max_wait=1)
        self.assertEqual((outcome.status, outcome.exit_code), ("busy", 1))
        self.assertEqual(self.world.spawned, [])

    def test_only_an_explicit_start_records_a_new_install_port(self) -> None:
        key = self.home / ".config/charmer/api-key"

        def fresh_install() -> None:
            endpoint.endpoint_path(self.home).unlink(missing_ok=True)
            key.unlink(missing_ok=True)
            self.world.lock, self.world.listener, self.world.alive = False, "none", False

        def child(instance: str) -> None:  # the started gateway's render creates the key
            state.atomic_write(key, (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii"))
            self.world.become_ready(instance)

        self.world.on_spawn = child
        seams = self.world.seams(port_probe=lambda port: port == 18319)
        fresh_install()
        gateway = self.gateway(seams=seams)
        self.assertIsNone(gateway.config)
        self.assertEqual(gateway.ensure(explicit=True, choose_port=True).status, "ready")
        self.assertEqual(endpoint.read_config(self.home).port, 18319)
        self.assertEqual(gateway.base_url, "http://127.0.0.1:18319")
        # An automatic ensure in a home that was never set up starts nothing
        # (another program may own the packaged port) and records nothing.
        fresh_install()
        spawned = len(self.world.spawned)
        outcome = self.gateway(seams=seams).ensure()
        self.assertEqual(outcome.status, "refused")
        self.assertIn("not set up", outcome.message)
        self.assertEqual(outcome.remedy, f"set it up: {endpoint.SETUP_COMMAND}")
        self.assertEqual(len(self.world.spawned), spawned)
        self.assertFalse(endpoint.endpoint_path(self.home).exists())  # never on an automatic ensure


class SpawnEnvironmentTests(_Lifecycle):
    ENV = {
        "HOME": "/ignored", "PATH": "/opt/bin:/usr/bin", "LANG": "C.UTF-8", "LC_ALL": "C", "LC_TIME": "C",
        "TMPDIR": "/tmp/x", "TZ": "Europe/Belgrade", "SSL_CERT_FILE": "/etc/ca.pem", "SSL_CERT_DIR": "/etc/ca",
        "XDG_STATE_HOME": "/xdg/state", "XDG_CONFIG_HOME": "/xdg/config",
        "HTTPS_PROXY": "http://proxy.invalid:3128", "http_proxy": "http://proxy.invalid:3128",
        "CHARMER_ASSETS": "/assets", "CHARMER_CHANNEL": "bundle", "CHARMER_PROXY_BIN": "/bin/gw",
        "CHARMER_SECRET_ENV": "/secrets.env", "CHARMER_PROXY_PATCHES": "a.patch",
        "CHARMER_MANAGED_ID": "11111111-1111-4111-8111-111111111111", "CHARMER_GATEWAY": "1",
        "MANAGEMENT_PASSWORD": "never", "ANTHROPIC_API_KEY": "never", "CLAUDECODE": "1",
    }

    def test_the_allowlist(self) -> None:
        env = gl.spawn_environment(self.ENV, self.home)
        self.assertEqual(env, {
            "HOME": str(self.home), "PATH": gl.SPAWN_PATH, "TZ": "UTC", "LANG": "C.UTF-8", "LC_ALL": "C",
            "LC_TIME": "C", "TMPDIR": "/tmp/x", "SSL_CERT_FILE": "/etc/ca.pem", "SSL_CERT_DIR": "/etc/ca",
            "CHARMER_ASSETS": "/assets", "CHARMER_CHANNEL": "bundle",
            "CHARMER_PROXY_BIN": "/bin/gw", "CHARMER_SECRET_ENV": "/secrets.env",
        })

    def test_the_state_root_is_explicit_when_xdg_state_home_moves_it(self) -> None:
        xdg = self.root / "xdg-state"
        environ = self.wrapper_env(XDG_STATE_HOME=str(xdg))
        runtime_root = cli.Runtime(asset_root=FIXTURE_ROOT, environ=environ, cwd=self.root,
                                   health_get=lambda _b, _p: 200, managed_root=self.root / "managed",
                                   refresh_shims=False, allow_state_writes=False)
        gateway = runtime_root.gateway(seams=self.world.seams(), state_root=xdg / "charmer")
        self.assertEqual(runtime_root.gateway().state_root, xdg / "charmer")
        argv = gateway.spawn_argv("0123456789abcdef")
        self.assertEqual(argv[argv.index("--state-root") + 1], str(xdg / "charmer"))
        self.assertNotIn("XDG_STATE_HOME", gateway.spawn_env())

    def test_a_script_entry_resolves_the_gateway_binary_on_the_launcher_path(self) -> None:
        bindir = self.root / "bin"
        bindir.mkdir()
        binary = bindir / service.GATEWAY_BINARY
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        env = gl.spawn_environment({"PATH": str(bindir)}, self.home, resolve_binary=True)
        self.assertEqual(env["CHARMER_PROXY_BIN"], str(binary))
        gateway = self.gateway(environ={"HOME": str(self.home), "PATH": str(bindir)})
        command = gateway.spawn_argv("0123456789abcdef")
        self.assertEqual(command[:2], [sys.executable, str(REPO_ROOT / "bin" / "charmer-proxy")])
        self.assertEqual(gateway.spawn_env()["CHARMER_PROXY_BIN"], str(binary))

    def test_an_installed_wrapper_is_used_as_is(self) -> None:
        bindir = self.root / "wrapper-bin"
        bindir.mkdir()
        for name in ("charmer", "charmer-proxy"):
            (bindir / name).write_text("#!/bin/sh\n")
            (bindir / name).chmod(0o755)
        environ = {"HOME": str(self.home), "CHARMER_HOOK_COMMAND": str(bindir / "charmer"),
                   "PATH": str(self.root / "nothing")}
        gateway = self.gateway(environ=environ)
        self.assertEqual(gateway.spawn_argv("0123456789abcdef")[0], str(bindir / "charmer-proxy"))
        self.assertNotIn("CHARMER_PROXY_BIN", gateway.spawn_env())

    def test_the_script_entry_belongs_to_the_installation_never_the_resources(self) -> None:
        # Executables are not resources: a resource override never moves the
        # entry point, and the running installation is the default tree.
        environ = {"HOME": str(self.home), "CHARMER_ASSETS": str(FIXTURE_ROOT)}
        default = gl.Gateway(home=self.home, state_root=self.state_root, environ=environ,
                             gateway_document=self.document, seams=self.world.seams(), platform="linux")
        self.assertEqual(default.installation, REPO_ROOT)
        self.assertEqual(default._proxy_entry(), ([sys.executable, str(REPO_ROOT / "bin" / "charmer-proxy")], True))
        empty = self.root / "empty-installation"
        empty.mkdir()
        bare = gl.Gateway(home=self.home, state_root=self.state_root, environ=environ,
                          gateway_document=self.document, installation=empty, seams=self.world.seams(),
                          platform="linux")
        with self.assertRaisesRegex(gl.LifecycleError, "charmer-proxy was not found"):
            bare._proxy_entry()


class HoldAndStopTests(_Lifecycle):
    def test_stop_signals_only_a_proven_gateway_after_the_hold(self) -> None:
        self.running("1111111111111111")
        self.log_file("1111111111111111", [save_line("failed")])
        outcome = self.gateway().stop()
        self.assertEqual((outcome.status, outcome.exit_code), ("held", 1))
        self.assertIn("a credential save failed and no later save repaired it (claude)", outcome.message)
        self.assertIn("clear-hold", outcome.remedy)
        self.assertEqual(self.world.terminated, [])
        # A later persisted save of the same credential in the same instance repairs it.
        with open(self.logs / f"gateway-20261002T120000Z-1111111111111111.log", "a") as handle:
            handle.write(save_line("persisted", at="2026-10-02 12:00:05") + "\n")
        self.history(NOW)
        outcome = self.gateway().stop()
        self.assertEqual(outcome.status, "stopped", outcome.lines())
        self.assertEqual(self.world.terminated, [self.world.pid])
        self.assertEqual(self.gateway().recent_starts(), [])  # a deliberate stop is no crash
        self.assertEqual(self.gateway().stop().message, "the gateway is not running")
        self.assertEqual(self.world.terminated, [self.world.pid])

    def test_nothing_unproven_is_signalled(self) -> None:
        for listener, lock in (("foreign", True), ("unknown", True), ("ours", False)):
            self.running()
            self.world.listener, self.world.lock = listener, lock
            with self.subTest(listener=listener, lock=lock):
                self.assertEqual(self.gateway().stop().status, "refused")
        self.assertEqual(self.world.terminated, [])

    def test_a_process_that_does_not_exit_is_reported(self) -> None:
        self.running()
        self.world.exits_on_term = False
        outcome = self.gateway().stop(wait=2)
        self.assertEqual(outcome.status, "timeout")
        self.assertIn(f"pid {self.world.pid}", outcome.message)

    def test_clear_hold_records_the_position_and_later_failures_hold_again(self) -> None:
        self.running("2222222222222222")
        path = self.log_file("2222222222222222", ["noise", save_line("failed")])
        gateway = self.gateway()
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertEqual(report.cursors, {"2222222222222222": path.stat().st_size})
        gateway.clear_hold(report)
        self.assertFalse(gateway.persistence_hold().held)
        document = json.loads((self.workdir / gateway_hold.HOLD_FILE).read_text())
        self.assertEqual(document["cleared"], {"2222222222222222": path.stat().st_size})
        with open(path, "a") as handle:
            handle.write(save_line("failed", at="2026-10-02 12:01:00") + "\n")
        self.assertTrue(gateway.persistence_hold().held)

    def test_previous_instances_recorded_holds_and_unreadable_records_veto(self) -> None:
        self.running("3333333333333333")
        self.log_file("4444444444444444", [save_line("failed")],
                      started=NOW - datetime.timedelta(hours=1))
        report = self.gateway().persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("instance 4444444444444444", report.reasons[0])
        self.gateway().clear_hold(report)
        hold_file = self.workdir / gateway_hold.HOLD_FILE
        document = json.loads(hold_file.read_text())
        document["holds"] = [{"instance": "5555555555555555", "reason": "an unresolved save (pruned log)",
                              "recorded_at": NOW.isoformat()}]
        state.atomic_write(hold_file, json.dumps(document).encode())
        self.assertIn("pruned log", self.gateway().persistence_hold().reasons[0])
        state.atomic_write(hold_file, b"{broken")
        self.assertTrue(self.gateway().persistence_hold().held)
        self.assertEqual(self.gateway().stop().status, "held")
        self.assertEqual(self.world.terminated, [])

    def test_a_save_line_without_a_timestamp_or_cut_by_the_budget_holds(self) -> None:
        self.running("6666666666666666")
        self.log_file("6666666666666666", [save_line("failed", at="2026-99-99 99:99:99")])
        self.assertTrue(self.gateway().persistence_hold().held)
        report = gateway_hold.evaluate_files(self.state_root, ("claude",), file_budget=8)
        self.assertTrue(report.held)

    @unittest.skipIf(os.geteuid() == 0, "directory permissions do not bind root")
    def test_logs_that_cannot_be_listed_hold_instead_of_reading_as_none(self) -> None:
        self.running("1212121212121212")
        self.log_file("1212121212121212", [save_line("failed")])
        self.assertTrue(self.gateway().persistence_hold().held)
        os.chmod(self.logs, 0o300)
        self.addCleanup(os.chmod, self.logs, 0o700)
        report = self.gateway().persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("the gateway log directory cannot be listed", report.reasons[0])
        self.assertEqual(self.gateway().stop().status, "held")
        self.assertEqual(self.world.terminated, [])
        self.assertEqual(gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0).removed, ())

    def test_a_log_whose_metadata_cannot_be_read_holds(self) -> None:
        self.running("1313131313131313")
        real = os.scandir

        class Unreadable:
            name = "gateway-20261002T120000Z-1414141414141414.log"
            path = "/nonexistent/" + name

            def stat(self, follow_symlinks=True):
                raise PermissionError(13, "Permission denied")

        def scandir(path):
            return [*real(path), Unreadable()]

        with mock.patch.object(file_log.os, "scandir", side_effect=scandir):
            report = self.gateway().persistence_hold()
            self.assertTrue(report.held)
            self.assertIn("instance 1414141414141414: its log cannot be examined", report.reasons[0])
            self.assertEqual(self.gateway().stop().status, "held")
        self.assertEqual(self.world.terminated, [])

    def test_an_unfinished_save_record_holds_until_it_is_complete_or_cleared(self) -> None:
        self.running("1515151515151515")
        path = self.log_file("1515151515151515", ["charmer-proxy: gateway instance 1515151515151515 starting"])
        with open(path, "a") as handle:  # a full disk cut the failure record short
            handle.write(save_line("failed")[:60])
        gateway = self.gateway()
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("coverage incomplete", report.reasons[0])
        self.assertEqual(report.cursors, {"1515151515151515": path.stat().st_size})
        self.assertEqual(gateway.stop().status, "held")
        self.assertEqual(self.world.terminated, [])
        # The user's clear acknowledges the unfinished bytes too.
        gateway.clear_hold(report)
        self.assertFalse(gateway.persistence_hold().held)
        self.assertEqual(gateway.stop().status, "stopped")

    def test_restart_is_stop_then_an_explicit_start(self) -> None:
        self.running("7777777777777777")
        minute = datetime.timedelta(minutes=1)
        self.history(NOW - 3 * minute, NOW - 2 * minute, NOW - minute)
        outcome = self.gateway().restart()
        self.assertEqual(outcome.status, "ready", outcome.lines())
        self.assertEqual(self.world.terminated, [self.world.pid])
        self.assertEqual(len(self.world.spawned), 1)
        self.world.lock, self.world.listener = True, "ours"
        self.log_file("8888888888888888", [save_line("failed")])
        self.assertEqual(self.gateway().restart().status, "held")
        self.assertEqual(len(self.world.spawned), 1)


class _ServiceBackend(_Lifecycle):
    """The supervised backend with a stub service manager (``runner``)."""

    def setUp(self) -> None:
        super().setUp()
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port, backend="systemd"))
        self.calls: list[list[str]] = []
        self.timeouts: list[tuple[str, float]] = []
        self.active = "inactive"
        self.start_takes = 0.0  # seconds the manager's start call takes (fake clock)
        self.starts_unit = True
        self.after_start = "activating"  # the unit's state after a start that does not come up

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        label = "ActiveState" if "--property=ActiveState" in argv else argv[-2] if argv[0] == "systemctl" else argv[0]
        self.timeouts.append((label, kwargs.get("timeout")))
        if "--property=ActiveState" in argv:
            return subprocess.CompletedProcess(argv, 0, self.active + "\n", "")
        if argv[:2] == ["journalctl", "--user"]:
            return subprocess.CompletedProcess(argv, 0, b"one\ntwo\n", b"")
        if argv[-2] == "start":
            self.world.clock.now += self.start_takes
            self.active = "active" if self.starts_unit else self.after_start
            if self.starts_unit:
                self.world.become_ready("9999999999999999")
        if argv[-2] == "stop":
            self.world.lock, self.world.listener, self.world.alive = False, "none", False
        return subprocess.CompletedProcess(argv, 0, "", "")

    def service_gateway(self, *records: observation.LogRecord) -> gl.Gateway:
        return self.gateway(seams=self.world.seams(runner=self.runner, journal=self.journal(*records)))


class ServiceBackendTests(_ServiceBackend):
    def test_verbs_delegate_to_the_manager_behind_the_same_checks(self) -> None:
        gateway = self.service_gateway()
        self.assertEqual((gateway.backend, gateway.unit), ("systemd", "charmer-gateway"))
        self.assertEqual(gateway.ensure().status, "ready")
        self.assertIn(["systemctl", "--user", "--no-ask-password", "--no-block", "start", "charmer-gateway"],
                      self.calls)
        self.assertEqual(self.world.spawned, [])
        self.active = "active"
        self.assertEqual(gateway.stop().status, "stopped")
        self.assertIn(["systemctl", "--user", "--no-ask-password", "stop", "charmer-gateway"], self.calls)
        self.assertEqual(gateway.logs(lines=2), ["one", "two"])

    def test_the_journal_hold_vetoes_the_service_stop(self) -> None:
        self.world.become_ready("9999999999999999")
        self.active = "active"
        failed = observation.LogRecord(save_line("failed"), NOW, "boot:invocation", "c1")
        gateway = self.service_gateway(failed)
        outcome = gateway.stop()
        self.assertEqual(outcome.status, "held")
        self.assertFalse(any("stop" in call for call in self.calls))
        gateway.clear_hold(gateway.persistence_hold())
        cleared = gateway_hold.read_document(self.state_root)["cleared_through"]
        self.assertIsNotNone(cleared)

    def test_an_unproven_listener_is_refused_before_any_manager_request(self) -> None:
        for listener in ("foreign", "unknown"):
            self.calls.clear()
            self.world.listener = listener
            with self.subTest(listener=listener):
                outcome = self.service_gateway().ensure(max_wait=5)
                self.assertEqual(outcome.status, "refused", outcome.lines())
                self.assertIn("no token was sent", outcome.message)
                self.assertEqual(self.calls, [])  # no manager query, no start
        self.world.listener = "none"
        self.world.become_ready("9999999999999999")  # a running unit gateway is reused
        self.calls.clear()
        self.assertEqual(self.service_gateway().ensure(max_wait=5).status, "ready")
        self.assertEqual(self.calls, [])

    def test_one_deadline_bounds_the_manager_calls_and_readiness(self) -> None:
        self.start_takes = 20.0  # the manager blocks far beyond the helper's bound
        outcome = self.service_gateway().ensure(max_wait=10)
        self.assertEqual(outcome.status, "timeout", outcome.lines())
        self.assertGreater(self.world.clock.now, 10)
        bounds = dict(self.timeouts)
        self.assertLessEqual(bounds["ActiveState"], 10)
        self.assertLessEqual(bounds["start"], 10)
        # A start that is still coming up is left alone and reused by the next call.
        self.assertFalse(any("stop" in call for call in self.calls))
        # Readiness never comes after the deadline: a slow start with no answer yet.
        self.calls.clear()
        self.start_takes, self.starts_unit, self.active = 0.0, False, "inactive"
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        self.world.clock.now = 0.0
        outcome = self.service_gateway().ensure(max_wait=2)
        self.assertEqual(outcome.status, "timeout", outcome.lines())
        self.assertIn("the service is still starting", outcome.message)
        self.assertLessEqual(self.world.clock.now, 2.2)
        # A unit that is no longer on its way up has failed its start.
        self.active, self.after_start = "inactive", "failed"
        self.world.clock.now = 0.0
        self.assertEqual(self.service_gateway().ensure(max_wait=2).status, "failed")

    def test_the_helper_bound_holds_through_the_cli(self) -> None:
        self.start_takes = 11.0
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.wrapper_env(), cwd=self.root,
                              managed_root=self.root / "managed",
                              gateway_seams=self.world.seams(runner=self.runner, journal=self.journal()))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["gateway", "ensure", "--quiet", "--max-wait", "10"], runtime=runtime,
                            output_stream=out, interactive=False)
        self.assertEqual((code, out.getvalue()), (1, ""), err.getvalue())
        self.assertLessEqual(dict(self.timeouts)["start"], 10)

    def test_readiness_is_never_reported_after_the_helper_bound(self) -> None:
        # The manager's start takes 9 s and /healthz then answers after 1.4 s:
        # the answer comes after the 10 s bound, so no token may follow.
        self.start_takes = 9.0

        def slow_health(_base, _path):
            self.world.clock.now += 1.4
            return 200

        seams = self.world.seams(runner=self.runner, journal=self.journal(), health_get=slow_health)
        outcome = self.gateway(seams=seams).ensure(max_wait=10)
        self.assertEqual(outcome.status, "timeout", outcome.lines())
        self.assertIn("answered only after the deadline", outcome.message)
        # Through the token helper's command: nonzero and nothing on stdout.
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        self.active, self.world.clock.now = "inactive", 0.0
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.wrapper_env(), cwd=self.root,
                              managed_root=self.root / "managed", gateway_seams=seams)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["gateway", "ensure", "--quiet", "--max-wait", "10"], runtime=runtime,
                            output_stream=out, interactive=False)
        self.assertNotEqual(code, 0, err.getvalue())
        self.assertEqual(out.getvalue(), "")
        # A gateway that is already ready is reused within the bound.
        self.world.clock.now = 0.0
        self.assertEqual(self.gateway(seams=seams).ensure(max_wait=10).status, "ready")

    def test_a_running_instance_failure_never_ages_out_of_the_journal_hold(self) -> None:
        self.world.become_ready("9999999999999999")  # its stamp says it started at NOW
        self.active = "active"
        self.world.now = NOW + datetime.timedelta(hours=26)
        marker = observation.LogRecord(service.start_marker_prefix("9999999999999999") + " (pid 4242)",
                                       NOW, "boot:invocation", "c0")
        failed = observation.LogRecord(save_line("failed", at="2026-10-02 13:00:00"),
                                       NOW + datetime.timedelta(hours=1), "boot:invocation", "c1")
        records = [marker]
        asked = []

        def journal(_unit, since):
            asked.append(since)
            return observation.LogWindow(tuple(r for r in records if r.timestamp >= since), "bounded")

        gateway = self.gateway(seams=self.world.seams(
            runner=self.runner, journal=journal,
            journal_reaches=lambda _unit, at: any(r.timestamp <= at for r in records)))
        # The start marker proves the window covers the instance.
        self.assertFalse(gateway.persistence_hold().held)
        self.assertEqual(asked[-1], NOW - gateway_hold.START_MARGIN)  # not the last 24 hours
        # Rotated or vacuumed away: the instance is not covered, which holds.
        records.remove(marker)
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("start of the running instance 9999999999999999 is not in the journal", report.reasons[0])
        records[:] = [marker, failed]
        report = gateway.persistence_hold()
        self.assertTrue(report.held, report)
        self.assertEqual(gateway.stop().status, "held")
        self.assertFalse(any("stop" in call for call in self.calls))
        # A clear after the start acknowledges the earlier history; the read
        # still starts at the instance's start (the clear filters what counts).
        gateway.clear_hold(report)
        self.assertFalse(gateway.persistence_hold().held)
        self.assertEqual(asked[-1], NOW - gateway_hold.START_MARGIN)

    def test_a_journal_vacuum_after_a_clear_cannot_unhold_the_running_instance(self) -> None:
        self.world.become_ready("9999999999999999")  # started at NOW
        self.active = "active"
        marker = observation.LogRecord(service.start_marker_prefix("9999999999999999") + " (pid 4242)",
                                       NOW, "boot:invocation", "c0")

        def record(result: str, hours: float, cursor: str) -> observation.LogRecord:
            return observation.LogRecord(save_line(result), NOW + datetime.timedelta(hours=hours),
                                         "boot:invocation", cursor)

        records = [marker, record("failed", 0.5, "c1")]

        def journal(_unit, since):
            return observation.LogWindow(tuple(r for r in records if r.timestamp >= since), "bounded")

        gateway = self.gateway(seams=self.world.seams(
            runner=self.runner, journal=journal,
            journal_reaches=lambda _unit, at: any(r.timestamp <= at for r in records)))
        self.world.now = NOW + datetime.timedelta(hours=1)
        gateway.clear_hold(gateway.persistence_hold())  # the user verified the first failure
        self.assertFalse(gateway.persistence_hold().held)
        # A failure after the clear becomes a recorded hold as soon as it is seen ...
        records.append(record("failed", 2, "c2"))
        self.world.now = NOW + datetime.timedelta(hours=2, minutes=30)
        self.assertTrue(gateway.persistence_hold().held)
        holds = gateway_hold.read_document(self.state_root)["holds"]
        self.assertEqual([item["instance"] for item in holds], ["boot:invocation"])
        self.assertIn("evidence", holds[0])
        # ... and resolves itself while the journal still shows it and its repair.
        records.append(record("persisted", 3, "c3"))
        self.world.now = NOW + datetime.timedelta(hours=3, minutes=30)
        self.assertFalse(gateway.persistence_hold().held)
        self.assertEqual(gateway_hold.read_document(self.state_root)["holds"], [])
        # Another failure is seen, then the journal is vacuumed down to recent ordinary lines.
        records.append(record("failed", 4, "c4"))
        self.world.now = NOW + datetime.timedelta(hours=4, minutes=30)
        self.assertTrue(gateway.persistence_hold().held)
        self.world.now = NOW + datetime.timedelta(hours=28)
        records[:] = [observation.LogRecord("an ordinary line", NOW + datetime.timedelta(hours=27),
                                            "boot:invocation", "c9")]
        report = gateway.persistence_hold()
        self.assertTrue(report.held, report)
        self.assertIn("(claude; service journal)", " ".join(report.reasons))
        self.assertEqual(gateway.stop().status, "held")
        self.assertFalse(any("stop" in call for call in self.calls))
        # A repair the journal no longer pairs with its failure resolves nothing.
        records.append(record("persisted", 27.5, "c10"))
        self.assertTrue(gateway.persistence_hold().held)
        # Only the user's clear of what was shown releases it.
        gateway.clear_hold(gateway.persistence_hold())
        self.assertFalse(gateway.persistence_hold().held)
        self.assertEqual(gateway.stop().status, "stopped")

    def test_an_unsupported_recorded_backend_is_refused(self) -> None:
        with self.assertRaises(endpoint.EndpointError):
            self.gateway(platform="darwin")


class DarwinDeadlineTests(_Lifecycle):
    """macOS ownership observations run ``ps`` and ``lsof``: within an ensure
    they share its one deadline instead of five seconds each."""

    TAKES = 4.9  # what each ps/lsof call takes on a loaded machine

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple[str, float]] = []
        for target, value in (("_tool", lambda name: f"/fixture/{name}"),):
            patcher = mock.patch.object(darwin_process, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(posix_process, "exists", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, argv, **kwargs):
        timeout = kwargs["timeout"]
        self.calls.append((Path(argv[0]).name, timeout))
        if timeout < self.TAKES:
            self.world.clock.now += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        self.world.clock.now += self.TAKES
        if argv[0].endswith("/ps"):
            return subprocess.CompletedProcess(argv, 0, b"/fixture/gateway\n", b"")
        return subprocess.CompletedProcess(argv, 0, f"p{self.world.pid}\nu{os.getuid()}\n".encode(), b"")

    def test_the_observations_share_the_ensure_deadline(self) -> None:
        self.running()
        seams = self.world.seams(listener=None, process=None, runner=self.runner)
        gateway = self.gateway(seams=seams, platform="darwin")
        self.assertEqual(gateway.observe().state, gl.OURS)  # outside an ensure: the tools' own bound
        self.calls.clear()
        self.world.clock.now = 0.0
        outcome = gateway.ensure(max_wait=10)
        self.assertEqual(outcome.status, "timeout", outcome.lines())
        self.assertLessEqual(self.world.clock.now, 10.0)
        self.assertTrue(self.calls)
        spent = 0.0
        for _tool, timeout in self.calls:
            self.assertLessEqual(timeout, 10.0 - spent + 1e-9)
            spent += min(timeout, self.TAKES)
        # With enough time the same gateway is ready, and a zero-wait check's
        # looks share the shortest call bound.
        self.TAKES = 0.1
        self.world.clock.now = 0.0
        self.assertEqual(gateway.ensure(max_wait=10).status, "ready")
        self.calls.clear()
        self.assertEqual(gateway.ensure(max_wait=0).status, "ready")
        spent = 0.0
        for _tool, timeout in self.calls:
            self.assertLessEqual(timeout, gl.MIN_CALL - spent + 1e-9, self.calls)
            spent += min(timeout, self.TAKES)
        # A look that cannot finish within that bound proves nothing.
        self.TAKES, self.world.clock.now = 4.9, 0.0
        self.assertEqual(gateway.ensure(max_wait=0).status, "refused")
        self.assertLessEqual(self.world.clock.now, gl.MIN_CALL + 1e-9)


class LogRedactionTests(_Lifecycle):
    """``charmer gateway logs`` and the TUI log view show every line of
    either backend's log redacted: an instance's log file on demand, the
    unit's journal for the service."""

    def assert_redacted(self, shown: str) -> None:
        self.assertIn(secret_store.REDACTED, shown)
        for secret, _line in _log_secrets.secrets_and_lines():
            self.assertNotIn(secret, shown)
        for line in _log_secrets.ORDINARY:
            self.assertIn(line, shown)

    def lines(self) -> list[str]:
        return [*(line for _secret, line in _log_secrets.secrets_and_lines()), *_log_secrets.ORDINARY]

    def test_an_instance_log_file_is_shown_redacted(self) -> None:
        self.log_file("aaaaaaaaaaaaaaaa", self.lines())
        gateway = self.gateway()
        self.assert_redacted("\n".join(gateway.logs(lines=100)))
        self.assert_redacted("\n".join(gateway.logs(lines=100, instance="aaaaaaaaaaaaaaaa")))

    def test_the_units_journal_is_shown_redacted(self) -> None:
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port, backend="systemd"))
        raw = ("\n".join(self.lines()) + "\n").encode()
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, raw, b"")

        gateway = self.gateway(seams=self.world.seams(runner=runner))
        self.assertEqual(gateway.backend, "systemd")
        self.assert_redacted("\n".join(gateway.logs(lines=100)))
        self.assertEqual(calls[0][:2], ["journalctl", "--user"])
        # A service hand-off that failed carries the journal's tail the same way.
        outcome = gl.Outcome("failed", "the gateway service did not become ready", None,
                             tuple(service.journal_tail("charmer-gateway", lines=100, runner=runner)))
        self.assert_redacted("\n".join(outcome.lines()))

    def assert_clean(self, shown: list[str] | tuple[str, ...], secrets: list[str]) -> None:
        text = "\n".join(shown)
        self.assertIn(secret_store.REDACTED, text)
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("^[", text)

    def test_structured_values_on_either_backend(self) -> None:
        cases = _log_secrets.structured_cases()
        self.check_backends([line for _secrets, line in cases], [s for secrets, _line in cases for s in secrets])

    def test_terminal_escapes_on_either_backend(self) -> None:
        cases = _log_secrets.escape_cases()
        self.check_backends([line for _secrets, line in cases], [s for secrets, _line in cases for s in secrets])

    def test_a_private_key_across_lines_on_either_backend(self) -> None:
        body, lines = _log_secrets.private_key_lines()
        # The last five lines start inside the private key's body.
        self.assertNotIn("PRIVATE KEY-----", "".join(lines[-5:-3]))
        self.check_backends(lines, [piece[:8] for piece in body])

    def journal_gateway(self, lines: list[str]) -> tuple[gl.Gateway, object]:
        """The service backend, with ``lines`` as its unit's journal (the
        records as ``journalctl -o cat`` prints them, one per line feed)."""

        raw = ("\n".join(lines) + "\n").encode()
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port, backend="systemd"))
        runner = lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, raw, b"")  # noqa: E731
        gateway = self.gateway(seams=self.world.seams(runner=runner))
        self.assertEqual(gateway.backend, "systemd")
        return gateway, runner

    def test_a_line_separator_inside_a_journal_record_never_splits_it(self) -> None:
        # A Unicode line separator, a next-line character, a vertical tab or a
        # carriage return between a header's name and its colon (or its value)
        # stays inside the journal record and is removed before matching.
        for secrets, line in _log_secrets.line_break_cases():
            with self.subTest(line=repr(line[len(_log_secrets.STAMP):])):
                gateway, runner = self.journal_gateway([line])
                for count in (1, 100):
                    shown = gateway.logs(lines=count)
                    self.assertEqual(len(shown), 1, shown)
                    self.assert_clean(shown, list(secrets))
                # A failed start's tail read from the journal.
                outcome = gl.Outcome("failed", "the gateway service did not become ready", None,
                                     tuple(service.journal_tail("charmer-gateway", lines=gl.LOG_TAIL_LINES,
                                                                runner=runner)))
                self.assert_clean(outcome.log_tail, list(secrets))
                self.assert_clean(outcome.lines(), list(secrets))

    def assert_no_key(self, shown: list[str] | tuple[str, ...]) -> None:
        text = "\n".join(shown)
        for piece in _log_secrets.SHORT_KEY_PIECES:
            self.assertNotIn(piece, text)

    def check_every_tail(self, lines: list[str], nonce: str) -> None:
        """Both backends' logs, whole and from every line on, and a failed
        start's tail show nothing of the private key in ``lines``."""

        self.log_file(nonce, lines)
        file_backend = self.gateway()
        journal_backend, _runner = self.journal_gateway(lines)
        self.assertEqual(file_backend.backend, endpoint.ON_DEMAND)
        for count in (*range(1, len(lines) + 1), 100):
            for label, gateway, kwargs in (("file", file_backend, {"instance": nonce}),
                                           ("journal", journal_backend, {})):
                with self.subTest(backend=label, lines=count):
                    shown = gateway.logs(lines=count, **kwargs)
                    self.assertEqual(len(shown), min(count, len(lines)))
                    self.assert_no_key(shown)
        outcome = gl.Outcome("failed", "the gateway did not become ready", None, tuple(lines))
        self.assert_no_key(outcome.log_tail)
        self.assert_no_key(outcome.lines())
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port))

    def test_a_private_key_after_a_field_name_on_either_backend(self) -> None:
        # private_key: and then the key's BEGIN marker: its body and END line
        # are redacted too (a short body no long-run rule recognizes).
        for index, lines in enumerate(_log_secrets.field_key_cases()):
            with self.subTest(case=lines[0][len(_log_secrets.STAMP):60]):
                self.check_every_tail(lines, f"{index + 1:016x}")

    def test_an_unfinished_private_key_on_either_backend(self) -> None:
        # The log ends inside a key: a tail of its last body line alone (the
        # BEGIN line above it) shows nothing of it.
        for index, field in enumerate((False, True)):
            with self.subTest(field=field):
                self.check_every_tail(_log_secrets.unfinished_key_lines(field=field), f"{index + 10:016x}")

    def test_a_failed_start_tail_inside_an_unfinished_private_key(self) -> None:
        # The key's BEGIN line is above the lines a failed start shows: they
        # are read with it and show nothing of the key.
        lines = _log_secrets.unfinished_key_lines(gl.LOG_TAIL_LINES + 5, field=True)
        self.log_file("eeeeeeeeeeeeeeee", lines)
        minute = datetime.timedelta(minutes=1)
        self.history(NOW - 9 * minute, NOW - 5 * minute, NOW - minute)
        outcome = self.gateway().ensure()
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.log_tail, (secret_store.REDACTED,) * gl.LOG_TAIL_LINES)
        self.assert_no_key(outcome.lines())
        # The journal's tail of the same log, as the service hand-off reads it.
        _gateway, runner = self.journal_gateway(lines)
        outcome = gl.Outcome("failed", "the gateway service did not become ready", None,
                             tuple(service.journal_tail("charmer-gateway", lines=gl.LOG_TAIL_READ,
                                                        runner=runner)))
        self.assertEqual(outcome.log_tail, (secret_store.REDACTED,) * gl.LOG_TAIL_LINES)

    def check_backends(self, lines: list[str], secrets: list[str]) -> None:
        """Both backends' logs (whole, and a tail of five lines) and a
        failed start's tail show none of ``secrets``."""

        self.log_file("cccccccccccccccc", lines)
        gateway = self.gateway()
        self.assert_clean(gateway.logs(lines=100), secrets)
        self.assert_clean(gateway.logs(lines=5), secrets)
        raw = ("\n".join(lines) + "\n").encode()
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port, backend="systemd"))
        runner = lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, raw, b"")  # noqa: E731
        journal = self.gateway(seams=self.world.seams(runner=runner))
        self.assertEqual(journal.backend, "systemd")
        self.assert_clean(journal.logs(lines=100), secrets)
        self.assert_clean(journal.logs(lines=5), secrets)
        # A failed start's tail, as either backend hands it over.
        for tail in (tuple(lines), tuple(lines[-5:])):
            outcome = gl.Outcome("failed", "the gateway did not become ready", None, tail)
            self.assert_clean(outcome.lines(), secrets)
            self.assert_clean(outcome.log_tail, secrets)
            # Wrapped again (the service hands an outcome on): unchanged.
            again = gl.Outcome(outcome.status, outcome.message, outcome.remedy, outcome.log_tail)
            self.assertEqual(again.log_tail, outcome.log_tail)

    def test_the_command_and_the_tui_view_print_redacted_lines(self) -> None:
        from charmer.cli.screens import gateway_actions

        self.log_file("bbbbbbbbbbbbbbbb", self.lines())
        out, err = io.StringIO(), io.StringIO()
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.wrapper_env(), cwd=self.root,
                              managed_root=self.root / "managed", gateway_seams=self.world.seams())
        with redirect_stderr(err):
            code = cli.main(["gateway", "logs", "-n", "100"], runtime=runtime, input_stream=io.StringIO(),
                            output_stream=out, interactive=False)
        self.assertEqual(code, 0, err.getvalue())
        self.assert_redacted(out.getvalue())
        view = mock.Mock(gateway=self.gateway())
        _title, shown = gateway_actions.log_view(view)
        self.assert_redacted("\n".join(shown))


class FileLogTests(_Lifecycle):
    def test_instance_files_are_private_exclusive_and_listed_newest_first(self) -> None:
        older = self.log_file("aaaaaaaaaaaaaaaa", ["a"], started=NOW - datetime.timedelta(hours=2))
        newer = self.log_file("bbbbbbbbbbbbbbbb", ["b"])
        with self.assertRaises(FileExistsError):
            file_log.create_instance_log(self.logs, NOW, "bbbbbbbbbbbbbbbb")
        (self.logs / "gateway-20261002T130000Z-cccccccccccccccc.log").symlink_to(newer)
        (self.logs / "unrelated.log").write_text("x")
        self.assertEqual([log.path for log in file_log.instance_logs(self.logs)], [newer, older])
        self.assertEqual(file_log.find_instance(self.logs, "aaaaaaaaaaaaaaaa").path, older)
        with self.assertRaises(ValueError):
            file_log.instance_log_name(NOW, "XYZ")

    def test_windows_carry_cursors_utc_times_and_skip_a_partial_line(self) -> None:
        path = self.log_file("dddddddddddddddd", [save_line("failed"), "plain line"])
        with open(path, "a") as handle:
            handle.write("still being written")
        log = file_log.find_instance(self.logs, "dddddddddddddddd")
        window, end = file_log.read_window(log)
        self.assertEqual(window.coverage, "bounded")
        self.assertEqual([record.message for record in window.records], [save_line("failed"), "plain line"])
        first, second = window.records
        self.assertEqual(first.timestamp, NOW)
        self.assertEqual((first.gateway_instance, first.source_cursor), ("dddddddddddddddd", "dddddddddddddddd:0"))
        self.assertIsNone(second.timestamp)
        self.assertEqual(second.source_cursor, f"dddddddddddddddd:{len(save_line('failed')) + 1}")
        self.assertEqual(end, path.stat().st_size - len("still being written"))
        # The hold's strict read: the unfinished line makes the window incomplete.
        evidence = file_log.read_evidence(log)
        self.assertEqual((evidence.window.coverage, evidence.end, evidence.through),
                         ("incomplete", end, path.stat().st_size))
        self.assertEqual(evidence.window.records, window.records)
        later, _ = file_log.read_window(log, start=end)
        self.assertEqual(later.records, ())
        cut, _ = file_log.read_window(log, max_bytes=20)
        self.assertEqual(cut.coverage, "truncated")
        self.assertEqual(file_log.tail_lines(path, lines=2), ["plain line", "still being written"])
        saves = gateway_events.collect_credential_saves(window, providers={"claude"})
        self.assertEqual([event.result for event in saves.events], ["failed"])
        self.assertTrue(gateway_events.recovery_hold(saves))


class CommandTests(_Lifecycle):
    def runtime(self, **kwargs) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.wrapper_env(), cwd=self.root,
                           managed_root=self.root / "managed", gateway_seams=self.world.seams(), **kwargs)

    def run_cli(self, argv, text=""):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(argv, runtime=self.runtime(), input_stream=io.StringIO(text),
                            output_stream=out, interactive=False)
        return code, out.getvalue(), err.getvalue()

    def test_status_logs_and_ensure(self) -> None:
        code, out, _ = self.run_cli(["gateway", "status"])
        self.assertEqual(code, 0)
        self.assertIn("gateway: stopped", out)
        self.assertIn(f"endpoint: http://127.0.0.1:{self.port} (endpoint.json)", out)
        self.assertIn("backend: on-demand", out)
        code, out, err = self.run_cli(["gateway", "ensure", "--quiet"])
        self.assertEqual((code, out, err), (0, "", ""))
        code, out, _ = self.run_cli(["gateway", "logs", "-n", "1"])
        self.assertEqual(code, 0)
        self.assertIn("starting", out)
        code, out, _ = self.run_cli(["gateway", "status"])
        self.assertIn("gateway: ours", out)
        self.assertIn("persistence hold: none", out)

    def test_failures_exit_nonzero_with_empty_stdout(self) -> None:
        self.world.listener = "foreign"
        code, out, err = self.run_cli(["gateway", "ensure", "--quiet", "--max-wait", "1"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("charmer: port", err)
        self.world.listener = "none"
        self.running()
        self.world.listener = "ours"
        self.log_file("aaaaaaaaaaaaaaaa", [save_line("failed")])
        code, out, err = self.run_cli(["gateway", "stop"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not stopped", err)

    def test_public_exit_statuses(self) -> None:
        # Every not-ok outcome is 1 (the message keeps the detail); a read-only
        # run's refusal is 1 as well; an interrupt is 130.
        runtime = self.runtime()
        runtime.allow_state_writes = False
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["gateway", "start"], runtime=runtime, output_stream=out, interactive=False)
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertIn("not available in a read-only run", err.getvalue())
        self.assertEqual(self.world.spawned, [])
        with mock.patch.object(gl.Gateway, "ensure", side_effect=KeyboardInterrupt):
            code, out, err = self.run_cli(["gateway", "start"])
        self.assertEqual((code, out), (130, ""))
        self.assertIn("cancelled", err)

    def test_newer_state_refuses_every_gateway_writer_before_it_writes(self) -> None:
        from charmer import sessions
        from charmer.cli import parser as parser_mod

        parser = parser_mod.build_parser()
        writers = (["gateway", "start"], ["gateway", "stop"], ["gateway", "restart"], ["gateway", "ensure"],
                   ["gateway", "clear-hold"], ["gateway", "service", "install"],
                   ["gateway", "service", "uninstall"], ["setup", "--step", "gateway", "--no-proxy"])
        readers = (["gateway", "status"], ["gateway", "logs"], ["gateway", "service", "status"])
        for argv in writers:
            self.assertTrue(parser_mod._writes_versioned_state(parser.parse_args(argv)), argv)
        for argv in readers:
            self.assertFalse(parser_mod._writes_versioned_state(parser.parse_args(argv)), argv)
        runtime = self.runtime()
        state.atomic_write(runtime.session_store.root / sessions.STATE_MARKER, b"5\n")
        for argv in (["gateway", "ensure", "--quiet"],
                     ["setup", "--step", "gateway", "--proxy", "http://proxy.example.invalid:3128"]):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                code = cli.main(argv, runtime=runtime, output_stream=out, interactive=False)
            with self.subTest(argv=argv):
                self.assertEqual(code, 1)
                self.assertIn("newer charmer", err.getvalue())
        self.assertEqual(self.world.spawned, [])
        self.assertFalse((self.workdir / gl.START_HISTORY).exists())
        self.assertIsNone(endpoint.read_config(self.home).proxy_url)
        # Reading stays available.
        out = io.StringIO()
        self.assertEqual(cli.main(["gateway", "status"], runtime=runtime, output_stream=out, interactive=False), 0)

    def test_clear_hold_needs_a_terminal_and_the_typed_word(self) -> None:
        self.running("aaaaaaaaaaaaaaaa")
        self.log_file("aaaaaaaaaaaaaaaa", [save_line("failed")])
        code, out, err = self.run_cli(["gateway", "clear-hold"], "clear-hold\n")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("needs a terminal", err)
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(consent, "_answer_stream", side_effect=lambda stream: stream):
            code, out, err = self.run_cli(["gateway", "clear-hold"], "yes\n")
            self.assertEqual((code, out.strip()), (3, "not cleared — nothing written"))  # declined
            self.assertIn("Type clear-hold to confirm", err)
            newer = ({"instance": "bbbbbbbbbbbbbbbb", "reason": "a credential save failed (log pruned)",
                      "recorded_at": NOW.isoformat()},)
            with mock.patch.object(gl.Gateway, "clear_hold", return_value=newer):
                code, out, _ = self.run_cli(["gateway", "clear-hold"], "clear-hold\n")
            self.assertEqual(code, 1)
            self.assertIn("evidence recorded after it still holds", out)
            self.assertIn("instance bbbbbbbbbbbbbbbb: a credential save failed (log pruned)", out)
            code, out, _ = self.run_cli(["gateway", "clear-hold"], "clear-hold\n")
            self.assertEqual((code, out.strip()), (0, "persistence hold cleared"))
            code, out, _ = self.run_cli(["gateway", "clear-hold"], "")
            self.assertIn("nothing to clear", out)
        self.assertEqual(self.run_cli(["gateway", "stop"])[0], 0)


class RuntimeEnsureTests(_Lifecycle):
    def runtime(self, **kwargs) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.wrapper_env(), cwd=self.root,
                           managed_root=self.root / "managed", **kwargs)

    def test_launch_readiness_ensures_first(self) -> None:
        runtime = self.runtime(gateway_seams=self.world.seams())
        order = []
        with mock.patch.object(launch, "check_readiness", side_effect=lambda *a, **k: order.append("ready") or "t"), \
                mock.patch.object(launch, "check_listener", side_effect=lambda *a, **k: None):
            runtime._launch_readiness(runtime.catalog.docs["gateway"], home=runtime.home)
        self.assertEqual(len(self.world.spawned), 1)
        self.assertEqual(order, ["ready"])

    def test_a_first_automatic_start_records_the_new_install_port(self) -> None:
        endpoint.endpoint_path(self.home).unlink()
        (self.home / ".config/charmer/api-key").unlink()  # nothing rendered yet: a new install
        packaged = self.document["gateway"]["base_url"]
        token = self.home / ".config/charmer/api-key"

        def prepared(instance: str) -> None:  # the start renders the config and the key
            state.atomic_write(token, (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii"))
            self.world.become_ready(instance)

        self.world.on_spawn = prepared
        runtime = self.runtime(gateway_seams=self.world.seams())
        self.assertEqual(runtime.catalog.docs["gateway"]["gateway"]["base_url"], packaged)
        runtime.ensure_gateway()
        self.assertEqual(len(self.world.spawned), 1)
        chosen = endpoint.read_config(self.home)
        self.assertEqual(chosen.port, endpoint.NEW_INSTALL_PORTS[0])
        # The runtime's own view (scopes, readiness) names the recorded endpoint.
        self.assertEqual(runtime.catalog.docs["gateway"]["gateway"]["base_url"], chosen.base_url)
        # A launch prepared for the old endpoint is refused rather than sent elsewhere.
        stale = {**runtime.catalog.docs["gateway"], "gateway": {**runtime.catalog.docs["gateway"]["gateway"],
                                                                "base_url": packaged}}
        with self.assertRaises(launch.LaunchError) as ctx:
            runtime._launch_readiness(stale, home=runtime.home)
        self.assertIn("endpoint changed after this launch was prepared", str(ctx.exception))

    def test_a_new_install_records_its_port_before_a_launch_is_prepared(self) -> None:
        endpoint.endpoint_path(self.home).unlink()
        (self.home / ".config/charmer/api-key").unlink()
        self.assertFalse(self.runtime(health_get=lambda _b, _p: 200).provision_endpoint())  # a fixture gateway
        self.assertIsNone(endpoint.read_config(self.home))
        self.assertFalse(self.runtime(gateway_seams=self.world.seams(), refresh_shims=False).provision_endpoint())
        self.assertIsNone(endpoint.read_config(self.home))
        runtime = self.runtime(gateway_seams=self.world.seams())
        self.assertTrue(runtime.provision_endpoint())
        self.assertEqual(runtime.catalog.docs["gateway"]["gateway"]["base_url"],
                         endpoint.read_config(self.home).base_url)
        self.assertFalse(runtime.provision_endpoint())  # recorded once
        self.assertEqual(self.world.spawned, [])

    def test_provisioning_rechecks_the_state_root_before_it_writes(self) -> None:
        from charmer import sessions

        endpoint.endpoint_path(self.home).unlink()
        (self.home / ".config/charmer/api-key").unlink()  # a new install
        runtime = self.runtime(gateway_seams=self.world.seams())  # built while the state was ours
        target = cli.LaunchTarget("profile", runtime.profiles.load("balanced"), "balanced", True,
                                  "Profile balanced")
        # A migration holds the state root exclusively: nothing is recorded.
        migration = sessions.migration_lock(self.state_root)
        migration.acquire()
        try:
            with self.assertRaises(launch.LaunchError) as ctx:
                runtime.prepare(target, action="fresh", passthrough=[])
        finally:
            migration.release()
        self.assertIn("the gateway port could not be recorded", str(ctx.exception))
        self.assertIsNone(endpoint.read_config(self.home))
        # A newer release took the state root over after this Runtime was built,
        # or the marker is damaged: refused before any write.
        for marker in (b"5\n", b"four\n"):
            with self.subTest(marker=marker):
                state.atomic_write(self.state_root / sessions.STATE_MARKER, marker)
                with self.assertRaises(sessions.StateMarkerError):
                    runtime.prepare(target, action="fresh", passthrough=[])
                with self.assertRaises(sessions.StateMarkerError):
                    runtime.ensure_gateway()
                self.assertIsNone(endpoint.read_config(self.home))
        self.assertFalse(endpoint.endpoint_path(self.home).exists())
        self.assertEqual(self.world.spawned, [])
        # The state is ours again: the port is recorded.
        state.atomic_write(self.state_root / sessions.STATE_MARKER, b"4\n")
        self.assertTrue(runtime.provision_endpoint())
        self.assertEqual(endpoint.read_config(self.home).port, endpoint.NEW_INSTALL_PORTS[0])

    def test_a_tui_start_refreshes_the_runtime_endpoint(self) -> None:
        from charmer import scope
        from charmer.cli.screens import gateway_actions

        endpoint.endpoint_path(self.home).unlink()
        token = self.home / ".config/charmer/api-key"
        token.unlink()
        self.world.on_spawn = lambda instance: (
            state.atomic_write(token, (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii")),
            self.world.become_ready(instance))
        runtime = self.runtime(gateway_seams=self.world.seams())
        packaged = self.document["gateway"]["base_url"]
        self.assertEqual(scope.catalog_meta_v2(runtime.ordinary_docs).gateway_base_url, packaged)
        view = gateway_actions.observe(runtime)
        self.assertTrue(view.can_start)
        outcome = gateway_actions.act(view, "start")  # the W dialog's start
        self.assertTrue(outcome.ok, outcome.lines())
        chosen = endpoint.read_config(self.home).base_url
        self.assertNotEqual(chosen, packaged)
        self.assertEqual(scope.catalog_meta_v2(runtime.ordinary_docs).gateway_base_url, chosen)
        # A read-only run acts on nothing.
        runtime.allow_state_writes = False
        self.assertEqual(gateway_actions.act(gateway_actions.observe(runtime), "restart").status, "refused")

    def test_injected_fixture_gateways_start_nothing_and_failures_refuse_the_launch(self) -> None:
        self.runtime(health_get=lambda _b, _p: 200).ensure_gateway()
        self.assertEqual(self.world.spawned, [])
        calls = []
        self.runtime(gateway_ensure=calls.append).ensure_gateway()
        self.assertEqual(len(calls), 1)
        self.world.listener = "foreign"
        with self.assertRaises(launch.LaunchError) as ctx:
            self.runtime(gateway_seams=self.world.seams()).ensure_gateway()
        self.assertIn("local gateway: port", str(ctx.exception))
        self.assertIn(f"free port {self.port}", ctx.exception.remedy)


GATEWAY_OLD, GATEWAY_NEW = "a" * 64, "b" * 64
WAY_BACK = gl.ROLLBACK_REMEDY.format(version="2.0.1")


class RollbackRemedyTests(_Lifecycle):
    """A failed start after a gateway-changing update names the way back:
    a fixture installed release (current 2.1.0, previous 2.0.1) under the
    temporary HOME's data root, and a fake gateway that never becomes ready."""

    def environ(self, channel: str | None = "bundle") -> dict[str, str]:
        return self.wrapper_env(**({endpoint.CHANNEL_ENV: channel} if channel else {}))

    def install(self, *, previous_gateway: str = GATEWAY_OLD, previous_state: int = 4) -> Path:
        from _fake_release import FakeRelease
        from charmer import installs

        root = installs.bundle_root(self.environ())
        versions = root / "versions"
        versions.mkdir(parents=True)
        for version, gateway, state_format in (("2.0.1", previous_gateway, previous_state),
                                               ("2.1.0", GATEWAY_NEW, 4)):
            release = FakeRelease(self.root / f"release-{version}", version, gateway=gateway,
                                  state_format=state_format)
            with tarfile.open(release.asset()) as tar:
                tar.extractall(versions, filter="data")
            (versions / f"charmer-{version}-linux-x86_64").rename(versions / version)
        (root / "current").symlink_to("versions/2.1.0")
        (root / "previous").symlink_to("versions/2.0.1")
        state.atomic_write(self.state_root / "state-version", b"4\n")
        return root

    def never_ready(self) -> None:
        self.world.on_spawn = lambda instance: (self.world.become_ready(instance),
                                                setattr(self.world, "listener", "none"))

    def runtime(self, channel: str | None = "bundle", **kwargs) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.environ(channel), cwd=self.root,
                           managed_root=self.root / "managed", gateway_seams=self.world.seams(), **kwargs)

    def run_cli(self, argv, channel: str | None = "bundle"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(argv, runtime=self.runtime(channel), input_stream=io.StringIO(""),
                            output_stream=out, interactive=False)
        return code, out.getvalue(), err.getvalue()

    def test_a_start_that_never_becomes_ready_names_the_rollback(self) -> None:
        self.install()
        self.never_ready()
        code, out, err = self.run_cli(["gateway", "start"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not ready after", err)
        self.assertIn(f"\n  {WAY_BACK}\n", err)
        # The first remedy keeps its own line; the way back is the second.
        lines = err.splitlines()
        fix = next(index for index, line in enumerate(lines) if line.startswith("  fix: "))
        self.assertEqual(lines[fix + 1], f"  {WAY_BACK}")
        self.assertNotIn("charmer update --rollback", lines[fix])

    def test_restart_and_an_early_exit_name_it_too(self) -> None:
        self.install()
        self.never_ready()
        outcome = self.runtime().gateway().restart()
        self.assertEqual((outcome.status, outcome.rollback), ("timeout", WAY_BACK))
        self.world.lock, self.world.listener, self.world.alive = False, "none", False
        self.world.spawn_code = 1
        outcome = self.runtime().gateway().ensure(explicit=True)
        self.assertEqual((outcome.status, outcome.rollback), ("failed", WAY_BACK))
        self.assertIn(f"  {WAY_BACK}", outcome.lines())

    def test_a_launch_refused_by_the_failed_start_carries_it(self) -> None:
        self.install()
        self.world.spawn_code = 1
        with self.assertRaises(launch.LaunchError) as ctx:
            self.runtime().ensure_gateway()
        self.assertIn(WAY_BACK, ctx.exception.remedy)

    def test_nothing_when_the_rollback_would_not_bring_another_gateway_back(self) -> None:
        from charmer import installs

        self.never_ready()
        for label, install in (("same gateway digest", {"previous_gateway": GATEWAY_NEW}),
                               ("state format past previous", {"previous_state": 3})):
            with self.subTest(label):
                shutil.rmtree(installs.bundle_root(self.environ()), ignore_errors=True)
                self.install(**install)
                self.world.lock, self.world.listener, self.world.alive = False, "none", False
                code, _out, err = self.run_cli(["gateway", "start"])
                self.assertEqual(code, 1)
                self.assertIn("not ready after", err)
                self.assertNotIn("charmer update --rollback", err)
        # Another channel: the bundle links on disk are not this charmer's installation.
        shutil.rmtree(installs.bundle_root(self.environ()), ignore_errors=True)
        self.install()
        for channel in ("nix", "source", None):
            with self.subTest(channel=channel):
                self.world.lock, self.world.listener, self.world.alive = False, "none", False
                outcome = self.gateway(environ=self.environ(channel)).ensure(explicit=True)
                self.assertEqual((outcome.status, outcome.rollback), ("timeout", None))
                self.assertFalse(any("charmer update --rollback" in line for line in outcome.lines()))
        self.assertEqual(len(self.world.spawned), 5)

    def test_a_ready_start_reads_no_installation(self) -> None:
        from charmer import self_update

        self.install()
        with mock.patch.object(self_update, "rollback_restores_gateway") as helper:
            outcome = self.runtime().gateway().ensure(explicit=True)
        self.assertEqual((outcome.status, outcome.rollback), ("ready", None))
        helper.assert_not_called()

    def test_the_gateway_dialog_and_doctor_show_it_while_stopped(self) -> None:
        from charmer.cli.screens import gateway_actions

        self.install()
        view = gateway_actions.observe(self.runtime())
        self.assertEqual(view.rollback, WAY_BACK)
        self.assertIsNone(gateway_actions.observe(self.runtime("nix")).rollback)
        problems, _info, _attention = cli._collect_doctor_reports(self.runtime())
        gateway = [line for line in problems if line.startswith("local gateway: ")]
        self.assertEqual(len(gateway), 1, problems)
        self.assertTrue(gateway[0].endswith(f"; {WAY_BACK}"), gateway[0])
        self.assertEqual(getattr(gateway[0], "area", None), "Gateway")
        self.running()
        self.assertIsNone(gateway_actions.observe(self.runtime()).rollback)  # running: nothing to go back for


class MalformedInstanceLogTests(_Lifecycle):
    def test_invalid_calendar_values_are_bounded_scan_problems(self) -> None:
        valid = self.log_file("aaaaaaaaaaaaaaaa", ["valid log"])
        stamps = ("20261302T120000Z", "20260230T120000Z", "20250229T120000Z",
                  "20261002T240000Z", "20261002T126000Z", "20261002T120060Z", "00001002T120000Z")
        expected = []
        for index, stamp in enumerate(stamps):
            nonce = f"{index:016x}"
            path = self.logs / f"gateway-{stamp}-{nonce}.log"
            state.atomic_write(path, b"fixture log\n")
            expected.append(f"instance {nonce}: its log name has an invalid UTC start time")
        logs, problems = file_log.scan_instance_logs(self.logs)
        self.assertEqual([log.path for log in logs], [valid])
        self.assertCountEqual(problems, expected)
        self.assertTrue(all(len(problem) < 100 for problem in problems))
        self.assertEqual(file_log.instance_logs(self.logs), logs)

    def test_invalid_log_date_holds_and_survives_pruning_and_status(self) -> None:
        path = self.logs / "gateway-20261302T120000Z-bbbbbbbbbbbbbbbb.log"
        contents = (save_line("failed") + "\n").encode()
        state.atomic_write(path, contents)
        valid = self.log_file("aaaaaaaaaaaaaaaa", ["valid log"])
        gateway = self.gateway()
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("invalid UTC start time", "\n".join(report.reasons))
        pruned = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0)
        self.assertEqual(pruned.removed, ())
        self.assertIn("invalid UTC start time", pruned.skipped)
        self.assertEqual(path.read_bytes(), contents)
        self.assertTrue(valid.exists())  # the unknown log prevents computing the prune budget
        self.assertIn("invalid UTC start time", "\n".join(gateway.status().lines({"HOME": str(self.home)})))
        outcome = gateway.ensure(max_wait=1)
        self.assertTrue(outcome.ok, outcome.lines())  # starting is allowed; stopping still holds
        self.assertEqual(gateway.stop().status, "held")
        self.assertEqual(self.world.terminated, [])
        self.assertEqual(path.read_bytes(), contents)


class PruneDeadlineTests(_Lifecycle):
    def test_ensure_does_not_record_or_spawn_after_pruning_uses_its_time(self) -> None:
        self.history(NOW - datetime.timedelta(hours=1))
        history = (self.workdir / gl.START_HISTORY).read_bytes()
        for elapsed in (1 - gl.MIN_CALL / 2, 1, 2):
            with self.subTest(elapsed=elapsed):
                self.world.clock.now = 0

                def prune(*_args, **_kwargs):
                    self.world.clock.now += elapsed
                    return gateway_hold.PruneReport()

                with mock.patch.object(gateway_hold, "prune_at_start", side_effect=prune):
                    outcome = self.gateway().ensure(max_wait=1)
                self.assertEqual(self.world.spawned, [])
                self.assertEqual(outcome.status, "timeout", outcome.lines())
                self.assertEqual((self.workdir / gl.START_HISTORY).read_bytes(), history)
                self.assertEqual(list(self.logs.iterdir()), [])

    def test_pruning_gets_the_shorter_deadline_and_the_same_clock(self) -> None:
        for max_wait in (1, gateway_hold.PRUNE_TIMEOUT * 2):
            with self.subTest(max_wait=max_wait):
                self.world.clock.now = 50
                self.world.lock, self.world.alive, self.world.listener = False, False, "none"
                with mock.patch.object(gateway_hold, "prune_at_start", return_value=gateway_hold.PruneReport()) as prune:
                    outcome = self.gateway().ensure(max_wait=max_wait)
                self.assertTrue(outcome.ok, outcome.lines())
                self.assertEqual(prune.call_args.kwargs["deadline"], 50 + min(max_wait, gateway_hold.PRUNE_TIMEOUT))
                self.assertIs(prune.call_args.kwargs["clock"], self.world.clock)

    def test_a_slow_evidence_read_exhausts_ensure_without_losing_the_log(self) -> None:
        path = self.log_file("1111111111111111", ["x" * 300, save_line("failed")])
        before = path.read_bytes()
        read = os.read
        calls = []

        def slow_read(fd, size):
            calls.append(size)
            self.world.clock.now += 0.6
            return read(fd, size)

        with mock.patch.object(gateway_hold, "PRUNE_BUDGET", 0), \
                mock.patch.object(file_log, "_SCAN_CHUNK", 32), \
                mock.patch.object(file_log.os, "read", side_effect=slow_read):
            outcome = self.gateway().ensure(max_wait=1)
        self.assertEqual(self.world.spawned, [])
        self.assertEqual(outcome.status, "timeout", outcome.lines())
        self.assertEqual(len(calls), 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.logs.iterdir()), [path])
        self.assertFalse((self.workdir / gl.START_HISTORY).exists())
        self.assertFalse(gateway_hold.hold_path(self.state_root).exists())
        # A later start with time left still records the failure before pruning.
        with mock.patch.object(gateway_hold, "PRUNE_BUDGET", 0):
            outcome = self.gateway().ensure(max_wait=5)
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertFalse(path.exists())
        self.assertTrue(self.gateway().persistence_hold().held)


class PruneTests(_Lifecycle):
    """Logs are safety evidence: pruning happens only at a start, only for
    previous instances, and only after their unresolved failures became holds."""

    def test_an_enospc_failure_survives_pruning_and_vetoes_every_stop_path(self) -> None:
        old = self.log_file("1111111111111111", ["noise", save_line("failed")],
                            started=NOW - datetime.timedelta(hours=2))
        self.log_file("2222222222222222", ["x" * 100] * 8, started=NOW - datetime.timedelta(hours=1))
        with mock.patch.object(gateway_hold, "PRUNE_BUDGET", 900):
            outcome = self.gateway().ensure(max_wait=5)
        self.assertEqual(outcome.status, "ready", outcome.lines())
        names = {log.nonce for log in file_log.instance_logs(self.logs)}
        self.assertNotIn("1111111111111111", names)  # pruned
        self.assertIn("2222222222222222", names)      # within the budget
        self.assertFalse(old.exists())
        document = json.loads((self.workdir / gateway_hold.HOLD_FILE).read_text())
        (held,) = document["holds"]
        self.assertEqual((held["instance"], held["coverage"]), ("1111111111111111", "bounded"))
        self.assertIn("a credential save failed", held["reason"])
        self.assertIn("log pruned", held["reason"])
        gateway = self.gateway()
        report = gateway.persistence_hold()  # what update and uninstall evaluate first
        self.assertTrue(report.held)
        self.assertIn("instance 1111111111111111", report.reasons[0])
        self.assertEqual(gateway.stop().status, "held")
        self.assertEqual(gateway.restart().status, "held")
        self.assertEqual(self.world.terminated, [])
        gateway.clear_hold(report)
        self.assertEqual(gateway.stop().status, "stopped")

    def test_the_running_instance_and_an_unreadable_hold_record_are_never_pruned(self) -> None:
        self.log_file("3333333333333333", ["x" * 200] * 4, started=NOW - datetime.timedelta(hours=2))
        self.log_file("4444444444444444", ["y" * 200] * 4, started=NOW - datetime.timedelta(hours=1))
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), keep={"3333333333333333"}, budget=10)
        self.assertEqual(report.removed, ("4444444444444444",))
        self.assertTrue(file_log.find_instance(self.logs, "3333333333333333"))
        self.log_file("5555555555555555", ["z" * 200] * 4, started=NOW - datetime.timedelta(minutes=30))
        state.atomic_write(self.workdir / gateway_hold.HOLD_FILE, b"{broken")
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=10)
        self.assertEqual(report.removed, ())
        self.assertIn("invalid JSON", report.skipped)
        self.assertEqual(len(file_log.instance_logs(self.logs)), 2)

    def test_an_incomplete_evaluation_is_persisted_with_its_coverage(self) -> None:
        self.log_file("6666666666666666", ["x" * 300, save_line("failed")],
                      started=NOW - datetime.timedelta(hours=1))
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0, file_budget=64)
        self.assertEqual((report.removed, report.persisted), (("6666666666666666",), ("6666666666666666",)))
        (held,) = json.loads((self.workdir / gateway_hold.HOLD_FILE).read_text())["holds"]
        self.assertEqual(held["coverage"], "truncated")
        self.assertIn("coverage truncated", gateway_hold.evaluate_files(self.state_root, ("claude",)).reasons[0])

    def test_a_clear_keeps_evidence_recorded_after_its_report(self) -> None:
        self.running("aaaaaaaaaaaaaaaa")
        path = self.log_file("aaaaaaaaaaaaaaaa", [save_line("failed")])
        gateway = self.gateway()
        shown = gateway.persistence_hold()  # the report the user is confirming
        self.assertTrue(shown.held)
        # Meanwhile another credential fails, the instance exits and a start prunes its log.
        with open(path, "a") as handle:
            handle.write(save_line("failed", at="2026-10-02 12:05:00").replace(
                "auth_index=0123456789abcdef", "auth_index=fedcba9876543210") + "\n")
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        with mock.patch.object(gateway_hold, "PRUNE_BUDGET", 0):
            self.assertEqual(self.gateway().ensure(max_wait=5).status, "ready")
        self.assertFalse(path.exists())
        newer = gateway.clear_hold(shown)
        self.assertEqual([item["instance"] for item in newer], ["aaaaaaaaaaaaaaaa"])
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("log pruned", report.reasons[0])
        self.assertEqual(self.gateway().stop().status, "held")
        self.assertEqual(self.world.terminated, [])
        # Clearing the report that shows it lifts it.
        self.assertEqual(gateway.clear_hold(report), ())
        self.assertFalse(gateway.persistence_hold().held)

    def test_an_unfinished_record_in_a_pruned_log_becomes_an_incomplete_hold(self) -> None:
        path = self.log_file("6767676767676767", ["noise"], started=NOW - datetime.timedelta(hours=1))
        with open(path, "a") as handle:
            handle.write(save_line("failed")[:60])
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0)
        self.assertEqual((report.removed, report.persisted), (("6767676767676767",), ("6767676767676767",)))
        (held,) = json.loads((self.workdir / gateway_hold.HOLD_FILE).read_text())["holds"]
        self.assertEqual(held["coverage"], "incomplete")
        self.assertTrue(gateway_hold.evaluate_files(self.state_root, ("claude",)).held)

    def test_a_cleared_log_is_pruned_without_a_hold_and_a_repeat_adds_none(self) -> None:
        path = self.log_file("7777777777777777", [save_line("failed")], started=NOW - datetime.timedelta(hours=1))
        gateway = self.gateway()
        gateway.clear_hold(gateway.persistence_hold())
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0)
        self.assertEqual((report.removed, report.persisted), (("7777777777777777",), ()))
        self.assertFalse(path.exists())
        document = json.loads((self.workdir / gateway_hold.HOLD_FILE).read_text())
        self.assertEqual((document["holds"], document["cleared"]), ([], {}))

    def test_recent_logs_feed_one_chronological_window(self) -> None:
        older = self.log_file("8888888888888888", ["[2026-10-02 10:00:00] [--------] [info ] [a.go:1] older"],
                              started=NOW - datetime.timedelta(hours=3))
        newer = self.log_file("9999999999999999", ["[2026-10-02 11:30:00] [--------] [info ] [a.go:1] newer",
                                                   "no envelope"], started=NOW - datetime.timedelta(hours=1))
        for path, moment in ((older, NOW - datetime.timedelta(hours=2)), (newer, NOW)):
            os.utime(path, (moment.timestamp(), moment.timestamp()))
        stale = self.log_file("7777777777777777", ["[2026-09-01 10:00:00] [--------] [info ] [a.go:1] stale"],
                              started=NOW - datetime.timedelta(days=30))
        os.utime(stale, ((NOW - datetime.timedelta(days=29)).timestamp(),) * 2)
        window = file_log.read_recent(self.logs, since=NOW - datetime.timedelta(hours=24))
        self.assertEqual([r.message.split()[-1] for r in window.records], ["older", "newer", "envelope"])
        self.assertEqual(window.coverage, "bounded")
        recent = file_log.read_recent(self.logs, since=datetime.datetime(2026, 10, 2, 11, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual([r.message.split()[-1] for r in recent.records], ["newer", "envelope"])
        self.assertEqual(file_log.read_recent(self.logs, since=NOW - datetime.timedelta(hours=24),
                                              max_bytes=1).coverage, "truncated")
        self.assertEqual(file_log.since_instant("-90m", now=NOW), NOW - datetime.timedelta(minutes=90))
        self.assertEqual(file_log.since_instant("yesterday", now=NOW), NOW - datetime.timedelta(hours=24))


class TokenHelperTests(_Lifecycle):
    """The apiKeyHelper prints the token only for a gateway proven ours and ready."""

    def runtime(self, **kwargs) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.wrapper_env(), cwd=self.root,
                           managed_root=self.root / "managed", gateway_seams=self.world.seams(), **kwargs)

    def ensure(self, max_wait: str = "10"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["gateway", "ensure", "--quiet", "--max-wait", max_wait], runtime=self.runtime(),
                            output_stream=out, interactive=False)
        return code, out.getvalue(), err.getvalue()

    def test_unowned_listener_crash_loop_and_slow_start_exit_nonzero_with_empty_stdout(self) -> None:
        self.world.listener = "unknown"
        self.assertEqual(self.ensure()[:2], (1, ""))
        self.world.listener = "none"
        self.history(NOW - datetime.timedelta(minutes=1), NOW - datetime.timedelta(minutes=2),
                     NOW - datetime.timedelta(minutes=3))
        code, out, err = self.ensure()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("automatic starts are paused", err)
        self.assertEqual(self.world.spawned, [])
        self.history()
        ready_at = 5.0
        clock = self.world.clock
        self.world.on_spawn = lambda instance: self.world.write_stamp(instance) or setattr(self.world, "lock", True)
        original = self.world.verdict

        def slow(base, pid):
            if clock.now >= ready_at:
                self.world.alive, self.world.listener = True, "ours"
            return original(base, pid)

        self.world.seams = (lambda make: (lambda **o: make(listener=slow, process=lambda stamp: (
            stamp.pid, True if self.world.lock else None), **o)))(self.world.seams)
        code, out, err = self.ensure("2")
        self.assertEqual((code, out), (1, ""))  # the start outlasted the helper's bound
        self.assertIn("not ready after 2 s", err)
        code, out, err = self.ensure()
        self.assertEqual((code, out, err), (0, "", ""))  # the same instance, now ready: no second spawn
        self.assertEqual(len(self.world.spawned), 1)

    def test_a_session_compiled_for_another_endpoint_gets_no_token(self) -> None:
        self.running()
        old = f"http://127.0.0.1:{self.port + 1}"  # the session still sends to the port it was compiled for
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["gateway", "ensure", "--quiet", "--max-wait", "10", "--base-url", old],
                            runtime=self.runtime(), output_stream=out, interactive=False)
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertIn(f"this session sends to {old}", err.getvalue())
        self.assertIn("resume it", err.getvalue())
        self.assertEqual(self.world.spawned, [])
        # The session compiled for the configured endpoint is served.
        with redirect_stderr(io.StringIO()):
            code = cli.main(["gateway", "ensure", "--quiet", "--base-url", f"http://127.0.0.1:{self.port}/"],
                            runtime=self.runtime(), output_stream=out, interactive=False)
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_concurrent_helpers_share_one_start(self) -> None:
        import threading

        def slow_ready(instance):
            time.sleep(0.3)
            self.world.become_ready(instance)

        self.world.on_spawn = slow_ready
        seams = self.world.seams(clock=time.monotonic, sleep=time.sleep)
        results = []

        def helper():
            results.append(self.gateway(seams=seams).ensure(max_wait=10).status)

        threads = [threading.Thread(target=helper) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results, ["ready"] * 3)
        self.assertEqual(len(self.world.spawned), 1)

    def test_the_shim_prints_the_token_only_after_a_successful_ensure(self) -> None:
        from charmer import scope

        token = self.home / ".config/charmer/api-key"
        record = self.root / "launcher-calls"
        launcher = self.root / "launcher dir" / "charmer"
        launcher.parent.mkdir()
        launcher.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{record}"\necho noise\nexit "$HELPER_EXIT"\n')
        launcher.chmod(0o755)
        state_root = self.root / "state root with space"
        shim = scope.ensure_gateway_token_shim(state_root, token, str(launcher))
        setting = scope.token_helper_setting(shim)
        self.assertEqual(shlex.split(setting), [str(shim)])
        path = os.environ.get("PATH") or os.defpath  # the helper's `cat` (the sandbox has no /bin/cat)
        for code, expected in (("0", FIXTURE_GATEWAY_TOKEN + "\n"), ("3", ""), ("4", ""), ("6", "")):
            with self.subTest(exit=code):
                result = subprocess.run(["sh", "-c", setting], capture_output=True, text=True, timeout=10,
                                        env={"PATH": path, "HELPER_EXIT": code})
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.returncode, int(code))
        self.assertEqual(set(record.read_text().splitlines()), {"gateway ensure --quiet --max-wait 10"})
        # Inside a session the client's environment names its destination; the helper passes it on.
        record.unlink()
        result = subprocess.run(["sh", "-c", setting], capture_output=True, text=True, timeout=10,
                                env={"PATH": path, "HELPER_EXIT": "3",
                                     "ANTHROPIC_BASE_URL": "http://127.0.0.1:18317"})
        self.assertEqual((result.returncode, result.stdout), (3, ""))
        self.assertEqual(record.read_text().splitlines(),
                         ["gateway ensure --quiet --max-wait 10 --base-url http://127.0.0.1:18317"])
        missing = scope.ensure_gateway_token_shim(self.root / "other", token, str(self.root / "absent"))
        result = subprocess.run([str(missing)], capture_output=True, text=True, timeout=10,
                                env={"PATH": "/nonexistent"})
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("no charmer launcher found", result.stderr)

    def test_a_spaced_state_root_is_quoted_in_the_compiled_helper(self) -> None:
        environ = {**self.wrapper_env(), "XDG_STATE_HOME": str(self.root / "state home")}
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=environ, cwd=self.root,
                              managed_root=self.root / "managed")
        shim = Path(environ["XDG_STATE_HOME"]) / "charmer" / "bin" / "charmer-gateway-token"
        self.assertEqual(runtime.token_helper_command, shlex.quote(str(shim)))
        self.assertIn(shlex.quote(environ["CHARMER_HOOK_COMMAND"]), shim.read_text())

    @unittest.skipUnless(sys.platform.startswith("linux"), "the listener proof reads /proc")
    def test_the_real_helper_refuses_an_unowned_listener(self) -> None:
        """The installed shim and the real launcher against a socket this test holds."""

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        home = self.root / "real-home"
        home.mkdir(mode=0o700)
        token_dir = state.ensure_private_dir(home / ".config" / "charmer")
        state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii"))
        endpoint.write_config(home, endpoint.EndpointConfig(port=server.getsockname()[1]))
        # The real launcher behind an executable wrapper (its own `#!/usr/bin/env`
        # line has no interpreter inside the build sandbox).
        launcher = self.root / "real-bin" / "charmer"
        launcher.parent.mkdir()
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} '
                            f'{shlex.quote(str(REPO_ROOT / "bin" / "charmer"))} "$@"\n')
        launcher.chmod(0o755)
        environ = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                   "XDG_STATE_HOME": str(self.root / "real-state"), "XDG_CONFIG_HOME": str(self.root / "real-config"),
                   "CHARMER_HOOK_COMMAND": str(launcher)}
        prepared = subprocess.run([str(launcher), "gateway", "status"], capture_output=True,
                                  text=True, timeout=60, env=environ, cwd=self.root)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertIn("gateway: unknown", prepared.stdout)
        shim = self.root / "real-state" / "charmer" / "bin" / "charmer-gateway-token"
        # A report never installs the shims; a writable launcher run does.
        self.assertFalse(shim.exists())
        cli.Runtime(asset_root=FIXTURE_ROOT, environ=environ, cwd=self.root, managed_root=self.root / "managed")
        self.assertTrue(shim.is_file())
        result = subprocess.run(["sh", "-c", shlex.quote(str(shim))], capture_output=True, text=True,
                                timeout=60, env=environ, cwd=self.root)
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertIn("no token was sent", result.stderr)


class StatusFactsTests(_Lifecycle):
    def test_status_names_supervision_confinement_log_size_and_binary_drift(self) -> None:
        self.running("aaaaaaaaaaaaaaaa")
        self.log_file("aaaaaaaaaaaaaaaa", ["line"])
        binary = self.root / "cli-proxy-api"
        binary.write_bytes(b"fixture previous gateway")
        installed = self.root / "new-cli-proxy-api"
        installed.write_bytes(b"fixture changed gateway")
        environ = {"HOME": str(self.home), "CHARMER_PROXY_BIN": str(installed)}
        service.write_exec_stamp(self.workdir, service.ExecStamp(
            "1.0.0", "signature", self.world.pid, str(binary), NOW.isoformat(), "pid:[101]",
            instance="aaaaaaaaaaaaaaaa"))
        gateway = self.gateway(environ=environ)
        lines = "\n".join(gateway.status().lines(environ))
        self.assertIn("supervision: none (on-demand) · confinement: none (on-demand)", lines)
        self.assertIn("1 instance log(s), 5 bytes in all", lines)
        self.assertIn(f"gateway restart pending: the running gateway runs {binary}", lines)
        self.assertEqual(gateway.running_binary(), str(binary))
        environ["CHARMER_PROXY_BIN"] = str(binary)
        self.assertIsNone(self.gateway(environ=environ).binary_drift(gateway.observe()))
        binary.unlink()
        self.assertIn("no longer installed", self.gateway(environ=environ).binary_drift(gateway.observe()))

    def test_a_state_root_on_a_windows_drive_is_refused_under_wsl(self) -> None:
        seams = self.world.seams(filesystem=lambda _root: "9p")
        environ = {"HOME": str(self.home), "WSL_DISTRO_NAME": "Ubuntu"}
        outcome = self.gateway(environ=environ, seams=seams).ensure(max_wait=1)
        self.assertEqual(outcome.status, "refused")
        self.assertIn("Windows drive (9p)", outcome.message)
        self.assertEqual(self.world.spawned, [])
        ext4 = self.world.seams(filesystem=lambda _root: "ext4")
        self.assertEqual(self.gateway(environ=environ, seams=ext4).ensure(max_wait=5).status, "ready")
        self.assertIsNone(self.gateway(environ={"HOME": str(self.home)}, seams=seams).state_root_refusal())


@unittest.skipUnless(sys.platform.startswith("linux"), "the process proof reads /proc")
class RealSpawnTests(_Lifecycle):
    """One real start: the proxy entry, detach, the lock across exec and a guarded stop."""

    FAKE = textwrap.dedent('''\
        #!{python}
        import http.server, re, signal, sys
        config = sys.argv[sys.argv.index("--config") + 1]
        text = open(config).read()
        port = int(re.search(r"^port: ([0-9]+)", text, re.M).group(1))
        aliases = re.findall(r'alias: "([^"]+)"', text)
        print("fake gateway listening", flush=True)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"ok" if self.path == "/healthz" else (
                    '{{"data": [' + ",".join('{{"id": "%s"}}' % a for a in aliases) + ']}}').encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))
        try:
            server.serve_forever(poll_interval=0.01)
        finally:
            server.server_close()
        ''')

    PUBLISHED_SENTINEL = False

    def setUp(self) -> None:
        super().setUp()
        (self.home / ".config/charmer/api-key").unlink()
        self.fake = self.root / "fake-gateway"
        self.fake.write_text(self.FAKE.format(python=sys.executable))
        self.fake.chmod(0o755)
        self.addCleanup(self._kill_leftover)

    def _kill_leftover(self) -> None:
        stamp = service.read_exec_stamp(self.workdir)
        if stamp is not None and posix_process.exists(stamp.pid):
            os.kill(stamp.pid, signal.SIGKILL)

    def _listener(self, _base: str, pid: int | None) -> service.OwnerVerdict:
        # The host socket table is off limits to tests: probe the test port instead.
        probe = socket.socket()
        try:
            probe.settimeout(1)
            probe.connect(("127.0.0.1", self.port))
        except OSError:
            return service.OwnerVerdict("none", "nothing listens")
        finally:
            probe.close()
        if pid is not None and posix_process.exists(pid):
            return service.OwnerVerdict("ours", "the spawned fixture gateway", os.getuid(), pid)
        return service.OwnerVerdict("unknown", "unconfirmed")

    def test_start_reuse_and_guarded_stop(self) -> None:
        code = ("import os, sys; sys.path.insert(0, {src!r}); from charmer.proxy import main; "
                "raise SystemExit(main(sys.argv[1:], chdir=os.chdir))").format(src=str(REPO_ROOT / "src"))
        environ = {
            "HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8",
            "CHARMER_ASSETS": str(FIXTURE_ROOT), "CHARMER_PROXY_BIN": str(self.fake),
            "CHARMER_SECRET_ENV": str(self.root / "absent.env"),
            "HTTPS_PROXY": "http://proxy.invalid:3128",
        }
        seams = gl.Seams(listener=self._listener,
                         process=lambda stamp: (stamp.pid, posix_process.exists(stamp.pid)))
        gateway = gl.Gateway(home=self.home, state_root=self.state_root, environ=environ,
                             gateway_document=self.document, providers=("claude",), seams=seams,
                             proxy_command=[sys.executable, "-c", code])
        started = time.monotonic()
        outcome = gateway.ensure(max_wait=30)
        self.assertEqual(outcome.status, "ready", outcome.lines())
        self.assertLess(time.monotonic() - started, 30)
        stamp = service.read_exec_stamp(self.workdir)
        self.assertNotEqual(stamp.pid, os.getpid())
        self.assertEqual(Path(os.readlink(f"/proc/{stamp.pid}/cwd")), self.workdir)
        self.assertTrue(posix_fs.lock_held(service.instance_lock_path(self.state_root)))
        (log,) = file_log.instance_logs(self.logs)
        self.assertEqual(log.nonce, stamp.instance)
        text = log.path.read_text()
        self.assertIn(f"gateway instance {stamp.instance} starting (pid {stamp.pid}, port {self.port}", text)
        self.assertIn("fake gateway listening", text)
        environ_text = Path(f"/proc/{stamp.pid}/environ").read_bytes().split(b"\0")
        names = {item.split(b"=", 1)[0] for item in environ_text if item}
        self.assertIn(b"TZ", names)
        self.assertNotIn(b"HTTPS_PROXY", names)
        self.assertEqual(gateway.ensure(max_wait=5).status, "ready")
        self.assertEqual(len(file_log.instance_logs(self.logs)), 1)  # reused, not respawned
        stopped = gateway.stop(wait=10)
        self.assertEqual(stopped.status, "stopped", stopped.lines())
        self.assertFalse(posix_fs.lock_held(service.instance_lock_path(self.state_root)))
        self.assertEqual(gateway.observe().state, gl.STOPPED)


# ---------------------------------------------------------------- hold coverage over a whole run

def micros(moment: datetime.datetime) -> int:
    return int(moment.timestamp() * 1_000_000)


def save_at(at: datetime.datetime, result: str = "persisted", *, provider: str = "claude",
            category: str | None = None, generation: int = 1) -> str:
    """A credential-save line of patch 8's emitter at ``at``."""

    line = save_line(result, at=f"{at:%Y-%m-%d %H:%M:%S}").replace("provider=claude", f"provider={provider}")
    line = line.replace("generation=1 ", f"generation={generation} ")
    return line.replace("category=none", f"category={category}") if category else line


def request_at(at: datetime.datetime, n: int) -> str:
    """An ordinary access-log line (about 575 bytes of journal JSON)."""

    return (f'[{at:%Y-%m-%d %H:%M:%S}] [{n:08x}] [info ] [gin_logger.go:92] 200 |  1.234s |  127.0.0.1 | '
            f'POST "/v1/messages?beta=true" | model=charmer-opus-high[1m] ' + "r" * 290)


# The operator's measured week: 36 claude refreshes, one codex refresh, an
# unverified (no_sync) and a skipped (stale_generation) claude save among
# them, and later persisted ones.
WEEK_SAVES = (*(("persisted", "claude", None),) * 12, ("unverified", "claude", "no_sync"),
              *(("persisted", "claude", None),) * 12, ("skipped", "claude", "stale_generation"),
              ("persisted", "codex", None), *(("persisted", "claude", None),) * 12)


def week_long_entries(started: datetime.datetime, nonce: str, *, unit: str = "charmer-gateway.service",
                      marker=service.start_marker_prefix, requests: int = 5000) -> list[dict]:
    """A week of one instance's journal: its start line, ``requests``
    ordinary request lines and the 39 saves of :data:`WEEK_SAVES`."""

    span = datetime.timedelta(days=7) - datetime.timedelta(minutes=5)
    entries = [fake.entry(marker(nonce) + " (pid 4242, port 8317, launcher 2.0.1)",
                          micros(started + datetime.timedelta(milliseconds=500)), unit=unit, cursor="s=week;start")]
    for n in range(requests):
        at = started + datetime.timedelta(seconds=1) + span * n / requests
        entries.append(fake.entry(request_at(at, n), micros(at), unit=unit, cursor=f"s=week;r={n}"))
    for index, (result, provider, category) in enumerate(WEEK_SAVES):
        at = started + datetime.timedelta(seconds=7) + span * index / len(WEEK_SAVES)
        entries.append(fake.entry(save_at(at, result, provider=provider, category=category, generation=index + 1),
                                  micros(at), unit=unit, cursor=f"s=week;s={index}"))
    return entries


class ServiceHoldScaleTests(_ServiceBackend):
    """The supervised hold over a long run, read through a fake journalctl
    alone on PATH with the production seams (no journal seam): the host's
    journalctl is never run."""

    NONCE = "0123456789abcdef"

    def setUp(self) -> None:
        super().setUp()
        self.bindir = self.root / "fake-bin"
        self.bindir.mkdir()
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bindir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.system = [fake.system_entry(micros(NOW - datetime.timedelta(minutes=5)))]
        self.log_path: Path | None = None

    def run_instance(self, started: datetime.datetime) -> None:
        service.write_exec_stamp(self.workdir, service.ExecStamp(
            "2.0.1", "signature", self.world.pid, "/fixture/gateway", started.isoformat(), "pid:[101]",
            instance=self.NONCE))
        self.world.lock, self.world.alive, self.world.listener = True, True, "ours"
        self.active = "active"

    def install(self, user: list[dict], system: list[dict] | None = None, mode: str | None = None) -> Path:
        self.log_path = fake.install(self.bindir, user, self.system if system is None else system, mode)
        return self.log_path

    def rewrite(self, user: list[dict], system: list[dict] | None = None, mode: str | None = None) -> None:
        fake.write_fixture(self.bindir, user, self.system if system is None else system, mode)

    def production(self) -> gl.Gateway:
        return self.gateway(seams=self.world.seams(runner=self.runner))

    def journal_calls(self) -> list[list[str]]:
        assert self.log_path is not None
        calls = fake.calls(self.log_path)
        self.assertTrue(calls, "the fake journalctl is the one that answered")
        return calls

    def test_a_week_long_instance_holds_nothing(self) -> None:
        # Revert-failing: b4d8004f read the newest JOURNAL_BUDGET bytes of the
        # whole unit journal and held "coverage truncated".
        started = NOW - datetime.timedelta(days=7)
        self.run_instance(started)
        self.install(week_long_entries(started, self.NONCE))
        with mock.patch.object(gateway_hold, "JOURNAL_BUDGET", 256 << 10):
            gateway = self.production()
            report = gateway.persistence_hold()
            self.assertFalse(report.held, report.reasons)
            self.assertEqual(report.evaluated, f"from {started - gateway_hold.START_MARGIN:%Y-%m-%d %H:%M} UTC: "
                                               "39 credential saves")
            self.assertIn(f"persistence hold: none (journal evaluated) — {report.evaluated}",
                          "\n".join(gateway.status().lines({})))
            outcome = gateway.stop()
        self.assertEqual(outcome.status, "stopped", outcome.lines())
        self.assertIn(["systemctl", "--user", "--no-ask-password", "stop", "charmer-gateway"], self.calls)
        calls = self.journal_calls()
        self.assertTrue(any(arg.startswith("--grep=") for arg in calls[0]))
        self.assertIn("--all", calls[0])

    def test_without_pcre_the_same_report_and_a_failing_journalctl_holds(self) -> None:
        started = NOW - datetime.timedelta(days=7)
        self.run_instance(started)
        entries = week_long_entries(started, self.NONCE)
        self.install(entries)
        with mock.patch.object(gateway_hold, "JOURNAL_BUDGET", 256 << 10):
            expected = self.production().persistence_hold()
            log = self.install(entries, mode="no-pcre")
            report = self.production().persistence_hold()
        self.assertEqual((report.held, report.reasons, report.coverage_only, report.evaluated),
                         (expected.held, expected.reasons, expected.coverage_only, expected.evaluated))
        reads = [argv for argv in fake.calls(log) if "--user" in argv]
        self.assertEqual(len(reads), 2)
        self.assertTrue(any(arg.startswith("--grep=") for arg in reads[0]))
        self.assertFalse(any(arg.startswith("--grep") for arg in reads[1]))
        self.install(entries, mode="error")
        gateway = self.production()
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertIn("(coverage unavailable); the unit's journal could not be read", "; ".join(report.reasons))
        self.assertEqual(gateway.stop().status, "held")
        self.assertFalse(any("stop" in call for call in self.calls))

    def clear_during_the_instance(self) -> tuple[gl.Gateway, list[dict], datetime.datetime]:
        """An instance started 3 days ago, cleared a day ago with its start
        line in the journal; returns the gateway, the entries and the clear."""

        started = NOW - datetime.timedelta(days=3)
        self.run_instance(started)
        entries = [entry for entry in week_long_entries(started, self.NONCE, requests=200)
                   if int(entry["__REALTIME_TIMESTAMP"]) < micros(NOW - datetime.timedelta(days=1))]
        self.install(entries)
        self.world.now = NOW - datetime.timedelta(days=1)
        gateway = self.production()
        report = gateway.persistence_hold()
        self.assertFalse(report.held, report.reasons)
        gateway.clear_hold(report)
        self.world.now = NOW
        return gateway, entries, NOW - datetime.timedelta(days=1)

    def recent(self) -> list[dict]:
        """Only entries of the last 12 hours, persisted saves among them."""

        start = NOW - datetime.timedelta(hours=12)
        entries = []
        for n in range(20):
            at = start + datetime.timedelta(minutes=30 * n)
            entries.append(fake.entry(request_at(at, n), micros(at), cursor=f"s=recent;r={n}"))
            entries.append(fake.entry(save_at(at + datetime.timedelta(seconds=1), generation=100 + n),
                                      micros(at) + 1_000_000, cursor=f"s=recent;s={n}"))
        return entries

    def test_a_vacuum_past_a_clear_during_the_instance_holds(self) -> None:
        # Revert-failing: b4d8004f read from the clear and never asked
        # whether the journal still reached back to it.
        gateway, _entries, cleared = self.clear_during_the_instance()
        self.rewrite(self.recent())  # rotated or vacuumed: nothing at or before the clear is left
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertTrue(report.coverage_only, report.reasons)
        self.assertIn(f"service journal: the journal no longer reaches back to the last clear-hold "
                      f"({cleared.isoformat(timespec='seconds')}); it was rotated or vacuumed since",
                      "; ".join(report.reasons))
        self.assertEqual(gateway_hold.remedy(report), gateway_hold.COVERAGE_REMEDY)
        self.assertEqual(gateway.stop().status, "held")
        self.assertFalse(any("stop" in call for call in self.calls))
        # The windowed read runs first, then the reach-back probe.
        calls = self.journal_calls()
        last_probe = max(i for i, argv in enumerate(calls) if "--until" in argv)
        last_read = max(i for i, argv in enumerate(calls) if "--user" in argv and "--since" in argv)
        self.assertLess(last_read, last_probe)
        self.assertEqual(calls[last_probe][calls[last_probe].index("--until") + 1], f"@{int(cleared.timestamp())}")

    def test_the_journal_still_reaching_the_clear_is_complete_and_a_failed_probe_holds(self) -> None:
        gateway, _entries, cleared = self.clear_during_the_instance()
        before = cleared - datetime.timedelta(hours=1)
        self.rewrite([fake.entry(request_at(before, 1), micros(before), cursor="s=kept;r=1"), *self.recent()])
        report = gateway.persistence_hold()
        self.assertFalse(report.held, report.reasons)
        self.assertIn("20 credential saves", report.evaluated)
        self.rewrite(self.recent(), mode="until-error")
        report = gateway.persistence_hold()
        self.assertTrue(report.held)
        self.assertTrue(report.coverage_only)
        self.assertIn("whether the journal still reaches back to the last clear-hold", "; ".join(report.reasons))
        self.assertIn("could not be read", "; ".join(report.reasons))

    def test_journald_drop_notices_on_the_users_service_group_hold(self) -> None:
        # Revert-failing: b4d8004f never read journald's drop notices.
        gateway, entries, cleared = self.clear_during_the_instance()
        uid = os.getuid()
        after, before = micros(NOW - datetime.timedelta(hours=2)), micros(cleared - datetime.timedelta(hours=5))
        cases = {
            "a notice after the clear": ([fake.drop_notice(after, uid=uid, dropped="5")], "dropped 5 log lines"),
            "attributed to another user unit": (
                [fake.drop_notice(after, uid=uid, dropped="7", user_unit="pipewire.service")], "dropped 7 log lines"),
            "a count journald did not give": ([fake.drop_notice(after, uid=uid, dropped=None)], "dropped some log"),
            "a notice before the clear": ([fake.drop_notice(before, uid=uid, dropped="5")], None),
            "another user's services": ([fake.drop_notice(after, uid=uid + 1, dropped="5")], None),
        }
        for label, (notices, held) in cases.items():
            with self.subTest(label):
                self.rewrite(entries, [*self.system, *notices])
                report = gateway.persistence_hold()
                self.assertEqual(report.held, held is not None, report.reasons)
                if held is not None:
                    self.assertTrue(report.coverage_only)
                    text = "; ".join(report.reasons)
                    self.assertIn("service journal: journald's rate limit for your user services, which the gateway "
                                  "shares, " + held, text)
                    self.assertIn(f"(last notice {(NOW - datetime.timedelta(hours=2)).isoformat(timespec='seconds')})"
                                  "; a credential save among them cannot be ruled out", text)
                else:
                    self.assertNotIn("drop notices", report.evaluated)
        # An ordinary user cannot read the system journal: said, not held.
        self.rewrite(entries, [*self.system, fake.drop_notice(after, uid=uid, dropped="5")], mode="system-unreadable")
        report = gateway.persistence_hold()
        self.assertFalse(report.held, report.reasons)
        self.assertIn("; journald's drop notices are not readable for this user", report.evaluated)
        status = "\n".join(gateway.status().lines({}))
        self.assertIn("drop notices are not readable for this user", status)

    def test_both_passes_read_the_same_window_and_report(self) -> None:
        started = NOW - datetime.timedelta(days=2)
        self.run_instance(started)
        entries = [fake.entry(service.start_marker_prefix(self.NONCE) + " (pid 4242)", micros(started) + 500_000,
                              cursor="s=eq;start")]
        for n in range(2000):
            at = started + datetime.timedelta(seconds=30 * n + 2)
            entries.append(fake.entry(request_at(at, n), micros(at), cursor=f"s=eq;r={n}"))
        for n in range(50):
            at = started + datetime.timedelta(seconds=1000 * n + 3)
            result = "failed" if n == 10 else "persisted"
            entries.append(fake.entry(save_at(at, result, generation=n + 1), micros(at), cursor=f"s=eq;s={n}"))
        binary_at = started + datetime.timedelta(hours=20)
        entries.append(fake.entry(list(save_at(binary_at, generation=99).encode()), micros(binary_at),
                                  cursor="s=eq;bin-save"))
        entries.append(fake.entry(list(b"an ordinary line with a byte \xff"), micros(binary_at) + 1,
                                  cursor="s=eq;bin-other"))
        self.install(entries)
        since = f"@{int((started - gateway_hold.START_MARGIN).timestamp())}"

        def read() -> observation.LogWindow:
            return service.read_hold_journal(unit="charmer-gateway", since=since,
                                             evidence=gateway_events.SAVE_LINE_PATTERN,
                                             marker=service.start_marker_prefix(self.NONCE),
                                             max_bytes=gateway_hold.JOURNAL_BUDGET)

        first, first_report = read(), self.production().persistence_hold()
        self.rewrite(entries, mode="no-pcre")
        second, second_report = read(), self.production().persistence_hold()
        self.assertEqual(first, second)
        self.assertEqual((first.coverage, len(first.records)), ("bounded", 1 + 50 + 1))
        self.assertEqual(
            (first_report.held, first_report.reasons, first_report.coverage_only, first_report.evaluated),
            (second_report.held, second_report.reasons, second_report.coverage_only, second_report.evaluated))
        self.assertFalse(first_report.held, first_report.reasons)
        self.assertIn("51 credential saves", first_report.evaluated)
        modes = [any(arg.startswith("--grep") for arg in argv) for argv in self.journal_calls() if "--user" in argv]
        self.assertEqual(modes[-2:], [True, False])

    def test_the_systemd_stop_refuses_a_gateway_that_changed_while_its_hold_was_read(self) -> None:
        # Revert-failing: b4d8004f dispatched the stop to whatever ran then.
        self.world.become_ready("9999999999999999")
        self.active = "active"
        marker = observation.LogRecord(service.start_marker_prefix("9999999999999999"), NOW, "boot:invocation", "c0")

        def journal(_unit, _since):
            self.world.become_ready("8888888888888888")  # a restart during the evaluation
            return observation.LogWindow((marker,), "bounded")

        outcome = self.gateway(seams=self.world.seams(runner=self.runner, journal=journal)).stop()
        self.assertEqual(outcome.status, "refused", outcome.lines())
        self.assertIn("the gateway changed meanwhile; nothing was stopped", outcome.message)
        self.assertFalse(any("stop" in call for call in self.calls))


class OnDemandHoldScaleTests(_Lifecycle):
    """The on-demand hold reads each instance log's credential-save lines
    whole: how much the gateway logged no longer matters."""

    FILLER = ('[2026-10-02 11:00:00] [a1b2c3d4] [info ] [gin_logger.go:92] 200 |  1.2s |  127.0.0.1 | '
              'POST "/v1/messages" ' + "y" * 200)

    def big_log(self, nonce: str, megabytes: int, *, started: datetime.datetime = NOW,
                failure: bool = True) -> Path:
        """``megabytes`` of ordinary lines with persisted saves at the start,
        middle and end, and (``failure``) a failure in the middle that the
        last save repairs."""

        half = (megabytes << 20) // (len(self.FILLER) + 1) // 2 + 1
        lines = [f"charmer-proxy: gateway instance {nonce} starting", save_line("persisted", at="2026-10-02 10:00:00"),
                 *[self.FILLER] * half, save_line("persisted", at="2026-10-02 10:30:00"),
                 *([save_line("failed", at="2026-10-02 11:00:00")] if failure else []), *[self.FILLER] * half,
                 save_line("persisted", at="2026-10-02 11:30:00")]
        path = self.log_file(nonce, lines, started=started)
        self.assertGreater(path.stat().st_size, megabytes << 20)
        return path

    def test_a_20_mb_running_log_with_a_repaired_failure_holds_nothing(self) -> None:
        # Revert-failing: b4d8004f read the newest 8 MiB and held "truncated".
        self.running("abababababababab")
        self.big_log("abababababababab", 20)
        report = self.gateway().persistence_hold()
        self.assertFalse(report.held, report.reasons)
        self.assertEqual(report.evaluated, "1 instance log(s): 4 credential saves")
        self.assertEqual(self.gateway().stop().status, "stopped")

    def test_keep_mode_equals_the_full_read_on_small_logs(self) -> None:
        failed = save_line("failed").encode()
        cases = {
            "a cleared start": ([b"noise", save_line("persisted").encode(), failed, b"tail"], 6),
            "an unfinished last line": ([b"noise", failed, failed[:40] + b"\x00no newline"], 0),
            "a line over the limit": ([failed, b"x" * (file_log.LINE_LIMIT + 5), failed, b"after"], 0),
            "invalid UTF-8 around the token": ([b"\xff" + failed, failed.replace(b"[--------]", b"[\xfe\xff]") + b"\xc3",
                                                b"[2026-10-02 12:00:00] [\xff] [warn ] [credential_save.go:214] "
                                                b"credential_save_v1 \xff"], 0),
        }
        for label, (lines, start) in cases.items():
            with self.subTest(label):
                nonce = f"{abs(hash(label)) % (1 << 64):016x}"
                descriptor, path = file_log.create_instance_log(self.logs, NOW, nonce)
                body = b"\n".join(lines) + (b"" if label == "an unfinished last line" else b"\n")
                os.write(descriptor, body)
                os.close(descriptor)
                log = file_log.find_instance(self.logs, nonce)
                full = file_log.read_evidence(log, start=start)
                kept = file_log.read_evidence(log, start=start, keep=gateway_events.SAVE_LINE_BYTES)
                self.assertEqual(kept.window.records, tuple(record for record in full.window.records
                                                            if gateway_events.is_save_line(record.message)))
                self.assertEqual((kept.window.coverage, kept.end, kept.through),
                                 (full.window.coverage, full.end, full.through))
                for record in kept.window.records:  # each cursor is its line's exact offset
                    offset = int(record.source_cursor.split(":")[1])
                    self.assertTrue(offset == 0 or body[offset - 1:offset] == b"\n")
                    self.assertEqual(body[offset:].split(b"\n", 1)[0].decode("utf-8", "replace"), record.message)
                os.unlink(path)

    def test_the_scan_budget_and_the_deadline_hold_and_say_why(self) -> None:
        self.running("cdcdcdcdcdcdcdcd")
        self.big_log("cdcdcdcdcdcdcdcd", 2, failure=False)
        gateway = self.gateway()
        with mock.patch.object(gateway_hold, "TOTAL_BUDGET", 1 << 20):
            report = gateway.persistence_hold()
            self.assertTrue(report.held)
            self.assertTrue(report.coverage_only, report.reasons)
            self.assertIn("instance cdcdcdcdcdcdcdcd: the log could not be evaluated completely (coverage truncated); "
                          "the logs are larger than one evaluation reads", report.reasons)
            gateway.clear_hold(report)
            self.assertFalse(gateway.persistence_hold().held)  # cleared through the end of the log
        self.log_file("efefefefefefefef", [save_line("persisted")], started=NOW - datetime.timedelta(hours=1))
        report = gateway_hold.evaluate_files(self.state_root, ("claude",), deadline=time.monotonic() - 1)
        self.assertTrue(report.held)
        self.assertTrue(report.coverage_only)
        self.assertIn("instance efefefefefefefef: the log could not be evaluated completely (coverage incomplete); "
                      "the log read did not finish within 30 s", report.reasons)

    def test_pruning_reads_a_whole_victim(self) -> None:
        # Revert-failing: b4d8004f evaluated the newest 8 MiB and recorded a
        # "(log pruned)" truncated hold.
        path = self.big_log("1111111111111111", 20, started=NOW - datetime.timedelta(hours=2))
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0)
        self.assertEqual((report.removed, report.persisted), (("1111111111111111",), ()))
        self.assertFalse(path.exists())
        self.assertEqual(gateway_hold.read_document(self.state_root)["holds"], [])

    def test_a_prune_that_runs_out_of_time_keeps_the_log_and_records_nothing(self) -> None:
        path = self.log_file("2222222222222222", ["noise", save_line("failed")],
                             started=NOW - datetime.timedelta(hours=2))
        hold = self.workdir / gateway_hold.HOLD_FILE
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0, deadline=time.monotonic() - 1)
        self.assertEqual((report.removed, report.persisted), ((), ()))
        self.assertEqual(report.retained_bytes, path.stat().st_size)
        self.assertTrue(path.exists())
        self.assertFalse(hold.exists())
        report = gateway_hold.prune_at_start(self.state_root, ("claude",), budget=0)
        self.assertEqual((report.removed, report.persisted), (("2222222222222222",),) * 2)
        (held,) = gateway_hold.read_document(self.state_root)["holds"]
        self.assertEqual(held["reason"], "a credential save failed and no later save repaired it (claude) (log pruned)")
        self.assertEqual(held["coverage"], "bounded")


def _valid_201(document) -> bool:
    """2.0.1's hold-document check (``gateway_hold._valid`` at b4d8004f),
    frozen: every document this release writes must pass it."""

    import re

    hold_keys, optional = frozenset({"instance", "reason", "recorded_at"}), frozenset({"coverage", "evidence"})
    coverages = ("bounded", "truncated", "incomplete", "unavailable")
    if not isinstance(document, dict) or set(document) != {"version", "cleared", "cleared_through", "holds"}:
        return False
    if document["version"] != 1:
        return False
    cleared = document["cleared"]
    if not isinstance(cleared, dict) or not all(
            isinstance(key, str) and re.compile(r"^[0-9a-f]{16}$").fullmatch(key) and type(value) is int
            and value >= 0 for key, value in cleared.items()):
        return False
    through = document["cleared_through"]
    if through is not None:
        try:
            if not isinstance(through, str) or datetime.datetime.fromisoformat(through).tzinfo is None:
                return False
        except ValueError:
            return False
    holds = document["holds"]
    return isinstance(holds, list) and all(
        isinstance(item, dict) and hold_keys <= set(item) <= hold_keys | optional
        and all(isinstance(item[key], str) and item[key] for key in item)
        and item.get("coverage", "bounded") in coverages
        for item in holds)


class HoldDocumentCompatibilityTests(_Lifecycle):
    """HOLD_VERSION 1 both ways: this release reads 2.0.1's documents and
    recognizes their coverage-only holds; 2.0.1 reads every document this
    release writes."""

    PRUNED = {"instance": "1111111111111111", "recorded_at": "2026-10-01T10:00:00+00:00",
              "reason": "the log could not be evaluated completely (coverage truncated) (log pruned)",
              "coverage": "truncated"}
    JOURNAL = {"instance": "boot:invocation", "recorded_at": "2026-10-01T10:00:00+00:00",
               "reason": "a credential save failed and no later save repaired it (claude; service journal)",
               "evidence": "journal boot:invocation claude 0123456789abcdef 2026-10-01T09:00:00+00:00"}

    def hold_document(self, *holds, through="2026-10-18T17:24:54.595482+00:00", cleared=None) -> dict:
        return {"version": 1, "cleared": cleared or {}, "cleared_through": through, "holds": list(holds)}

    def written(self) -> dict:
        document = json.loads((self.workdir / gateway_hold.HOLD_FILE).read_text())
        self.assertEqual(set(document), {"version", "cleared", "cleared_through", "holds"})
        self.assertTrue(_valid_201(document), document)
        return document

    def test_documents_of_2_0_1_read_and_their_coverage_holds_are_recognized(self) -> None:
        hold = self.workdir / gateway_hold.HOLD_FILE
        for holds, coverage_only in (((self.PRUNED,), True), ((self.PRUNED, self.JOURNAL), False),
                                     ((dict(self.PRUNED, reason="an unresolved save (pruned log)"),), False), ((), None)):
            with self.subTest(holds=len(holds), coverage_only=coverage_only):
                document = self.hold_document(*holds, cleared={"2222222222222222": 7})
                self.assertTrue(_valid_201(document))
                state.atomic_write(hold, json.dumps(document).encode())
                self.assertEqual(gateway_hold.read_document(self.state_root), document)
                report = gateway_hold.evaluate_files(self.state_root, ("claude",))
                self.assertEqual(report.held, coverage_only is not None)
                if coverage_only is not None:
                    self.assertEqual(report.coverage_only, coverage_only)
        # The operator's document: cleared just before the running instance
        # started, so its start line decides.
        state.atomic_write(hold, json.dumps(self.hold_document(through="2026-10-18T17:24:54.595482Z")).encode())
        started = datetime.datetime(2026, 10, 18, 17, 25, 25, 493491, tzinfo=datetime.timezone.utc)
        marker = observation.LogRecord(service.start_marker_prefix("d26721e6b9c12dfb") + " (pid 1)", started,
                                       "boot:invocation", "c0")
        asked = []

        def reader(since):
            asked.append(since)
            return observation.LogWindow((marker,), "bounded")

        report = gateway_hold.evaluate_journal(self.state_root, ("claude",), reader=reader,
                                               now=lambda: "2026-10-25T12:00:00+00:00",
                                               running=("d26721e6b9c12dfb", started), drops=lambda _since: (),
                                               journald_start=lambda: started - datetime.timedelta(days=1))
        self.assertFalse(report.held, report.reasons)
        self.assertEqual(asked, [started - gateway_hold.START_MARGIN])

    def test_every_document_this_release_writes_passes_2_0_1s_check(self) -> None:
        # A clear.
        self.running("3333333333333333")
        self.log_file("3333333333333333", [save_line("failed")])
        gateway = self.gateway()
        gateway.clear_hold(gateway.persistence_hold())
        self.assertEqual(self.written()["cleared"], {"3333333333333333": len(save_line("failed")) + 1})
        # A prune: a failure and a cut evaluation, with byte-identical 2.0.1 reasons.
        self.log_file("4444444444444444", [save_line("failed", at="2026-10-02 11:00:00")],
                      started=NOW - datetime.timedelta(hours=3))
        self.log_file("5555555555555555", [save_line("persisted", at="2026-10-02 10:00:00")],
                      started=NOW - datetime.timedelta(hours=4))
        gateway_hold.prune_at_start(self.state_root, ("claude",), keep={"3333333333333333"}, budget=0, file_budget=64)
        reasons = {item["instance"]: item["reason"] for item in self.written()["holds"]}
        self.assertEqual(reasons, {
            "4444444444444444": "the log could not be evaluated completely (coverage truncated) (log pruned)",
            "5555555555555555": "the log could not be evaluated completely (coverage truncated) (log pruned)"})
        self.assertTrue(gateway_hold.evaluate_files(self.state_root, ("claude",)).coverage_only)
        # Journal evidence, from a complete and from an incomplete read.
        failed = observation.LogRecord(save_line("failed"), NOW, "boot:invocation", "c1")
        bogus = observation.LogRecord(save_line("persisted").replace("operation=refresh", "operation=bogus"), NOW,
                                      "boot:invocation", "c2")
        for coverage, records in (("bounded", (failed,)), ("truncated", (failed, bogus))):
            gateway_hold.evaluate_journal(self.state_root, ("claude",), now=lambda: NOW.isoformat(),
                                          reader=lambda _since, records=records, coverage=coverage:
                                          observation.LogWindow(records, coverage))
            self.assertTrue(any("evidence" in item for item in self.written()["holds"]))


if __name__ == "__main__":
    unittest.main()
