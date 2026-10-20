"""The qualification battery through the real pinned gateway, offline.

The built cli-proxy-api (a Nix store executable, or an explicit portable
build bound to ``CHARMER_TEST_GATEWAY_BUILD`` by the shared test selector)
runs inside ``bwrap --unshare-net`` with a rendered fixture config;
its only upstream is a unix-socket fake that counts every attempt. The
production transport (``qualify.loopback_post``) reaches the gateway through
the ingress bridge. Proves the claim a design could not assume:
one planned request is one upstream attempt (no hidden gateway retry), for
a success, a rejection, a 429 and a 5xx. Skips with an explicit boundary
when the gateway binary or network isolation is unavailable.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

import _gateway
from _catalog import FIXTURE_ROOT
from _gateway_harness import boundary
from charmer import catalog, probe, qualify, render, service, state

GATEWAY_TOKEN = "q" * 64
DUMMY_KIMI = "dummy-kimi-key"
TEST_BINARY_ENV = "CHARMER_TEST_CLI_PROXY_API"
OWNER = staticmethod(lambda _base: service.OwnerVerdict("ours", "fixture gateway"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _binary() -> str | None:
    binary, reason = _gateway._gateway_binary()
    if binary is None and os.environ.get(TEST_BINARY_ENV):
        boundary(reason)
    return binary


class QualifyBinarySelectionTests(unittest.TestCase):
    """Pure selector integration: validate fixture bytes, never execute them."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charmer-qualify-selector-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.binary = self.root / "gateway" / "cli-proxy-api"
        self.binary.parent.mkdir(mode=0o700)
        self.binary.write_bytes(b"synthetic gateway fixture; never execute\n")
        self.binary.chmod(0o700)
        self.record = self.root / "BUILD.json"
        self.document = {"format": 1, "admitted_series": True, "series": _gateway.admitted_series(),
                         "targets": {"linux-amd64": {"sha256": _gateway.sha256_file(self.binary)}}}
        self.write_record()
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.home), "CHARMER_TEST_REQUIRE_GATEWAY": "1",
            TEST_BINARY_ENV: str(self.binary), _gateway.BUILD_RECORD_ENV: str(self.record),
        }, clear=True))
        self.enterContext(mock.patch.object(_gateway, "host_target", return_value="linux-amd64"))
        self.wrapper = self.enterContext(mock.patch.object(_gateway, "_wrapper_gateway", return_value=None))
        self.which = self.enterContext(mock.patch.object(_gateway.shutil, "which", return_value=None))
        self.version = self.enterContext(mock.patch.object(
            _gateway, "_version", return_value=(str(self.binary), "fixture-version")))

    def write_record(self) -> None:
        self.record.write_text(json.dumps(self.document), encoding="utf-8")
        self.record.chmod(0o600)

    def assertRefused(self, reason: str) -> None:
        with self.assertRaisesRegex(AssertionError, reason) as raised:
            _binary()
        self.assertIn("CHARMER_TEST_REQUIRE_GATEWAY=1:", str(raised.exception))
        self.version.assert_not_called()
        self.wrapper.assert_not_called()
        self.which.assert_not_called()

    def test_valid_explicit_portable_build_is_validated_before_version(self) -> None:
        with mock.patch.object(_gateway, "bound_gateway_problem", wraps=_gateway.bound_gateway_problem) as validate:
            def version(real):
                validate.assert_called_once_with(str(self.binary), str(self.record))
                self.assertEqual(real, self.binary)
                return str(real), "fixture-version"

            self.version.side_effect = version
            self.assertEqual(_binary(), str(self.binary))
        self.version.assert_called_once_with(self.binary)
        self.wrapper.assert_not_called()
        self.which.assert_not_called()

    def test_missing_build_record_and_changed_hash_or_series_are_refused(self) -> None:
        with mock.patch.dict(os.environ, {_gateway.BUILD_RECORD_ENV: ""}):
            self.assertRefused("set " + _gateway.BUILD_RECORD_ENV)
        with mock.patch.dict(os.environ, {_gateway.BUILD_RECORD_ENV: str(self.root / "missing-BUILD.json")}):
            self.assertRefused("cannot be read")
        self.document["targets"]["linux-amd64"]["sha256"] = "0" * 64
        self.write_record()
        self.assertRefused("sha256 differs")
        self.document["targets"]["linux-amd64"]["sha256"] = _gateway.sha256_file(self.binary)
        self.assertGreater(len(self.document["series"]), 1)
        self.document["series"].reverse()
        self.write_record()
        self.assertRefused("another patch series")

    def test_invalid_explicit_selection_never_falls_back(self) -> None:
        os.environ[TEST_BINARY_ENV] = str(self.root / "missing-cli-proxy-api")
        self.wrapper.return_value = self.which.return_value = str(self.binary)
        self.assertRefused("missing-cli-proxy-api")

    def test_ambient_portable_binary_is_not_accepted_without_an_explicit_selection(self) -> None:
        os.environ.pop(TEST_BINARY_ENV)
        self.which.return_value = str(self.binary)
        self.assertIsNone(_binary())
        self.version.assert_not_called()

    def test_optional_absence_returns_none(self) -> None:
        os.environ.pop(TEST_BINARY_ENV)
        os.environ.pop("CHARMER_TEST_REQUIRE_GATEWAY")
        self.assertIsNone(_binary())
        self.version.assert_not_called()
        self.wrapper.assert_called_once_with()
        self.which.assert_called_once_with("cli-proxy-api")


