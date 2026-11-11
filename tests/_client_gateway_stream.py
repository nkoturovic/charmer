"""Streaming fixtures for client lanes through the candidate gateway.

The real-client lanes that put charmer's gateway between the pinned client
and a fake upstream (``test_client_gateway_refusals``,
``test_client_first_byte``) need what the buffering relay of
``test_gateway_hint_client`` cannot give: response bytes passed on as they
arrive, no relay deadline, and the moments each side saw them.

* :class:`ScriptedUpstream`: the Claude OAuth pool's upstream (the same
  CONNECT and fixture-TLS endpoint as ``_OAuthTLSUpstream``), whose replies a
  script writes step by step (:class:`UpstreamTurn`): hold, headers, chunks,
  a cut connection. :func:`start_gateway` starts the candidate gateway on it.
* :class:`StreamRelay`: the probe's allowed fake endpoint, piping each client
  request to the gateway and the gateway's bytes back unchanged.

Rows are metadata only (times from the fixture's start, routes, models,
statuses, counts); bodies and headers stay in memory.
"""

from __future__ import annotations

import json
import re
import select
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from unittest import mock
from urllib.parse import urlsplit

from charmer import probe

import _gateway_harness as harness
import test_gateway_hint_client as recognition  # module import: no tests re-exported

_COMMENT = re.compile(rb"(?:^|\n):")


def peer_closed(connection: socket.socket, wait: float) -> bool:
    """The peer closed ``connection`` within ``wait`` seconds. Nothing is
    expected from it meanwhile (an HTTP peer waiting for its answer), so a
    readable socket with no data is a closed one."""

    readable, _, _ = select.select([connection], [], [], wait)
    if not readable:
        return False
    if isinstance(connection, ssl.SSLSocket):
        previous = connection.gettimeout()
        connection.settimeout(0.5)
        try:
            return not connection.recv(1)
        except (socket.timeout, ssl.SSLWantReadError):
            return False
        except (OSError, ssl.SSLError):
            return True
        finally:
            connection.settimeout(previous)
    try:
        return not connection.recv(1, socket.MSG_PEEK)
    except OSError:
        return True


class Clock:
    def __init__(self) -> None:
        self.start = time.monotonic()

    def now(self) -> float:
        return round(time.monotonic() - self.start, 1)


class UpstreamTurn:
    """One upstream request's reply, written step by step by a script."""

    def __init__(self, handler: BaseHTTPRequestHandler, hit: dict[str, Any], clock: Clock):
        self.handler, self.hit, self.clock = handler, hit, clock
        self._chunked = False

    def hold(self, seconds: float) -> bool:
        """Wait ``seconds``; False (and ``closed`` recorded) when the gateway
        closed the connection first."""

        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if peer_closed(self.handler.connection, min(1.0, remaining)):
                self.hit["closed"] = self.clock.now()
                return False
        return True

    def headers(self, status: int = 200, content_type: str = "text/event-stream") -> None:
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", content_type)
        self.handler.send_header("Cache-Control", "no-cache")
        self.handler.send_header("Transfer-Encoding", "chunked")
        self.handler.end_headers()
        self.handler.wfile.flush()
        self._chunked = True
        self.hit["status"] = status
        self.hit["headers"] = self.clock.now()

    def chunk(self, data: bytes) -> None:
        self.handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.handler.wfile.flush()
        self.hit.setdefault("first_chunk", self.clock.now())

    def end(self) -> None:
        self.handler.wfile.write(b"0\r\n\r\n")
        self.handler.wfile.flush()
        self.hit["ended"] = self.clock.now()

    def full(self, status: int, data: bytes, content_type: str) -> None:
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", content_type)
        self.handler.send_header("Content-Length", str(len(data)))
        self.handler.end_headers()
        self.handler.wfile.write(data)
        self.handler.wfile.flush()
        self.hit["status"] = status
        self.hit["ended"] = self.clock.now()

    def events(self, body: dict[str, Any], content: list[dict[str, Any]], stop: str) -> list[bytes]:
        """The SSE frames of a canned native reply."""

        payload = probe._message_payload(content, model=body.get("model"), stop_reason=stop,
                                         message_id="msg_gateway_stream")
        return [harness.sse_frame(kind, value) for kind, value in probe._sse_message(payload).events]

    def cut(self) -> None:
        """Close the connection now, mid-reply."""

        self.hit["cut"] = self.clock.now()
        self.handler.close_connection = True
        try:
            self.handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


