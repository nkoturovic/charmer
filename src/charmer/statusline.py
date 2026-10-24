"""The status lines charmer renders for Claude Code (standard library only).

``charmer statusline subagents`` is the managed sessions' compiled
``subagentStatusLine`` (Claude Code 2.1.293+). The client writes one JSON
object to stdin, with ``columns`` and a ``tasks`` array, and reads one JSON
line ``{"id": ..., "content": ...}`` per row it should draw instead of its
own: an omitted row keeps the native default (an empty ``content`` would
hide it). A row shows the agent type, its name when that differs, the
model (a charmer gateway selector as ``requested: <selector>``: the
selector the agent asked for, not proof of the model the gateway served),
the effort, the context use, the status when it is not running and the
label fitted to the width. Only the label is fitted; the client clips a
row whose fixed segments are wider than the panel.

Claude Code runs the command every few seconds while a subagent row is
visible, so this module imports ``json``, ``os``, ``re`` and ``sys`` and
the selector-prefix leaf only: never the catalog, the command-line runtime
or a configuration reader. It never raises and always exits 0: input it
cannot use prints nothing, and the client keeps its own rows.
"""

from __future__ import annotations

import json
import os
import re
import sys

from .selector_prefixes import GATEWAY_SELECTOR_PREFIXES

# ``charmer statusline <kind>``.
KINDS = ("subagents", "main")
# The largest stdin document read; a longer one prints nothing.
MAX_INPUT_BYTES = 4 << 20
# The label budget when the client reports no width.
FALLBACK_LABEL_COLUMNS = 60
# A label is dropped when fewer columns than this are left for it.
MIN_LABEL_COLUMNS = 8
ELLIPSIS = "…"

RESET = "\x1b[0m"
CYAN, BLUE, MAGENTA, YELLOW = "36", "34", "35", "33"
GREEN, RED, DIM = "32", "31", "2"

# Display text may carry no terminal control: C0, DEL, C1 and the Unicode
# line and paragraph separators become spaces; every other character,
# non-ASCII included, is kept. Only the renderer's own colour codes reach
# the terminal.
_CONTROL_TO_SPACE = {code: " " for code in (*range(0x20), *range(0x7F, 0xA0), 0x2028, 0x2029)}


def _color(code: str) -> str:
    return f"\x1b[{code}m"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _first(*values: object) -> object:
    """The first value that is neither null nor false (``a // b``)."""

    for value in values:
        if value is not None and value is not False:
            return value
    return None


