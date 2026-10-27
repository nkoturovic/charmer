"""Kimi K3.1 Pro added by hand on the Kimi Code route.

On the Kimi Code subscription the model is ``k3.1-pro`` (262,144 tokens;
``k3.1-pro[1m]`` is the 1M variant on Pro plans and above, a wire the
declaration grammar does not admit), thinking-only with the efforts low, high
and max (default high); the vendor refuses a request with thinking disabled
and needs complete assistant turns, reasoning included, replayed unchanged.
``kimi-for-coding`` (the shipped ``kimi-code`` line) is still K2.8 Preview, so
K3.1 Pro on this route is a model of your own: the documented add path
declares it with the route's own contracts.

The gateway half runs the real pinned gateway inside ``bwrap --unshare-net``
on the render the add produced; its only upstream is a unix-socket fake of the
Kimi Code endpoint that answers like the vendor (thinking disabled: 400). The
Kimi provider's route filter removes the client's thinking parameter on every
alias, the declared one included, so the route's own default (thinking on)
applies and a request with thinking disabled never reaches it. No provider
request; never 8317/8316.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

import charmer.proxy
import test_cli  # module import: no test classes re-exported
import test_qualify_gateway as qualify_gateway
from _catalog import SHIPPED_ROOT
from _gateway_harness import boundary
from charmer import cli, probe, proxy, state

KEY = "custom-k3-1-pro"
SOURCE = "https://www.kimi.com/code/docs/en/kimi-code/models.html, 2026-10-27"
ADD = ["models", "add", "kimi", "k3.1-pro", "--effort", "low=output-config-low", "--effort", "high=output-config-high",
       "--effort", "max=output-config-max", "--default-effort", "high", "--context", "262144", "--source", "docs",
       "--source-ref", SOURCE]
LEVELS = ("low", "high", "max")
SIGNATURE = "fixture-signature-0001"


class _ShippedOperatorCase(test_cli.OperatorCommandCase):
    """The operator command fixture on the shipped catalog (its kimi
    provider carries the route's output-config low/high/max contracts)."""

    def setUp(self) -> None:
        super().setUp()
        self.runtime = cli.Runtime(
            **self._runtime_seams(), asset_root=SHIPPED_ROOT, environ=self.runtime.environ, cwd=self.root / "project",
            launch_callback=lambda prepared: self.launches.append(prepared) or 0, doctor_callback=lambda _r: [],
            doctor_binary_callback=lambda _contract: ([], ["fixture"]),
            doctor_daemon_callback=self.runtime.doctor_daemon_callback)
        self.runtime.qualify_transport = self._transport
        self.runtime.qualify_http = self._qualify_http
        patcher = mock.patch.object(self.runtime, "verify_reload", side_effect=lambda sentinel: charmer.proxy.ReloadResult(
            "reloaded", f"gateway: reloaded (sentinel {sentinel})"))
        patcher.start()
        self.addCleanup(patcher.stop)


class KimiK31ProDeclarationTests(_ShippedOperatorCase):
    def test_the_add_declares_the_coding_id_with_the_route_efforts(self) -> None:
        code, out, err = self.op(ADD)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"declared {KEY} — New · not admitted · selectors ", out)
        self.assertIn("class custom-262144", out)
        entry = self.runtime.operator_snapshot().layer.lines[KEY].core_entry
        self.assertEqual(entry["wire_model"], "k3.1-pro")
        self.assertEqual(entry["default_effort"], "high")
        self.assertEqual({level: spec["proxy_contract"] for level, spec in entry["efforts"].items()},
                         {level: f"output-config-{level}" for level in LEVELS})
        # Above the 200K class: the 1M client class, bound at the declared 262,144.
        self.assertEqual(sorted(spec["selector"] for spec in entry["efforts"].values()),
                         [f"{KEY}-{level}[1m]" for level in sorted(LEVELS)])
        self.assertEqual(entry["context"]["provider_tokens"], 262144)

    def test_the_route_refuses_what_it_does_not_offer(self) -> None:
        before = self.state_bytes()
        # No medium contract on the Kimi Code route (the vendor maps medium to high).
        code, out, err = self.op([*ADD[:4], "--effort", "medium=output-config-medium", *ADD[4:]])
        self.assertEqual(code, 1, out + err)
        self.assertIn("output-config-medium", err)
        # The 1M variant's id is not a declarable wire.
        code, out, err = self.op(["models", "add", "kimi", "k3.1-pro[1m]", *ADD[4:]])
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(err, "charmer: models add: k3.1-pro[1m] cannot be declared: charmer adds [1m] to its own "
                              "selectors from --context; a provider id that itself ends in [1m] cannot be declared "
                              "— nothing written\n")
        self.assertEqual(self.state_bytes(), before)


class _KimiCoding(BaseHTTPRequestHandler):
    """The Kimi Code Anthropic-compatible endpoint, as the vendor documents
    it: thinking cannot be disabled (400); a turn answers with reasoning."""

    documents: list[dict] = []

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        document = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.split("?", 1)[0].endswith("/v1/messages"):
            self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": "fake"}})
            return
        type(self).documents.append(document)
        if (document.get("thinking") or {}).get("type") == "disabled":
            self._send(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                        "message": "thinking cannot be disabled for this model"}})
            return
        self._send(200, {
            "id": "msg_kimi", "type": "message", "role": "assistant", "model": document.get("model"),
            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 3},
            "content": [{"type": "thinking", "thinking": "fixture reasoning", "signature": SIGNATURE},
                        {"type": "text", "text": "ok"}]})