Script = Callable[[UpstreamTurn, str, dict[str, Any]], None]


class ScriptedUpstream:
    """The Claude OAuth pool's upstream, offline (``_OAuthTLSUpstream``'s
    CONNECT endpoint for exactly ``api.anthropic.com:443`` and its fixture
    TLS), answering through ``script(turn, route, body)``. ``hits``: one
    metadata row per request."""

    def __init__(self, path, tls, script: Script, clock: Clock | None = None):
        self.hits: list[dict[str, Any]] = []
        self.connects: list[str] = []
        self.clock = clock or Clock()
        self.lock = threading.Lock()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls[0]), str(tls[1]))
        owner = self

        class Inner(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                UpstreamTurn(self, {}, owner.clock).full(404, b"{}", "application/json")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw) if raw else {}
                hit = {"at": owner.clock.now(), "route": urlsplit(self.path).path, "model": body.get("model"),
                       "stream": bool(body.get("stream")), "main": bool(body.get("tools"))}
                with owner.lock:
                    owner.hits.append(hit)
                try:
                    script(UpstreamTurn(self, hit, owner.clock), hit["route"], body)
                except (OSError, ssl.SSLError):
                    hit.setdefault("closed", owner.clock.now())
                    self.close_connection = True

        class Outer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                owner.connects.append(self.path)
                if self.path != "api.anthropic.com:443":
                    self.send_error(403)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                try:
                    with context.wrap_socket(self.connection, server_side=True) as connection:
                        Inner(connection, self.client_address, self.server)
                except (OSError, ssl.SSLError):
                    pass
                self.close_connection = True

        self.server = probe.UnixHTTPServer(str(path), Outer)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def start_gateway(root, script: Script, clock: Clock):
    """The candidate gateway on the Claude OAuth pool path
    (``test_gateway_hint_client.start_oauth_gateway``: a file OAuth
    credential, the shipped Claude-pool render, the gateway's own HTTPS
    client) with a :class:`ScriptedUpstream` behind it. Main thread only."""

    def upstream(path, tls, respond):
        return ScriptedUpstream(path, tls, respond, clock)

    with mock.patch.object(recognition, "_OAuthTLSUpstream", upstream):
        return recognition.start_oauth_gateway(root, script)


def native_text(turn: UpstreamTurn, body: dict[str, Any], text: str = "PROBE-OK") -> None:
    """A canned native reply, streamed or not as the request asked."""

    status, data, content_type = recognition.native_reply(body, [{"type": "text", "text": text}], "end_turn")
    turn.hit["sent"] = "answer"
    turn.full(status, data, content_type)


# A chunk-size line: hex digits, optional whitespace and chunk extensions.
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}[ \t]*(?:;[^\r\n]*)?")
_LINE_LIMIT = 4096


