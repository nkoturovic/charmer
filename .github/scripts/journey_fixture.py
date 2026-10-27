"""Fixture helpers for .github/scripts/journey.sh (standard library only).

The journey runs this file with the installed bundle's own Python, so it
works on hosts without one. Nothing here contacts anything but loopback
listeners it starts itself, except ``client``, which only reads the
installed release's pin.

  serve --port-file F --log F [--port N] [--hold-release F]
      a keyless OpenAI-compatible chat endpoint on 127.0.0.1 (port N, or
      an ephemeral one; written to F once listening). The Nth chat request
      is answered "fixture reply N" ("INTERACTIVE-OK-N" when its last user
      message asks for "journey turn interactive"); each request is logged
      as one JSON line of metadata (method, path, model, the earlier fixture
      replies its conversation carried, whether it was held), never a
      message text. With --hold-release, a request whose last user message
      asks for "journey turn held" is logged, then answered only once that
      file exists (120 s at most): its client stays running meanwhile.
  held --log F
      exit 0 once the log records a held request, else 1.
  exec [--cwd D] -- CMD [ARG...]
      replace this process with CMD (in D): the journey's background
      helpers start their own process group first, so a managed launch
      started this way can be stopped as a whole.
  run --timeout S [--cwd D] -- CMD [ARG...]
      run CMD (in D, its own process group) with its output passed through
      and its exit status kept; past S seconds it is killed and the exit is
      124 (a command that blocks fails the step instead of hanging it).
  carried --log F --reply N --earlier M
      exit 0 when the request answered "fixture reply N" carried "fixture
      reply M" back (a resumed conversation), else 1.
  answer -- CMD [ARG...]
      run CMD on a pseudo-terminal, answer "y" to every "[y/N]" question,
      keep the credentials when an uninstall asks whether to delete them
      (Enter), and exit with its status (a journey has no human at the
      keyboard; it only ever confirms requests to these fixtures and never
      types the phrase that deletes credentials).
  listen --port N --log F [--host H] [--port-file F]
      a listener on H:N (default 127.0.0.1; port 0 picks one, written to
      the port file) that answers every request and logs whether it carried
      credentials, until it is killed.
  addresses
      the host's non-loopback IPv4 addresses, one per line.
  reach --host H --port N [--timeout S]
      exit 0 when a TCP connection to H:N opens, 1 when it does not.
  client --install DIR
      the installed release's pinned Claude Code for this host: its
      download URL, sha256 and size on one line.
  first-run --launcher CM --install DIR
      the first run's checks of a new installation (HOME's): CM doctor
      --first-run --json and CM doctor --first-run. Exit 0 (printing the
      result) only when the checks are ready, or not ready for nothing but
      the checks every fresh install fails before its setup
      (expected_unconfigured: claude, gateway, providers — nothing connected
      yet —, models and profile waiting, hooks; each by its state, its whole
      detail and its fix), with both runs agreeing; any other outcome, a
      nonzero exit for another reason included, exits 1 and says why.
  serve-files --dir D --port-file F [--cert C --key K]
      the regular files directly in D (no listing, no subdirectory) on
      127.0.0.1, an ephemeral port written to F once listening; over https
      with the certificate C and its key K when given (the Windows
      journey's loopback source of install.sh); until it is killed.
  private-dirs
      refuse any group/other-accessible mutable product directory in HOME;
      the signed release payload keeps its archive modes and is not state.
  ended --install DIR [--timeout S]
      wait (S seconds at most, default 60) until every managed session
      record of the state root (HOME's, or XDG_STATE_HOME's) has recorded
      its end, as uninstall requires before it removes anything without
      --force. Exit 0 printing how many there are; else 1, naming each
      session still open by the first 8 characters of its id and its last
      lifecycle event (metadata only, never a transcript).
  session-ids --install DIR [--open]
      the managed session ids of the state root, one per line (--open:
      only those without a recorded end).
  processes --install DIR [--session ID]
      the installed release's own process observation on this host, with
      the real system tables and tools (macOS: ps and lsof; Linux: /proc),
      against disposable processes started here: the session scan finds a
      process by either argument form (--session-id ID, --resume=ID) and
      loses it once it exits; the listener owner of a loopback port is the
      recorded process (ours), another process of this user (unknown), or
      nothing once it exits (none); the gateway identity check accepts the
      recorded PID running the recorded program, refuses it for another
      program and reports it stopped once it exits; the parent walk reaches
      this process; on macOS the executable path ps reports is the
      program's; the mount table names the home directory's filesystem;
      the daemon root reads as known (JOURNEY_DAEMON_ROOT names
      another root, for the repository's tests). With --session, the
      session's runtime id must be in the scan too (its client is running).
  scope-rules --install DIR --session ID --wsl present|absent
      the session's compiled scope denies charmer's own credential folders
      to Read and Edit, and carries the rules for the Windows side's Claude
      configuration (present, under WSL) or none of them (absent).
  claude-config
      whether this account has a Claude Code configuration yet
      (~/.claude.json, ~/.claude): "empty", or the names that exist.
  user-claude plant|record|check (--file F)
      the user's own Claude Code installation as the owned client must
      leave it: the launcher (~/.local/bin/claude), the version files
      (~/.local/share/claude/versions, or XDG_DATA_HOME's) and the
      install and update preferences (installMethod, autoUpdates and
      autoUpdatesChannel in ~/.claude.json). plant makes a disposable
      stand-in (a launcher link to a non-executable version file; with
      --preferences, the two preferences in an existing ~/.claude.json
      that has neither) only where the account has none of its own, and
      never changes or runs an existing one. record --what
      installation|preferences writes their facts to F; check compares
      them with F and names what changed.
  interactive --log F [--timeout S] [--cwd D] -- CMD [ARG...]
      the first interactive managed turn on a pseudo-terminal (CMD: the
      managed launch): the one-time screens are answered as a person
      would (charmer's launch confirmation, the text style, the security
      notes, the folder trust question: yes) and recorded in their order,
      with a line of the text each showed, in F and on stdout; then one
      turn (its fixture reply must show) and /exit, whose status must be 0.
      A run that stalls (on a screen it does not know), a missing reply or a
      run past S seconds fails, printing the screen's last lines.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pty
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "fixture-model-1"
REPLY = "fixture reply {n}"
_REPLY = re.compile(r"fixture reply ([0-9]+)")
_QUESTION = re.compile(rb"\[y/N\]\s*$")
_KEEP_CREDENTIALS = re.compile(rb"press Enter to keep them:\s*$")
HELD = "journey turn held"
HOLD_LIMIT = 120.0
INTERACTIVE = "journey turn interactive"
INTERACTIVE_REPLY = "INTERACTIVE-OK-{n}"


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _log(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _write_port(path: Path, port: int) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(f"{port}\n")
    os.replace(temporary, path)


def _last_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _text(message.get("content"))
    return ""


class _Chat(BaseHTTPRequestHandler):
    log_path: Path
    hold_release: Path | None = None
    counter = [0]
    lock = threading.Lock()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base signature
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the handler protocol
        _log(self.log_path, {"method": "GET", "path": self.path})
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]}).encode())
        else:
            self._send(404, b'{"error": {"message": "not found"}}')

    def do_POST(self) -> None:  # noqa: N802 - the handler protocol
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            request = {}
        if not (self.path.rstrip("/").endswith("/chat/completions") and isinstance(request, dict)):
            _log(self.log_path, {"method": "POST", "path": self.path})
            self._send(404, b'{"error": {"message": "not found"}}')
            return
        messages = request.get("messages") if isinstance(request.get("messages"), list) else []
        earlier = sorted({int(n) for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
                          for n in _REPLY.findall(_text(m.get("content")))})
        asked = _last_user_text(messages)
        held = self.hold_release is not None and HELD in asked
        with self.lock:
            self.counter[0] += 1
            number = self.counter[0]
        _log(self.log_path, {"method": "POST", "path": self.path, "model": request.get("model"),
                             "stream": bool(request.get("stream")), "reply": number, "carried": earlier,
                             "held": held})
        if held:
            # The client waits for this answer, so it stays running until the
            # journey releases it (or the bound passes).
            deadline = time.monotonic() + HOLD_LIMIT
            while not self.hold_release.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
        reply = (INTERACTIVE_REPLY if INTERACTIVE in asked else REPLY).format(n=number)
        usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        model = request.get("model") or MODEL
        head = {"id": f"chatcmpl-fixture-{number}", "created": 0, "model": model}
        try:
            if not request.get("stream"):
                body = {**head, "object": "chat.completion", "usage": usage,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                     "finish_reason": "stop"}]}
                self._send(200, json.dumps(body).encode())
                return
            events = [{**head, "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                      for delta in ({"role": "assistant", "content": ""}, {"content": reply})]
            events.append({**head, "object": "chat.completion.chunk", "usage": usage,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            body = (b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
                    + b"data: [DONE]\n\n")
            self._send(200, body, "text/event-stream")
        except (BrokenPipeError, ConnectionResetError):
            pass  # a held request's client was stopped before its answer


class _Files(BaseHTTPRequestHandler):
    directory: Path

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base signature
        pass

    def do_GET(self) -> None:  # noqa: N802 - the handler protocol
        name = self.path.split("?", 1)[0].lstrip("/")
        path = self.directory / name
        if "/" in name or not name or name.startswith(".") or path.is_symlink() or not path.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_files(directory: Path, port_file: Path, cert: Path | None, key: Path | None) -> int:
    import ssl

    handler = type("Files", (_Files,), {"directory": directory.resolve()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if cert is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    _write_port(port_file, server.server_address[1])
    server.serve_forever()
    return 0


def serve(port_file: Path, log_path: Path, port: int = 0, hold_release: Path | None = None) -> int:
    handler = type("Chat", (_Chat,), {"log_path": log_path, "counter": [0], "hold_release": hold_release})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True  # a held request never keeps the stopped server alive
    _write_port(port_file, server.server_address[1])
    server.serve_forever()
    return 0


def held(log_path: Path) -> int:
    try:
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        return 1
    return 0 if any(record.get("held") is True for record in records) else 1


def run_bounded(argv: list[str], timeout: float, cwd: Path | None) -> int:
    """Run ``argv`` in its own process group; 124 (and the group killed)
    when it outlives ``timeout``."""

    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        print(f"journey_fixture: {argv[0]} did not start: {exc}", file=sys.stderr)
        return 127
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        print(f"journey_fixture: {' '.join(argv[:3])} ... did not finish within {timeout:g} s", file=sys.stderr)
        return 124


def carried(log_path: Path, reply: int, earlier: int) -> int:
    try:
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError) as exc:
        print(f"journey_fixture: {log_path} is unreadable ({exc})", file=sys.stderr)
        return 1
    for record in records:
        if record.get("reply") == reply:
            if earlier in record.get("carried", []):
                return 0
            print(f"journey_fixture: the request answered with reply {reply} did not carry reply {earlier}",
                  file=sys.stderr)
            return 1
    print(f"journey_fixture: no request was answered with reply {reply}", file=sys.stderr)
    return 1


def answer(argv: list[str]) -> int:
    """Run ``argv`` on a pseudo-terminal, answering every [y/N] with y."""

    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    tail = b""
    while True:
        try:
            ready, _, _ = select.select([master], [], [], 1.0)
        except InterruptedError:
            continue
        if not ready:
            continue
        try:
            data = os.read(master, 4096)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        tail = (tail + data)[-256:]
        if _QUESTION.search(tail):
            os.write(master, b"y\n")
            tail = b""
        elif _KEEP_CREDENTIALS.search(tail):
            os.write(master, b"\n")
            tail = b""
    os.close(master)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


class _Listener(BaseHTTPRequestHandler):
    log_path: Path

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base signature
        pass

    def _record(self) -> None:
        credentials = any(self.headers.get(name) is not None for name in ("Authorization", "X-Api-Key"))
        _log(self.log_path, {"method": self.command, "path": self.path, "credentials": credentials})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"ok")

    do_GET = do_POST = do_HEAD = _record  # noqa: N815 - the handler protocol


def listen(host: str, port: int, log_path: Path, port_file: Path | None) -> int:
    handler = type("Listener", (_Listener,), {"log_path": log_path})
    server = ThreadingHTTPServer((host, port), handler)
    if port_file is not None:
        _write_port(port_file, server.server_address[1])
    server.serve_forever()
    return 0


def addresses() -> int:
    found: set[str] = set()
    try:
        for _, _, _, _, address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(address[0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # sends nothing: it only selects the source address
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    for address in sorted(found):
        if not address.startswith("127.") and address != "0.0.0.0":
            print(address)
    return 0


def reach(host: str, port: int, timeout: float) -> int:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return 0
    except OSError:
        return 1


def _installed_package(install: Path) -> bool:
    """Put the installed release's package first on the import path."""

    sites = sorted(install.glob("lib/python3.*/site-packages"))
    if not sites:
        print(f"journey_fixture: {install} is not an installed release", file=sys.stderr)
        return False
    sys.path.insert(0, str(sites[0]))
    return True