def _text(value: object) -> str:
    """A value as display text: a string as it is, any other value as JSON."""

    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e17:
        return str(int(value))
    if _is_number(value):
        return repr(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sanitize(value: object) -> str:
    """Display text without terminal controls (see ``_CONTROL_TO_SPACE``)."""

    return _text(value).translate(_CONTROL_TO_SPACE)


def _floor(number: float) -> int:
    whole = int(number)
    return whole - 1 if number < whole else whole


def _round_half_up(number: float) -> int:
    whole = _floor(number)
    return whole + 1 if number - whole >= 0.5 else whole


def format_tokens(count: float) -> str:
    """``900``, ``250K``, ``1.0M``: thousands truncated, millions to one decimal."""

    if count >= 1_000_000:
        tenths = _round_half_up(count / 100_000)
        return f"{_floor(tenths / 10)}.{tenths % 10}M"
    if count >= 1000:
        return f"{_floor(count / 1000)}K"
    return str(_floor(count))


def _segment(plain: str, code: str) -> tuple[str, str]:
    """``(plain text, coloured text)``; widths are measured on the plain text."""

    return plain, _color(code) + plain + RESET


def _context_segment(task: dict) -> list[tuple[str, str]]:
    tokens = task.get("tokenCount")
    if not _is_number(tokens):
        return []
    window = task.get("contextWindowSize")
    if not (_is_number(window) and window > 0):
        return [_segment(format_tokens(tokens), DIM)]
    percent = _floor(float(tokens) * 100.0 / float(window))
    code = RED if percent >= 85 else YELLOW if percent >= 60 else GREEN
    usage = f"{format_tokens(tokens)}/{format_tokens(window)}"
    return [(f"{percent}% {usage}", _color(code) + f"{percent}%" + RESET + " " + _color(DIM) + usage + RESET)]


def _row(task: dict, columns: float) -> dict[str, str] | None:
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        return None
    agent = sanitize(_first(task.get("agentType"), task.get("type"), "agent"))
    name = task.get("name")
    name = sanitize(name) if isinstance(name, str) else name
    model = task.get("model")
    model = sanitize(model) if isinstance(model, str) else model
    effort = task.get("effort")
    effort = None if effort is None else sanitize(effort)
    status = task.get("status")
    status = None if status is None else sanitize(status)
    label = sanitize(_first(task.get("label"), task.get("description"), ""))

    who = [_segment(agent, CYAN)]
    if isinstance(name, str) and name and name != agent:
        who.append(_segment(name, DIM))
    if isinstance(model, str) and model:
        if model.startswith(GATEWAY_SELECTOR_PREFIXES):
            who.append((f"requested: {model}",
                        _color(MAGENTA) + "requested: " + RESET + _color(BLUE) + model + RESET))
        else:
            who.append(_segment(model, BLUE))
    rest: list[tuple[str, str]] = []
    if effort:
        rest.append(_segment(effort, YELLOW))
    rest += _context_segment(task)
    if status is not None and status != "running":
        rest.append(_segment("done", DIM) if status == "completed" else _segment(status, RED))

    fixed = len(" ".join(plain for plain, _ in who) + "".join(" · " + plain for plain, _ in rest))
    room = _floor(columns - fixed - 3) if columns > 0 else FALLBACK_LABEL_COLUMNS
    if not label or room < MIN_LABEL_COLUMNS:
        label = ""
    elif len(label) > room:
        label = label[:room - 1] + ELLIPSIS
    dot = " " + _color(DIM) + "·" + RESET + " "
    content = " ".join(coloured for _, coloured in who) + "".join(dot + coloured for _, coloured in rest)
    if label:
        content += dot + _color(DIM) + label + RESET
    return {"id": task_id, "content": content}


def render_subagent_rows(document: object) -> list[dict[str, str]]:
    """The overridden rows for one decoded stdin document, in task order.

    A row only for a task whose ``id`` is a non-empty string; the id is
    returned exactly as given. A document that is not an object, a
    ``tasks`` that is not a list, a task that is not an object or a
    ``columns`` that is not a number gives no rows at all. A task whose own
    values cannot be rendered keeps its native row. Never raises."""

    if not isinstance(document, dict):
        return []
    tasks = _first(document.get("tasks"), [])
    columns = _first(document.get("columns"), 0)
    if columns is True:
        columns = 0
    if not isinstance(tasks, list) or not _is_number(columns):
        return []
    if not all(isinstance(task, dict) for task in tasks):
        return []
    rows = []
    for task in tasks:
        try:
            row = _row(task, columns)
        except (ArithmeticError, ValueError, TypeError, RecursionError):
            row = None
        if row is not None:
            rows.append(row)
    return rows


def _reject_constant(name: str) -> object:
    raise ValueError(f"not JSON: {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")):
        return sys.float_info.max if value > 0 else -sys.float_info.max
    return value


def decode(data: bytes) -> object:
    """One JSON document (invalid UTF-8 replaced; NaN and Infinity are not
    JSON; an out-of-range number is the largest finite one), or None."""

    if len(data) > MAX_INPUT_BYTES:
        return None
    try:
        return json.loads(data.decode("utf-8", "replace"), parse_constant=_reject_constant,
                          parse_float=_finite_float)
    except (ValueError, RecursionError):
        return None


def subagent_output(data: bytes) -> bytes:
    """stdout for one stdin document: one compact JSON line per row."""

    try:
        rows = render_subagent_rows(decode(data))
        return b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode("ascii") for row in rows)
    except Exception:  # the status line never fails
        return b""


def _read_stdin(stream: object) -> bytes:
    buffer = getattr(stream, "buffer", stream)
    data = buffer.read(MAX_INPUT_BYTES + 1)
    return data if isinstance(data, bytes) else data.encode("utf-8", "surrogateescape")


def _write_stdout(stream: object, data: bytes) -> None:
    if not data:
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        stream.write(data.decode("ascii"))
        stream.flush()


def run(kind: str, stdin: object = None, stdout: object = None, environ: object = None) -> int:
    """``charmer statusline <kind>``: read stdin, write the lines, exit 0."""

    try:
        data = _read_stdin(sys.stdin if stdin is None else stdin)
        if kind == "subagents":
            output = subagent_output(data)
        else:
            output = b""
        _write_stdout(sys.stdout if stdout is None else stdout, output)
    except Exception:  # the status line never fails, whatever the streams do
        pass
    return 0