class KimiK31ProGatewayTests(_ShippedOperatorCase):
    """The declared selectors through the real gateway to the fake route."""

    def setUp(self) -> None:
        super().setUp()
        self.binary = qualify_gateway._binary()
        if self.binary is None:
            boundary("boundary: pinned cli-proxy-api binary not available; the K3.1 Pro route check skipped")
        reason = probe.network_isolation_available()
        if reason is not None:
            boundary("BOUNDARY: " + reason)
        code, out, err = self.op(ADD)
        self.assertEqual(code, 0, out + err)
        code, out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, out + err)
        self.work = Path(tempfile.mkdtemp(prefix="charmer-kimi-k31-"))
        os.chmod(self.work, 0o700)
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
        shutil.rmtree(self.work, ignore_errors=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _start(self) -> None:
        _KimiCoding.documents = []
        upstream_port, proxy_port = self._free_port(), self._free_port()
        self.assertNotIn(proxy_port, (8316, 8317))
        upstream_socket = self.work / "upstream.sock"
        self.gateway_socket = self.work / "gateway.sock"
        self.upstream = probe.UnixHTTPServer(str(upstream_socket), _KimiCoding)
        threading.Thread(target=self.upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        text = (proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        # The render the add produced, on a free port and the fake route.
        for old, new in (("\nport: 8317\n", f"\nport: {proxy_port}\n"),
                         ('base-url: "https://api.kimi.com/coding"',
                          f'base-url: "http://127.0.0.1:{upstream_port}/coding"')):
            self.assertEqual(text.count(old), 1, old)
            text = text.replace(old, new)
        self.assertIn(f'alias: "{KEY}-high"', text)
        config = self.work / "config.yaml"
        state.atomic_write(config, text.encode("utf-8"))
        self.token = self.runtime.gateway_token()
        self.process = probe.start_isolated_process(
            [os.path.realpath(self.binary), "--config", str(config), "--local-model"],
            bridges=[probe.PortBridge(upstream_port, upstream_socket),
                     probe.PortBridge(proxy_port, self.gateway_socket, "ingress")],
            cwd=self.work, env={"HOME": str(self.work), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                connection = probe.UnixHTTPConnection(self.gateway_socket, timeout=1)
                connection.request("GET", "/healthz", headers={"Authorization": f"Bearer {self.token}"})
                if connection.getresponse().status == 200:
                    connection.close()
                    return
                connection.close()
            except OSError:
                pass
            time.sleep(0.1)
        self.fail("disposable gateway did not become ready")

    def _post(self, body: dict) -> tuple[int, dict]:
        connection = probe.UnixHTTPConnection(self.gateway_socket, timeout=30)
        try:
            connection.request("POST", "/v1/messages", body=json.dumps(body).encode(), headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "anthropic-version": "2023-06-01"})
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw or b"{}")
        finally:
            connection.close()

    def _turn(self, level: str, **extra) -> dict:
        # The client sends the selector without its [1m] suffix.
        return {"model": f"{KEY}-{level}", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}],
                **extra}

    def test_each_effort_reaches_the_coding_id_with_the_route_default_thinking(self) -> None:
        for level in LEVELS:
            with self.subTest(level=level):
                _KimiCoding.documents = []
                status, reply = self._post(self._turn(level, thinking={"type": "adaptive"}))
                self.assertEqual(status, 200, reply)
                (sent,) = _KimiCoding.documents
                self.assertEqual(sent["model"], "k3.1-pro")
                self.assertEqual(sent["output_config"]["effort"], level)
                self.assertNotIn("thinking", sent)
                self.assertEqual([block["type"] for block in reply["content"]], ["thinking", "text"])

    def test_a_client_that_disables_thinking_is_never_refused(self) -> None:
        status, reply = self._post(self._turn("high", thinking={"type": "disabled"}))
        self.assertEqual(status, 200, reply)
        (sent,) = _KimiCoding.documents
        self.assertEqual(sent["model"], "k3.1-pro")
        self.assertNotIn("thinking", sent)

    def test_reasoning_is_replayed_unchanged(self) -> None:
        assistant = [{"type": "thinking", "thinking": "fixture reasoning", "signature": SIGNATURE},
                     {"type": "tool_use", "id": "toolu_kimi_1", "name": "fixture_tool", "input": {"n": 1}}]
        body = self._turn("max", thinking={"type": "adaptive"}, tools=[{
            "name": "fixture_tool", "description": "fixture", "input_schema": {"type": "object"}}], messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": assistant},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_kimi_1", "content": "1"}]},
        ])
        status, reply = self._post(body)
        self.assertEqual(status, 200, reply)
        (sent,) = _KimiCoding.documents
        self.assertEqual(sent["messages"][1], {"role": "assistant", "content": assistant})
        self.assertEqual(sent["output_config"]["effort"], "max")


if __name__ == "__main__":
    unittest.main()