def client(install: Path) -> int:
    """URL, sha256 and size of the pinned Claude Code the installation ships."""

    if not _installed_package(install):
        return 1
    from charmer import acquire, pin, resources_root, strict_json

    contract = strict_json.load(resources_root() / "catalog" / "native-contract.json")
    version, platform = pin.version(contract), pin.host_platform()
    record = pin.platform_record(contract, platform)
    if record is None:
        print(f"journey_fixture: the release pins no Claude Code for {platform}", file=sys.stderr)
        return 1
    print(acquire.download_url(version, platform), record["sha256"], record["size"])
    return 0


# The first-run checks, in their order (doctor --first-run's contract).
FIRST_RUN_CHECKS = ("computer", "files", "policy", "claude", "gateway", "providers", "models", "profile", "hooks")


def _shown(path: Path, home: str) -> str:
    """A pattern for ``path`` as charmer may print it: under HOME as
    ``~/...``, else whole (HOME as given or resolved)."""

    whole = list(dict.fromkeys((str(path), os.path.realpath(path))))
    found = list(whole)
    for item in whole:
        for base in dict.fromkeys((home, os.path.realpath(home))):
            if base and item.startswith(base.rstrip("/") + "/"):
                found.append("~/" + os.path.relpath(item, base))
    return "(?:" + "|".join(re.escape(item) for item in dict.fromkeys(found)) + ")"


