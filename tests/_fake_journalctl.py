"""A fake ``journalctl`` for the persistence hold's journal reads.

:func:`install` writes an executable ``journalctl`` into a test's own
directory (its shebang is this interpreter with ``-I``) and the JSON fixture
it serves; a test puts that directory alone on ``PATH`` and asserts, from
the calls log, that the fake was the program called. The host's journalctl
is never run.

The fake honours what the hold's readers pass: ``--user``/``--system``,
``-u UNIT`` (an entry's ``_SYSTEMD_USER_UNIT``), ``FIELD=VALUE`` matches,
``--since @S``/``--until @S``, ``-n N``, ``--reverse``, ``--all`` (without
it a field over 4096 bytes is null, as journalctl's JSON output does),
``-o json``, ``--output-fields`` (``__CURSOR`` and ``__REALTIME_TIMESTAMP``
always come along), ``--case-sensitive`` and ``--grep`` in byte mode (a
regular expression searched in MESSAGE's bytes; entries without MESSAGE
never match). Like systemd 259 it exits 1 with no output when ``--grep``
matched nothing. Modes: ``no-pcre`` (any ``--grep``: "PCRE2 support is not
compiled in." on stderr, exit 1), ``error`` (every call exits 1),
``system-unreadable`` (``--system`` shows nothing), ``until-error`` (an
``--until`` read exits 1) and ``slow`` (sleeps before answering).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable, Mapping

_PROGRAM = r'''
import json, re, sys, time

FIXTURE = {fixture!r}
CALLS = {calls!r}


def main(argv):
    with open(CALLS, "a", encoding="utf-8") as calls:
        calls.write(json.dumps(argv) + "\n")
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    modes = set(fixture["modes"])
    if "slow" in modes:
        time.sleep(fixture.get("sleep", 30))
    if "error" in modes:
        sys.stderr.write("Failed to open journal\n")
        return 1
    scope = "system" if "--system" in argv else "user"
    unit = since = until = grep = fields = None
    count = None
    reverse = full = False
    matches = []
    output = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else None
        if arg in ("--user", "--system", "--no-pager"):
            pass
        elif arg == "-u":
            unit, index = value, index + 1
        elif arg == "--since":
            since, index = value, index + 1
        elif arg == "--until":
            until, index = value, index + 1
        elif arg == "-n":
            count, index = int(value), index + 1
        elif arg == "-o":
            output, index = value, index + 1
        elif arg == "--reverse":
            reverse = True
        elif arg == "--all":
            full = True
        elif arg.startswith("--output-fields="):
            fields = arg.split("=", 1)[1].split(",")
        elif arg.startswith("--case-sensitive"):
            pass
        elif arg.startswith("--grep="):
            grep = arg.split("=", 1)[1]
        elif "=" in arg and not arg.startswith("-"):
            matches.append(arg.split("=", 1))
        else:
            sys.stderr.write(f"unknown argument {{arg}}\n")
            return 2
        index += 1
    if output != "json":
        sys.stderr.write("the fake serves -o json only\n")
        return 2
    if grep is not None and "no-pcre" in modes:
        sys.stderr.write("PCRE2 support is not compiled in.\n")
        return 1
    if until is not None and "until-error" in modes:
        return 1
    if scope == "system" and "system-unreadable" in modes:
        return 0
    entries = list(fixture[scope])

    def micros(entry):
        return int(entry["__REALTIME_TIMESTAMP"])

    if unit is not None:
        name = unit if "." in unit else unit + ".service"
        entries = [e for e in entries if e.get("_SYSTEMD_USER_UNIT") == name]
    for field, value in matches:
        entries = [e for e in entries if isinstance(e.get(field), str) and e[field] == value]
    if since is not None:
        entries = [e for e in entries if micros(e) >= int(since.removeprefix("@")) * 1_000_000]
    if until is not None:
        entries = [e for e in entries if micros(e) <= int(until.removeprefix("@")) * 1_000_000]
    if grep is not None:
        pattern = re.compile(grep.encode())

        def raw(entry):
            message = entry.get("MESSAGE")
            if isinstance(message, str):
                return message.encode()
            if isinstance(message, list):
                return bytes(message)
            return None

        entries = [e for e in entries if raw(e) is not None and pattern.search(raw(e))]
        if not entries:
            return 1
    entries.sort(key=micros)
    if count is not None:
        entries = entries[-count:] if count else []
    if reverse:
        entries.reverse()
    out = sys.stdout.buffer
    for entry in entries:
        shown = {{}}
        for key, value in entry.items():
            if fields is not None and key not in fields and key not in ("__CURSOR", "__REALTIME_TIMESTAMP"):
                continue
            size = len(value.encode()) if isinstance(value, str) else len(value) if isinstance(value, list) else 0
            shown[key] = None if (size > 4096 and not full) else value
        out.write(json.dumps(shown, separators=(",", ":")).encode() + b"\n")
    out.flush()
    return 0


sys.exit(main(sys.argv[1:]))
'''


def write_fixture(directory: Path, user_entries: Iterable[Mapping], system_entries: Iterable[Mapping] = (),
                  mode: str | Iterable[str] | None = None, *, sleep: float = 30) -> None:
    """Replace what the installed fake serves (a vacuum, a rotation)."""

    modes = [mode] if isinstance(mode, str) else list(mode or ())
    fixture = {"user": [dict(entry) for entry in user_entries],
               "system": [dict(entry) for entry in system_entries], "modes": modes, "sleep": sleep}
    path = Path(directory) / "journal-fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")


def install(directory: Path, user_entries: Iterable[Mapping], system_entries: Iterable[Mapping] = (),
            mode: str | Iterable[str] | None = None, *, sleep: float = 30) -> Path:
    """Write the fake ``journalctl`` and its fixture into ``directory``;
    returns the calls log (one JSON argv per line)."""

    directory = Path(directory)
    calls = directory / "journalctl-calls.jsonl"
    calls.write_text("", encoding="utf-8")
    write_fixture(directory, user_entries, system_entries, mode, sleep=sleep)
    program = directory / "journalctl"
    program.write_text(f"#!{sys.executable} -I\n"
                       + _PROGRAM.format(fixture=str(directory / "journal-fixture.json"), calls=str(calls)),
                       encoding="utf-8")
    os.chmod(program, 0o755)
    return calls


def calls(log: Path) -> list[list[str]]:
    """The argv of every call the fake answered, in order."""

    return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines() if line]


def entry(message: str | list[int] | None, at_micros: int, *, unit: str = "charmer-gateway.service",
          boot: str = "a" * 32, invocation: str = "b" * 32, cursor: str | None = None,
          **fields: str) -> dict:
    """One user-journal entry of ``unit`` (``message`` None: no MESSAGE field)."""

    item = {"__CURSOR": cursor or f"s=fake;i={at_micros:x}", "__REALTIME_TIMESTAMP": str(at_micros),
            "_BOOT_ID": boot, "_SYSTEMD_INVOCATION_ID": invocation, "_SYSTEMD_USER_UNIT": unit}
    if message is not None:
        item["MESSAGE"] = message
    item.update(fields)
    return item


def drop_notice(at_micros: int, *, uid: int, dropped: str | None, user_unit: str = "charmer-gateway.service") -> dict:
    """journald's rate-limit notice in the system journal for the user's services."""

    item = {"__CURSOR": f"s=fake-system;i={at_micros:x}", "__REALTIME_TIMESTAMP": str(at_micros),
            "MESSAGE_ID": "a596d6fe7bfa4994828e72309e95d61e", "_UID": "0", "OBJECT_UID": str(uid),
            "OBJECT_SYSTEMD_UNIT": f"user@{uid}.service", "OBJECT_SYSTEMD_USER_UNIT": user_unit,
            "MESSAGE": f"Suppressed {dropped or 'some'} messages from user@{uid}.service"}
    if dropped is not None:
        item["N_DROPPED"] = dropped
    return item


def system_entry(at_micros: int) -> dict:
    """An ordinary system-journal entry (what the readability probe sees)."""

    return {"__CURSOR": f"s=fake-system;i={at_micros:x}", "__REALTIME_TIMESTAMP": str(at_micros),
            "_UID": "0", "MESSAGE": "an ordinary system message"}