class ChunkedBody:
    """Incremental HTTP/1.1 chunked transfer decoding: :meth:`feed` takes
    the raw bytes as they arrive and returns the payload bytes they complete
    (chunk-size lines, chunk ends and trailers removed). The framing is
    checked as it goes: a size line that is not hex digits (with optional
    extensions), a chunk not ended by CRLF, a malformed trailer or bytes
    after the last chunk set :attr:`error` and stop the decoding there; a
    framing failure is never completion. :meth:`ending` says how the body
    ended: ``complete``, ``error`` or where it was cut short."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.state = "size"
        self.remaining = 0
        self.complete = False
        self.error: str | None = None

    def feed(self, data: bytes) -> bytes:
        if self.error is not None:
            return b""
        if self.complete:
            if data:
                self.error = "bytes after the last chunk"
            return b""
        self.buffer += data
        out = bytearray()
        while self.error is None and not self.complete:
            if self.state == "data":
                if not self.buffer:
                    break
                take = min(self.remaining, len(self.buffer))
                out += self.buffer[:take]
                del self.buffer[:take]
                self.remaining -= take
                if self.remaining == 0:
                    self.state = "end"
                continue
            if self.state == "end":
                if len(self.buffer) < 2:
                    break
                if bytes(self.buffer[:2]) != b"\r\n":
                    self.error = "a chunk does not end with CRLF"
                    break
                del self.buffer[:2]
                self.state = "size"
                continue
            line_end = self.buffer.find(b"\r\n")
            if line_end < 0:
                if len(self.buffer) > _LINE_LIMIT:
                    self.error = "an over-long framing line"
                break
            line = bytes(self.buffer[:line_end])
            del self.buffer[:line_end + 2]
            if self.state == "trailer":
                if not line:
                    self.complete = True
                elif b":" not in line:
                    self.error = "a malformed trailer field"
                continue
            if not _CHUNK_SIZE.fullmatch(line):
                self.error = "a malformed chunk-size line"
                break
            size = int(line.split(b";", 1)[0].strip(), 16)
            self.state, self.remaining = ("trailer", 0) if size == 0 else ("data", size)
        if self.complete and self.buffer and self.error is None:
            self.error = "bytes after the last chunk"
        return bytes(out)

    def ending(self) -> str:
        """``error``, ``complete``, or how the bytes stopped short:
        ``truncated-mid-chunk``, ``truncated-before-chunk-end``,
        ``truncated-mid-size-line``, ``truncated-between-chunks`` or
        ``truncated-in-trailer``."""

        if self.error is not None:
            return "error"
        if self.complete:
            return "complete"
        if self.state == "data":
            return "truncated-mid-chunk"
        if self.state == "end":
            return "truncated-before-chunk-end"
        if self.state == "trailer":
            return "truncated-in-trailer"
        return "truncated-mid-size-line" if self.buffer else "truncated-between-chunks"


_STOP_REASON = re.compile(rb'"stop_reason"\s*:\s*"([a-z_]{1,40})"')


class BodyObserver:
    """What one response's body showed and when, from the gateway's raw
    bytes as they reach the relay: the head (status, transfer framing), then
    the payload with any chunked framing decoded, so a chunk-size line is no
    body byte and an SSE marker split across chunks counts when it is whole.
    ``row`` gets ``headers``, ``status``, ``first_body``, ``first_event``
    (a ``data:`` line), ``first_comment`` (a line starting with ``:``) and,
    from :meth:`finish`, the counts, the event types in order (at most 64),
    the stop reasons and ``framing``: how the body ended (``complete``,
    ``error`` with ``framing_error``, a ``truncated-*`` form, ``eof`` for a
    body that runs to the connection's end, ``no-response``). A decoded
    prefix stays observable whatever the framing; the verdicts read
    ``framing`` before trusting it."""

    def __init__(self, row: dict[str, Any], clock: Any):
        self.row, self.clock = row, clock
        self.head = bytearray()
        self.decoder: ChunkedBody | None = None
        self.length: int | None = None
        self.received = 0
        self.payload = bytearray()
        self.in_body = False

    def feed(self, data: bytes) -> None:
        if not self.in_body:
            self.head += data
            end = self.head.find(b"\r\n\r\n")
            if end < 0:
                return
            self.in_body = True
            head, rest = bytes(self.head[:end]), bytes(self.head[end + 4:])
            self.row["headers"] = self.clock.now()
            self.row["status"] = int(head.split(b" ", 2)[1])
            fields = [line.split(b":", 1) for line in head.split(b"\r\n")[1:] if b":" in line]
            if any(name.strip().lower() == b"transfer-encoding" and b"chunked" in value.lower()
                   for name, value in fields):
                self.decoder = ChunkedBody()
            else:
                lengths = [value.strip() for name, value in fields if name.strip().lower() == b"content-length"]
                self.length = int(lengths[0]) if lengths and lengths[0].isdigit() else None
            data = rest
        self.received += len(data)
        decoded = self.decoder.feed(data) if self.decoder is not None else data
        if not decoded:
            return
        self.payload += decoded
        now = self.clock.now()
        self.row.setdefault("first_body", now)
        if "first_event" not in self.row and re.search(rb"(?:^|\n)data:", self.payload):
            self.row["first_event"] = now
        if "first_comment" not in self.row and _COMMENT.search(self.payload):
            self.row["first_comment"] = now

    def ending(self) -> str:
        if not self.in_body:
            return "no-response"
        if self.decoder is not None:
            return self.decoder.ending()
        if self.length is None:
            return "eof"
        if self.received == self.length:
            return "complete"
        return "truncated-mid-body" if self.received < self.length else "error"

    def finish(self) -> None:
        payload = bytes(self.payload)
        self.row["framing"] = self.ending()
        if self.decoder is not None and self.decoder.error is not None:
            self.row["framing_error"] = self.decoder.error
        elif self.row["framing"] == "error":
            self.row["framing_error"] = "bytes beyond Content-Length"
        self.row.update(bytes=len(payload), events=len(re.findall(rb"(?:^|\n)data:", payload)),
                        comments=len(_COMMENT.findall(payload)),
                        event_types=[kind.decode() for kind in
                                     re.findall(rb"(?:^|\n)event: ?([A-Za-z_]{1,40})", payload)[:64]],
                        stop_reasons=sorted({reason.decode() for reason in _STOP_REASON.findall(payload)}))


class _StreamRelayHandler(probe._ProviderHandler):
    def _forward(self) -> None:
        relay: StreamRelay = self.server.provider  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            document = json.loads(body) if body else None
        except ValueError:
            document = None
        context = relay._record(self.command, self.path, self.headers, body, document)
        relay.forward(self, body, document if isinstance(document, dict) else {}, context)

    do_POST = _forward
    do_GET = _forward


class StreamRelay(probe.FakeAnthropicProvider):
    """The probe's fake endpoint, piping each client request to the gateway
    (transport address and gateway token only, ``Connection: close``) and the
    gateway's response bytes back as they arrive: nothing buffered, no relay
    deadline. A client that closes its connection first closes the gateway
    request too. ``rows``: per request, when the gateway's headers, first
    body byte, first SSE event and first SSE comment reached the relay, the
    counts, event types and stop reasons (:class:`BodyObserver`, transfer
    framing decoded), and who closed first; ``describe(path, headers,
    document)`` adds the request's own bounded facts."""

    def __init__(self, gateway, clock: Clock,
                 describe: Callable[[str, Any, dict[str, Any]], dict[str, Any]] | None = None):
        super().__init__()
        self.gateway = gateway
        self.clock = clock
        self.describe = describe
        self.rows: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def start(self):
        super().start()
        self._server.RequestHandlerClass = _StreamRelayHandler
        return self

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = _StreamRelayHandler
        return path

    def forward(self, handler: BaseHTTPRequestHandler, body: bytes, document: dict[str, Any], context) -> None:
        hints = dict(context.record.hint_header_values)
        row: dict[str, Any] = {"at": self.clock.now(), "method": handler.command,
                               "route": urlsplit(handler.path).path, "model": document.get("model"),
                               "class": hints.get("x-claude-code-request-class") or "main",
                               "stream": bool(document.get("stream")), "main": bool(document.get("tools"))}
        if self.describe is not None:
            row["request"] = self.describe(handler.path, handler.headers, document)
        with self.lock:
            self.rows.append(row)
        headers = {name.lower(): value for name, value in handler.headers.items()}
        headers.pop("x-api-key", None)
        headers.update({"host": f"127.0.0.1:{self.gateway.port}", "connection": "close",
                        "authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN,
                        "content-length": str(len(body))})
        request = (f"{handler.command} {handler.path} HTTP/1.1\r\n"
                   + "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n").encode()
        client = handler.connection
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        observer = BodyObserver(row, self.clock)
        try:
            upstream.connect(str(self.gateway.socket))
            upstream.sendall(request + body)
            while True:
                readable, _, _ = select.select([upstream, client], [], [], 1.0)
                if client in readable and peer_closed(client, 0):
                    row["client_closed"] = self.clock.now()
                    break
                if upstream not in readable:
                    continue
                data = upstream.recv(65536)
                if not data:
                    row["gateway_closed"] = self.clock.now()
                    break
                row.setdefault("first_bytes", self.clock.now())
                observer.feed(data)
                try:
                    client.sendall(data)
                except OSError:
                    row["client_closed"] = self.clock.now()
                    break
        except OSError as exc:
            row["error"] = type(exc).__name__
        finally:
            upstream.close()
            handler.close_connection = True
        observer.finish()
        row["ended"] = self.clock.now()