def expected_unconfigured(home: str, environ: dict[str, str] | None = None) -> dict[str, tuple[str, str, str | None]]:
    """The first-run checks a fresh installation does not pass before its
    setup (the journey installs with --no-setup), by check name: the state,
    the whole detail (a pattern) and the fix each one has. A first run that
    fails or waits on any other check, or on one of these another way,
    fails the journey."""

    env = os.environ if environ is None else environ
    state = Path(env.get("XDG_STATE_HOME") or Path(home) / ".local" / "state") / "charmer"
    return {
        # setup --step claude has not copied the pinned Claude Code yet.
        "claude": ("fail", re.escape("not set up for charmer"), "charmer setup --step claude"),
        # setup --step gateway has not recorded the gateway's port yet.
        "gateway": ("fail", re.escape("not set up yet"), "charmer setup --step gateway"),
        # Providers: nothing connected yet, so models and the profile wait.
        "providers": ("fail", re.escape("nothing connected yet"), "charmer setup --step providers"),
        "models": ("waiting", re.escape("waits for a provider"), None),
        "profile": ("waiting", re.escape("waits for a provider"), None),
        # The session helpers are written by the first command that changes state.
        "hooks": ("fail", _shown(state / "bin" / "charmer-hook", home) + re.escape(" is missing"),
                  "charmer doctor --repair-all"),
    }


def first_run_problem(json_run: subprocess.CompletedProcess, text_run: subprocess.CompletedProcess,
                      expected: dict[str, tuple[str, str, str | None]]) -> tuple[str | None, str]:
    """``(problem, result)`` of a first run's two runs (doctor --first-run
    --json and doctor --first-run): ``problem`` is None when the run
    succeeded, and ``result`` then says how."""

    try:
        report = json.loads(json_run.stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict) or report.get("version") != 1 or not isinstance(report.get("ready"), bool) \
            or not isinstance(report.get("items"), list) or not isinstance(report.get("info"), list):
        return f"doctor --first-run --json printed no first-run report (exit {json_run.returncode})", ""
    items = report["items"]
    if not all(isinstance(item, dict) for item in items) or [item.get("id") for item in items] != list(
            FIRST_RUN_CHECKS):
        return "doctor --first-run --json does not list the nine checks in order", ""
    ready = report["ready"]
    wanted = 0 if ready else 1
    if json_run.returncode != wanted:
        return f"doctor --first-run --json exited {json_run.returncode} with ready {str(ready).lower()}", ""
    if text_run.returncode != json_run.returncode:
        return f"doctor --first-run exited {text_run.returncode}, doctor --first-run --json {json_run.returncode}", ""
    failing = [item for item in items if item.get("state") in ("fail", "waiting")]
    if ready != (not failing) or report.get("next") != (failing[0]["id"] if failing else None):
        return "doctor --first-run --json disagrees with itself (ready, next and the failing checks)", ""
    lines = text_run.stdout.splitlines()
    summary = "Ready." if ready else f"Not ready: {len(failing)} item(s) need a fix"
    if not lines or lines[0] != "charmer doctor --first-run" or not lines[-1].startswith(summary):
        return "doctor --first-run and doctor --first-run --json disagree", ""
    if ready:
        return None, "ready"
    matched: list[str] = []
    for item in failing:
        check, state, detail = item["id"], item.get("state"), item.get("detail")
        fix = item.get("fix")
        fix_text = fix.get("text") if isinstance(fix, dict) else None
        condition = expected.get(check)
        if condition is None or condition[0] != state or not isinstance(detail, str) \
                or not re.fullmatch(condition[1], detail) or fix_text != condition[2]:
            return f"the first run {state}s a check a fresh install passes: {check} ({detail}; fix: {fix_text})", ""
        if not any(detail in line for line in lines):
            return f"doctor --first-run does not show the {check} check", ""
        matched.append(check)
    return None, f"not ready only for what a fresh install has not set up yet ({', '.join(matched)})"