class _Upstream(BaseHTTPRequestHandler):
    """A fake Anthropic-compatible upstream: counts attempts, answers by mode."""

    attempts: list[str] = []
    mode = "ok"

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, payload: dict | None = None, *, sse: list | None = None) -> None:
        if sse is not None:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in sse:
                self.wfile.write(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()
            return
        data = json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        document = json.loads(self.rfile.read(length) or b"{}")
        path = urllib.parse.urlsplit(self.path).path
        if not path.endswith("/v1/messages"):
            self._send(404, {"type": "error"})
            return
        type(self).attempts.append(path)
        mode = type(self).mode
        if mode in ("429", "500", "400"):
            self._send(int(mode), {"type": "error", "error": {"type": "fake", "message": "fake upstream"}})
            return
        message = {"id": "msg_up", "type": "message", "role": "assistant", "model": document.get("model"),
                   "stop_reason": "end_turn", "stop_sequence": None,
                   "usage": {"input_tokens": 7, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}
        if document.get("stream"):
            self._send(200, sse=[
                {"type": "message_start", "message": {**message, "content": []}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ])
            return
        last = document["messages"][-1]["content"]
        text = last if isinstance(last, str) else "".join(
            block.get("text", "") for block in last if isinstance(block, dict) and block.get("type") == "text")
        results = [] if isinstance(last, str) else [
            block for block in last if isinstance(block, dict) and block.get("type") == "tool_result"]
        if document.get("tools") and not results and 'nonce "' in text:
            nonce = text.split('nonce "', 1)[1].split('"', 1)[0]
            message["content"] = [{"type": "tool_use", "id": "toolu_up_1", "name": qualify.TOOL_NAME,
                                   "input": {"nonce": nonce}}]
            message["stop_reason"] = "tool_use"
        elif results:
            content = results[0].get("content")
            reply = content if isinstance(content, str) else "".join(
                part.get("text", "") for part in content if isinstance(part, dict))
            message["content"] = [{"type": "text", "text": reply}]
        self._send(200, message)


class AttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binary = _binary()
        if self.binary is None:
            boundary("boundary: pinned cli-proxy-api binary not available; gateway attempt count skipped")
        reason = probe.network_isolation_available()
        if reason is not None:
            boundary("BOUNDARY: " + reason)
        self.root = Path(tempfile.mkdtemp(prefix="charmer-qualify-gw-"))
        os.chmod(self.root, 0o700)
        self.process: subprocess.Popen | None = None
        self.upstream: probe.UnixHTTPServer | None = None
        self.addCleanup(self._cleanup)
        self._start()

    def _cleanup(self) -> None:
        if self.process is not None:
            probe.stop_isolated_process(self.process)
        if self.upstream is not None:
            self.upstream.shutdown()
            self.upstream.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _start(self) -> None:
        _Upstream.attempts = []
        upstream_port, proxy_port = _free_port(), _free_port()
        self.assertNotIn(proxy_port, (8316, 8317))
        upstream_socket = self.root / "upstream.sock"
        self.gateway_socket = self.root / "gateway.sock"
        self.upstream = probe.UnixHTTPServer(str(upstream_socket), _Upstream)
        threading.Thread(target=self.upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        gateway = copy.deepcopy(bundle.docs["gateway"])
        gateway["gateway"]["base_url"] = f"http://127.0.0.1:{proxy_port}"
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        providers["kimi"]["transport"]["base_url"] = f"http://127.0.0.1:{upstream_port}/coding"
        self.line = next(entry for entry in bundle.lines.values() if entry["provider"] == "kimi")
        self.provider = providers["kimi"]
        result = render.render_config(gateway, providers, bundle.lines, continuity={}, home=self.root,
                                      gateway_token=GATEWAY_TOKEN, resolve_secret=lambda _name: DUMMY_KIMI)
        config = self.root / "config.yaml"
        state.atomic_write(config, result.yaml.encode("utf-8"))
        self.process = probe.start_isolated_process(
            [os.path.realpath(self.binary), "--config", str(config), "--local-model"],
            bridges=[probe.PortBridge(upstream_port, upstream_socket),
                     probe.PortBridge(proxy_port, self.gateway_socket, "ingress")],
            cwd=self.root, env={"HOME": str(self.root), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.base_url = f"http://127.0.0.1:{proxy_port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                connection = probe.UnixHTTPConnection(self.gateway_socket, timeout=1)
                connection.request("GET", "/healthz", headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"})
                if connection.getresponse().status == 200:
                    connection.close()
                    return
                connection.close()
            except OSError:
                pass
            time.sleep(0.1)
        self.fail("disposable gateway did not become ready")

    def _post(self, call: qualify.PlannedCall, body: bytes) -> qualify.HttpResult:
        return qualify.loopback_post(
            self.base_url, GATEWAY_TOKEN, body, deadline=call.deadline, owner_check=OWNER,
            connect=lambda timeout: probe.UnixHTTPConnection(self.gateway_socket, timeout=timeout),
        )

    def _plan(self, checks):
        return qualify.build_plan("fixture-kimi", self.line, digest="a" * 64, provider_id="kimi",
                                  provider=self.provider, checks=checks)

    def test_one_planned_request_has_one_upstream_attempt(self) -> None:
        plan = self._plan(qualify.AGENT_CHECKS)
        results = qualify.run_battery(plan, self._post)
        self.assertEqual([(r.check, r.result) for r in results],
                         [("smoke", "pass"), ("efforts", "pass"), ("tools", "pass"), ("stream", "pass")],
                         [(r.check, r.result, r.http, r.reason) for r in results])
        self.assertEqual(len(_Upstream.attempts), len(plan.calls))
        for mode, expected in (("400", ("failed", "http-status")), ("429", ("inconclusive", "rate-limited")),
                               ("500", ("inconclusive", "upstream-error"))):
            with self.subTest(mode=mode):
                _Upstream.mode = mode
                _Upstream.attempts = []
                try:
                    (result,) = qualify.run_battery(self._plan(("smoke",)), self._post)
                finally:
                    _Upstream.mode = "ok"
                self.assertEqual((result.result, result.reason), expected, result)
                self.assertEqual(len(_Upstream.attempts), 1, "a planned request reached the upstream more than once")


if __name__ == "__main__":
    unittest.main()