def first_run(launcher: str, install: Path) -> int:
    """doctor's first run on a new installation (see the module docstring)."""

    if not _installed_package(install):
        return 1
    expected = expected_unconfigured(os.environ.get("HOME", ""))
    runs = []
    for argv in ([launcher, "doctor", "--first-run", "--json"], [launcher, "doctor", "--first-run"]):
        try:
            runs.append(subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                                       check=False))
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"journey_fixture: {' '.join(argv[1:])} did not run: {exc}", file=sys.stderr)
            return 1
    problem, result = first_run_problem(runs[0], runs[1], expected)
    if problem is not None:
        print(f"journey_fixture: the first run failed: {problem}", file=sys.stderr)
        for run in runs:  # doctor's own report, for the log (it never prints a secret)
            sys.stderr.write("".join(f"  | {line}\n" for line in (run.stdout + run.stderr).splitlines()[-40:]))
        return 1
    print(result)
    return 0


def session_events(root: Path) -> list[tuple[str, str | None]]:
    """The managed session records under the state root ``root``: ``(id,
    last lifecycle event)`` each, the event "end" once the session recorded
    its end. A record that cannot be read has no event (uninstall counts it
    as possibly running too)."""

    from charmer import sessions, strict_json

    directory = root / "sessions"
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    found: list[tuple[str, str | None]] = []
    for name in names:
        stem = name.removesuffix(".json")
        if name == stem or not sessions.UUID4.fullmatch(stem):
            continue
        try:
            event = sessions._lifecycle_view(strict_json.load(directory / name)).get("last_event_source")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            event = None
        found.append((stem, event if isinstance(event, str) else None))
    return found


def session_ends(root: Path) -> list[tuple[str, str | None]]:
    """:func:`session_events` with each id shortened to its first 8 characters."""

    return [(identifier[:8], event) for identifier, event in session_events(root)]


def ended(install: Path, timeout: float) -> int:
    """Wait for every managed session to record its end (module docstring)."""

    if not _installed_package(install):
        return 1
    from charmer import paths

    root = paths.state_root()
    deadline = time.monotonic() + timeout
    while True:
        records = session_ends(root)
        still = [(prefix, event) for prefix, event in records if event != "end"]
        if not still or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if still:
        for prefix, event in still:
            print(f"journey_fixture: session {prefix} has not recorded its end after {timeout:g} s "
                  f"(last event: {event or 'unreadable record'})", file=sys.stderr)
        return 1
    print(f"{len(records)} managed session record(s), every one ended")
    return 0


def session_ids(install: Path, open_only: bool) -> int:
    """The managed session ids of the state root (module docstring)."""

    if not _installed_package(install):
        return 1
    from charmer import paths

    for identifier, event in session_events(paths.state_root()):
        if not open_only or event != "end":
            print(identifier)
    return 0


class CheckError(Exception):
    """A native observation that does not hold (its message says which)."""


_SLEEPER = "import time; time.sleep(300)"
_LISTENER = ("import os, socket, sys, time\n"
             "server = socket.socket()\n"
             "server.bind(('127.0.0.1', 0))\n"
             "server.listen(8)\n"
             "open(sys.argv[1] + '.tmp', 'w').write(str(server.getsockname()[1]))\n"
             "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
             "time.sleep(300)\n")


class _Disposable:
    """A disposable child of this process (this interpreter), always reaped."""

    def __init__(self, code: str, *argv: str) -> None:
        self.process = subprocess.Popen([sys.executable, "-I", "-c", code, *argv], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pid = self.process.pid

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=30)

    def __enter__(self) -> "_Disposable":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _until(probe, accept, *, timeout: float = 10.0):
    """``probe()`` until ``accept`` takes its result (``timeout`` at most); the last result."""

    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if accept(result) or time.monotonic() >= deadline:
            return result
        time.sleep(0.1)


def native_checks(platform: str, scratch: Path, session_runtime: str | None = None) -> list[str]:
    """The process observation checks of ``processes`` (module docstring),
    with the backend ``platform`` selects ("darwin" or "linux"); one line
    per check, or CheckError naming the first that does not hold."""

    from charmer import service, sessions
    from charmer.platform import darwin_process, linux_process

    lines: list[str] = []
    program = os.path.realpath(sys.executable)
    other_program = os.path.realpath("/bin/sh")

    if platform == "darwin":
        def owner(base: str, pid: int):
            return darwin_process.listener_owner(base, pid=pid)

        def identity(pid: int, binary: str):
            return darwin_process.gateway_pid(service.ExecStamp("0", "journey", pid, binary, service.utc_now()))

        def ancestors(pid: int) -> frozenset[int]:
            return darwin_process.ancestor_pids(pid)
    else:
        namespace = os.readlink("/proc/self/ns/pid")

        def owner(base: str, pid: int):
            return linux_process.listener_owner(base, proc_root=Path("/proc"), euid=None, stamp={"pid": pid},
                                                main_pid=lambda: None)

        def identity(pid: int, binary: str):
            stamp = service.ExecStamp("0", "journey", pid, binary, service.utc_now(), namespace)
            return linux_process.gateway_pid(stamp, proc_root=Path("/proc"), readlink=os.readlink,
                                             main_pid=lambda: None)

        def ancestors(pid: int) -> frozenset[int]:
            return linux_process.ancestor_pids(pid=pid)

    # Session liveness: both argument forms, found while running, gone after.
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    with _Disposable(_SLEEPER, "--session-id", first), _Disposable(_SLEEPER, f"--resume={second}"):
        wanted = {first, second} | ({session_runtime} if session_runtime else set())
        scan = _until(sessions.proc_session_scan, lambda s: s.known and wanted <= s.ids)
        if not scan.known:
            raise CheckError(f"the process table is unreadable ({scan.reason})")
        if not {first, second} <= scan.ids:
            raise CheckError("the session scan did not find a running process by its --session-id or --resume= "
                             "argument")
        if session_runtime and session_runtime not in scan.ids:
            raise CheckError(f"the session scan did not find the running client of session {session_runtime[:8]}")
    scan = sessions.proc_session_scan()
    if not scan.known or first in scan.ids or second in scan.ids:
        raise CheckError("the session scan still finds processes that exited (or cannot read the table)")
    lines.append("session scan: ok (both argument forms while running, none after exit"
                 + (f"; the client of session {session_runtime[:8]} is running" if session_runtime else "") + ")")

    # Listener ownership of a loopback port.
    port_file = scratch / f"listener-{os.getpid()}.port"
    with _Disposable(_LISTENER, str(port_file)) as listener:
        port = _until(lambda: port_file.read_text().strip() if port_file.is_file() else "", bool)
        if not port:
            raise CheckError("the disposable listener did not start")
        base = f"http://127.0.0.1:{port}"
        ours = _until(lambda: owner(base, listener.pid), lambda verdict: verdict.kind == "ours")
        other = owner(base, os.getpid())
    gone = _until(lambda: owner(base, listener.pid), lambda verdict: verdict.kind == "none")
    kinds = (ours.kind, other.kind, gone.kind)
    if kinds != ("ours", "unknown", "none"):
        raise CheckError(f"listener ownership: the recorded process {ours.kind} ({ours.detail}), another process "
                         f"of this user {other.kind} ({other.detail}), after exit {gone.kind} ({gone.detail}); "
                         "expected ours, unknown, none")
    lines.append("listener ownership: ok (the recorded process ours, another of this user unknown, none after exit)")

    # The recorded-start identity, the parent walk and (macOS) the executable path.
    with _Disposable(_SLEEPER) as child:
        same = _until(lambda: identity(child.pid, program), lambda found: found == (child.pid, True))
        reused = identity(child.pid, other_program)
        walked = ancestors(child.pid)
        paths_seen = darwin_process.executable_paths() if platform == "darwin" else None
    stopped = _until(lambda: identity(child.pid, program), lambda found: found == (child.pid, False))
    if (same, reused, stopped) != ((child.pid, True), (None, None), (child.pid, False)):
        raise CheckError(f"process identity: the recorded program {same}, another program {reused}, after exit "
                         f"{stopped}; expected ({child.pid}, True), (None, None), ({child.pid}, False)")
    lines.append("process identity: ok (the recorded program alive, another program refused, stopped after exit)")
    if not {child.pid, os.getpid()} <= walked:
        raise CheckError(f"the parent walk from {child.pid} does not reach this process ({os.getpid()})")
    lines.append("parent walk: ok")
    if platform == "darwin":
        if paths_seen is None or not any(os.path.realpath(path) == program for path in paths_seen):
            raise CheckError(f"ps does not report the executable path {program} of a running process")
        lines.append("executable paths: ok (ps reports the program's path)")

    # The mount table (macOS: mount's output; Linux: mountinfo) names the
    # filesystem under the state root's home.
    from charmer.platform import mounts

    fstype = mounts.filesystem_type(Path.home(), platform=platform)
    if not fstype:
        raise CheckError("the mount table names no filesystem for the home directory")
    lines.append(f"mount table: ok (the home directory is on {fstype})")

    # The daemon root reads as known (nothing about its entries is printed).
    # The repository's tests name a root of their own (JOURNEY_DAEMON_ROOT).
    override = os.environ.get("JOURNEY_DAEMON_ROOT")
    daemon = (darwin_process if platform == "darwin" else linux_process).background_liveness(
        Path(override) if override else None)
    if not daemon.known:
        raise CheckError(f"the daemon root is unknown ({daemon.reason})")
    lines.append("daemon root: ok (known)")
    return lines


def processes(install: Path, session: str | None) -> int:
    """The installed release's process observation (module docstring)."""

    if not _installed_package(install):
        return 1
    import tempfile

    from charmer import paths, sessions, strict_json

    platform = "darwin" if sys.platform == "darwin" else "linux"
    runtime = None
    if session is not None:
        try:
            runtime = sessions.runtime_session_id(strict_json.load(paths.state_root() / "sessions"
                                                                   / f"{session}.json"))
        except (OSError, ValueError, sessions.SessionError) as exc:
            print(f"journey_fixture: session {session[:8]} has no readable runtime id ({exc})", file=sys.stderr)
            return 1
    with tempfile.TemporaryDirectory(prefix="journey-processes-") as scratch:
        try:
            lines = native_checks(platform, Path(scratch), runtime)
        except CheckError as exc:
            print(f"journey_fixture: native processes ({platform}): {exc}", file=sys.stderr)
            return 1
    for line in lines:
        print(f"native processes ({platform}): {line}")
    return 0


def scope_rules(install: Path, session: str, wsl: str) -> int:
    """The session's compiled credential denies (module docstring)."""

    if not _installed_package(install):
        return 1
    from charmer import paths, scope, strict_json

    path = scope.scope_dir(paths.state_root(), session) / scope.SETTINGS_RELPATH
    try:
        deny = strict_json.load(path)["permissions"]["deny"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"journey_fixture: the scope of session {session[:8]} has no readable deny rules ({exc})",
              file=sys.stderr)
        return 1
    missing = [rule for rule in scope.SECRET_PATH_DENIES if rule not in deny]
    if missing:
        print(f"journey_fixture: the scope does not deny charmer's own credentials: {', '.join(missing)}",
              file=sys.stderr)
        return 1
    windows = [rule for rule in scope.WSL_SECRET_PATH_DENIES if rule in deny]
    expected = list(scope.WSL_SECRET_PATH_DENIES) if wsl == "present" else []
    if windows != expected:
        print(f"journey_fixture: the scope carries {len(windows)} of the {len(scope.WSL_SECRET_PATH_DENIES)} "
              f"Windows-side rules; expected {wsl}", file=sys.stderr)
        return 1
    print(f"{len(scope.SECRET_PATH_DENIES)} credential rule(s); Windows-side rules {wsl}")
    return 0


def claude_config(home: Path) -> int:
    found = [name for name in (".claude.json", ".claude") if os.path.lexists(home / name)]
    print("empty" if not found else " ".join(found))
    return 0


# The user's own Claude Code installation, which the owned client must leave
# as it is. Preferences are the fields the client's own installer and
# updater read.
PREFERENCES = ("installMethod", "autoUpdates", "autoUpdatesChannel")
STAND_IN = "0.0.0-journey"


def _user_claude_paths(home: Path, environ: dict[str, str]) -> tuple[Path, list[Path], Path]:
    data = environ.get("XDG_DATA_HOME")
    versions = [Path(data) / "claude" / "versions"] if data and os.path.isabs(data) else []
    versions.append(home / ".local/share/claude/versions")
    return home / ".local/bin/claude", list(dict.fromkeys(versions)), home / ".claude.json"


def _file_fact(path: Path) -> list:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ["absent"]
    if os.path.islink(path):
        return ["link", os.readlink(path)]
    if not path.is_file():
        return ["other", oct(info.st_mode)]
    if info.st_size <= 1 << 20:
        return ["file", info.st_size, hashlib.sha256(path.read_bytes()).hexdigest()]
    return ["file", info.st_size, info.st_mtime_ns]  # a real client binary: not read in full


def user_claude_facts(home: Path, environ: dict[str, str], what: str) -> dict:
    launcher, versions, config = _user_claude_paths(home, environ)
    if what == "installation":
        facts: dict = {"launcher": _file_fact(launcher)}
        for directory in versions:
            names = sorted(os.listdir(directory)) if directory.is_dir() else None
            facts[f"versions {directory}"] = None if names is None else {
                name: _file_fact(directory / name) for name in names}
        return facts
    try:
        document = json.loads(config.read_bytes())
    except FileNotFoundError:
        document = None
    except ValueError:
        document = "unreadable"
    if not isinstance(document, dict):
        return {"preferences": document if document is None else "unreadable"}
    return {"preferences": {key: document.get(key) for key in PREFERENCES}}


def user_claude(command: str, home: Path, environ: dict[str, str], file: Path | None, what: str,
                preferences: bool) -> int:
    """The user's own Claude Code installation (module docstring)."""

    launcher, versions, config = _user_claude_paths(home, environ)
    if command == "plant":
        if preferences:
            try:
                document = json.loads(config.read_bytes())
            except FileNotFoundError:
                document = {}
            except (OSError, ValueError):
                print("journey_fixture: ~/.claude.json is unreadable", file=sys.stderr)
                return 1
            if not isinstance(document, dict):
                print("journey_fixture: ~/.claude.json is not an object", file=sys.stderr)
                return 1
            if any(key in document for key in PREFERENCES):
                print("preferences: already set, recorded and not changed")
                return 0
            document.update(installMethod="native", autoUpdates=True)
            temporary = config.with_name(config.name + ".journey")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(document))
            os.replace(temporary, config)
            print("preferences: stand-in values added (installMethod, autoUpdates)")
            return 0
        if os.path.lexists(launcher) or any(os.path.lexists(directory) for directory in versions):
            print("installation: the account's own, recorded and not changed (never run)")
            return 0
        version = versions[-1] / STAND_IN
        version.parent.mkdir(parents=True, exist_ok=True)
        version.write_bytes(b"a stand-in for the user's own Claude Code version (never run)\n")
        version.chmod(0o644)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.symlink_to(version)
        print(f"installation: stand-in planted (launcher -> versions/{STAND_IN}, not executable)")
        return 0
    assert file is not None
    if command == "record":
        file.write_text(json.dumps({"what": what, "facts": user_claude_facts(home, environ, what)}, sort_keys=True))
        print(f"{what}: recorded")
        return 0
    recorded = json.loads(file.read_text())
    now = user_claude_facts(home, environ, recorded["what"])
    changed = sorted(key for key in set(recorded["facts"]) | set(now) if recorded["facts"].get(key) != now.get(key))
    if changed:
        print(f"journey_fixture: the user's Claude Code {recorded['what']} changed: {', '.join(changed)}",
              file=sys.stderr)
        return 1
    print(f"{recorded['what']}: unchanged ({', '.join(sorted(now))})")
    return 0


# The one-time screens of a first interactive launch, in the order they come,
# each with a single-word marker (an incremental redraw may move the cursor
# between the words of a phrase) and what a person answers. The folder trust
# question preselects "No, exit": Down, then Enter once the selection glyph
# is drawn again on the "Yes" row.
_GLYPH = "❯".encode()
SCREENS = (
    ("launch-confirm", b"Enter launch", (b"\r",)),
    ("text-style", b"Choose", (b"\r",)),
    ("security-notes", b"Press", (b"\r",)),
    ("folder-trust", b"confirm", (b"\x1b[B", _GLYPH, b"\r")),
)
_ESCAPES = re.compile(rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]")


def screen_text(data: bytes) -> str:
    """Terminal output as plain text: escape sequences out, whitespace collapsed."""

    return " ".join(_ESCAPES.sub(b" ", data).decode("utf-8", "replace").split())


class _Terminal:
    """A child on a pseudo-terminal: what it printed, and keystrokes to it."""

    def __init__(self, master: int, pid: int, deadline: float) -> None:
        self.master, self.pid, self.deadline = master, pid, deadline
        self.output = bytearray()
        self.cursor = 0
        self.closed = False
        self.last_output = time.monotonic()

    def read(self, wait: float) -> None:
        if self.closed:
            time.sleep(wait)
            return
        ready, _, _ = select.select([self.master], [], [], max(0.0, wait))
        if not ready:
            return
        try:
            chunk = os.read(self.master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            chunk = b""
        if not chunk:
            self.closed = True
            return
        self.output.extend(chunk)
        self.last_output = time.monotonic()

    def wait_for(self, marker: bytes, *, limit: float) -> bool:
        """Read until ``marker`` shows after the cursor (``limit`` seconds at
        most, within the deadline); the cursor moves past it."""

        end = min(self.deadline, time.monotonic() + limit)
        while marker not in self.output[self.cursor:]:
            if time.monotonic() >= end or self.closed:
                return False
            self.read(0.2)
        self.cursor = self.output.index(marker, self.cursor) + len(marker)
        return True

    def send(self, data: bytes) -> None:
        os.write(self.master, data)
        time.sleep(0.3)  # let the dialog take the input

    def tail(self) -> str:
        return screen_text(bytes(self.output[-6000:]))[-1500:]


def interactive(argv: list[str], log_path: Path, timeout: float, cwd: Path | None) -> int:
    """The first interactive managed turn (module docstring)."""

    import fcntl
    import struct
    import termios

    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        try:
            if cwd is not None:
                os.chdir(cwd)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            env = dict(os.environ)
            if env.get("TERM", "dumb") == "dumb":
                env["TERM"] = "xterm-256color"
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    terminal = _Terminal(master, pid, time.monotonic() + timeout)
    seen: list[dict[str, str]] = []
    problem = None
    try:
        pending = list(SCREENS)
        while problem is None:
            fresh = bytes(terminal.output[terminal.cursor:])
            hit = next(((index, screen) for index, screen in enumerate(pending) if screen[1] in fresh), None)
            if hit is not None:
                index, (name, marker, answers) = hit
                text = screen_text(fresh)
                where = text.find(marker.decode())
                seen.append({"screen": name, "text": text[max(0, where - 120):where + 80]})
                terminal.cursor = len(terminal.output)
                for answer in answers:
                    if answer == _GLYPH:
                        if not terminal.wait_for(_GLYPH, limit=10):
                            problem = f"the {name} screen did not take the selection"
                            break
                    else:
                        terminal.send(answer)
                terminal.cursor = len(terminal.output)
                pending = pending[index + 1:]
                continue
            quiet = time.monotonic() - terminal.last_output
            if _GLYPH in fresh and quiet >= 2.0:
                break  # the input prompt: no known screen is waiting
            if terminal.closed:
                problem = "the launch ended before its input prompt"
            elif time.monotonic() >= terminal.deadline:
                problem = "no input prompt and no known screen (a screen this journey does not know?)"
            else:
                terminal.read(0.2)
        if problem is None:
            terminal.cursor = len(terminal.output)
            terminal.send(INTERACTIVE.encode() + b"\r")
            if not terminal.wait_for(b"INTERACTIVE-OK", limit=120):
                problem = "the turn showed no fixture reply"
        if problem is None:
            terminal.wait_for(_GLYPH, limit=20)
            time.sleep(0.5)
            terminal.send(b"/exit\r")
            end = time.monotonic() + 30
            while not terminal.closed and time.monotonic() < end:
                terminal.read(0.2)
            if not terminal.closed:
                problem = "/exit did not end the session within 30 s"
    finally:
        if problem is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _, status = os.waitpid(pid, 0)
        os.close(master)
    code = os.waitstatus_to_exitcode(status)
    names = [entry["screen"] for entry in seen]
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"screens": seen, "exit": code, "problem": problem}, sort_keys=True) + "\n")
    if problem is None and code != 0:
        problem = f"the session exited {code}"
    if problem is not None:
        print(f"journey_fixture: the first interactive turn failed: {problem}; screens so far: "
              f"{', '.join(names) or 'none'}", file=sys.stderr)
        print(f"  | {terminal.tail()}", file=sys.stderr)
        return 1
    for entry in seen:
        print(f"screen {entry['screen']}: {entry['text']}")
    print(f"screens: {', '.join(names) or 'none'}; one turn answered; /exit")
    return 0


def private_dirs(home: Path) -> int:
    """Check mutable product directories, not the immutable release payload."""

    def xdg(name: str, fallback: Path) -> Path:
        value = os.environ.get(name, "")
        return Path(value) if os.path.isabs(value) else fallback

    data = home / ".local/share/charmer"
    roots = (data, xdg("XDG_CONFIG_HOME", home / ".config") / "charmer",
             xdg("XDG_STATE_HOME", home / ".local/state") / "charmer")
    count = 0
    for root in roots:
        if not root.exists():
            continue
        for parent, children, _files in os.walk(root):
            path = Path(parent)
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                print(f"state directory {path} has group/other access ({mode:04o})", file=sys.stderr)
                return 1
            count += 1
            if path == data / "install/versions":
                # Signed, read-only bundle contents deliberately carry 0755
                # directory modes. Their enclosing mutable directories do not.
                children.clear()
    print(f"private state directories: ok ({count} checked)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="journey_fixture.py")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve")
    serve_cmd.add_argument("--port-file", type=Path, required=True)
    serve_cmd.add_argument("--log", type=Path, required=True)
    serve_cmd.add_argument("--port", type=int, default=0)
    serve_cmd.add_argument("--hold-release", type=Path)
    held_cmd = commands.add_parser("held")
    held_cmd.add_argument("--log", type=Path, required=True)
    exec_cmd = commands.add_parser("exec")
    exec_cmd.add_argument("--cwd", type=Path)
    exec_cmd.add_argument("argv", nargs=argparse.REMAINDER)
    run_cmd = commands.add_parser("run")
    run_cmd.add_argument("--timeout", type=float, required=True)
    run_cmd.add_argument("--cwd", type=Path)
    run_cmd.add_argument("argv", nargs=argparse.REMAINDER)
    ids_cmd = commands.add_parser("session-ids")
    ids_cmd.add_argument("--install", type=Path, required=True)
    ids_cmd.add_argument("--open", action="store_true")
    processes_cmd = commands.add_parser("processes")
    processes_cmd.add_argument("--install", type=Path, required=True)
    processes_cmd.add_argument("--session")
    rules_cmd = commands.add_parser("scope-rules")
    rules_cmd.add_argument("--install", type=Path, required=True)
    rules_cmd.add_argument("--session", required=True)
    rules_cmd.add_argument("--wsl", choices=("present", "absent"), required=True)
    commands.add_parser("claude-config")
    user_cmd = commands.add_parser("user-claude")
    user_cmd.add_argument("action", choices=("plant", "record", "check"))
    user_cmd.add_argument("--file", type=Path)
    user_cmd.add_argument("--what", choices=("installation", "preferences"), default="installation")
    user_cmd.add_argument("--preferences", action="store_true")
    interactive_cmd = commands.add_parser("interactive")
    interactive_cmd.add_argument("--log", type=Path, required=True)
    interactive_cmd.add_argument("--timeout", type=float, default=240.0)
    interactive_cmd.add_argument("--cwd", type=Path)
    interactive_cmd.add_argument("argv", nargs=argparse.REMAINDER)
    carried_cmd = commands.add_parser("carried")
    carried_cmd.add_argument("--log", type=Path, required=True)
    carried_cmd.add_argument("--reply", type=int, required=True)
    carried_cmd.add_argument("--earlier", type=int, required=True)
    answer_cmd = commands.add_parser("answer")
    answer_cmd.add_argument("argv", nargs=argparse.REMAINDER)
    listen_cmd = commands.add_parser("listen")
    listen_cmd.add_argument("--host", default="127.0.0.1")
    listen_cmd.add_argument("--port", type=int, required=True)
    listen_cmd.add_argument("--log", type=Path, required=True)
    listen_cmd.add_argument("--port-file", type=Path)
    commands.add_parser("addresses")
    commands.add_parser("private-dirs")
    reach_cmd = commands.add_parser("reach")
    reach_cmd.add_argument("--host", required=True)
    reach_cmd.add_argument("--port", type=int, required=True)
    reach_cmd.add_argument("--timeout", type=float, default=3.0)
    client_cmd = commands.add_parser("client")
    client_cmd.add_argument("--install", type=Path, required=True)
    first_run_cmd = commands.add_parser("first-run")
    first_run_cmd.add_argument("--launcher", required=True)
    first_run_cmd.add_argument("--install", type=Path, required=True)
    files_cmd = commands.add_parser("serve-files")
    files_cmd.add_argument("--dir", type=Path, required=True)
    files_cmd.add_argument("--port-file", type=Path, required=True)
    files_cmd.add_argument("--cert", type=Path)
    files_cmd.add_argument("--key", type=Path)
    ended_cmd = commands.add_parser("ended")
    ended_cmd.add_argument("--install", type=Path, required=True)
    ended_cmd.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.command in ("exec", "run", "interactive"):
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not command:
            parser.error(f"{args.command} needs a command after --")
        if args.command == "exec":
            if args.cwd is not None:
                os.chdir(args.cwd)
            os.execvp(command[0], command)
        if args.command == "run":
            return run_bounded(command, args.timeout, args.cwd)
        return interactive(command, args.log, args.timeout, args.cwd)
    if args.command == "serve":
        return serve(args.port_file, args.log, args.port, args.hold_release)
    if args.command == "held":
        return held(args.log)
    if args.command == "session-ids":
        return session_ids(args.install, args.open)
    if args.command == "processes":
        return processes(args.install, args.session)
    if args.command == "scope-rules":
        return scope_rules(args.install, args.session, args.wsl)
    if args.command == "claude-config":
        return claude_config(Path(os.environ["HOME"]))
    if args.command == "user-claude":
        if args.action != "plant" and args.file is None:
            parser.error(f"user-claude {args.action} needs --file")
        return user_claude(args.action, Path(os.environ["HOME"]), dict(os.environ), args.file, args.what,
                           args.preferences)
    if args.command == "carried":
        return carried(args.log, args.reply, args.earlier)
    if args.command == "answer":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not command:
            parser.error("answer needs a command after --")
        return answer(command)
    if args.command == "listen":
        return listen(args.host, args.port, args.log, args.port_file)
    if args.command == "addresses":
        return addresses()
    if args.command == "private-dirs":
        return private_dirs(Path(os.environ["HOME"]))
    if args.command == "client":
        return client(args.install)
    if args.command == "first-run":
        return first_run(args.launcher, args.install)
    if args.command == "serve-files":
        if (args.cert is None) != (args.key is None):
            parser.error("serve-files: --cert and --key go together")
        return serve_files(args.dir, args.port_file, args.cert, args.key)
    if args.command == "ended":
        return ended(args.install, args.timeout)
    return reach(args.host, args.port, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
