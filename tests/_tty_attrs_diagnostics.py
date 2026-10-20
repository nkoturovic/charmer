"""One alias fixture's B/S/R/A/F restoration observations, never input tracing.

B: existing alias_before; S: actual saved _terminal_attrs return objects;
R: original restore setter requests/outcomes; A: one original getter after
successful R; F: existing final getter results, returned unchanged for == B.
Only A adds termios reads (at most two). No additional setters, input, polls,
native-C interception, flushes, signals, waits or descriptor control.

Snapshots are immutable/deduplicated. attrs rows hold all six integers and a
cc-table reference; cc tokens bHEX/iDEC preserve bytes versus int exactly.
D lists changed AVAILABLE pairs: i contains field indices or [6, cc_index];
x contains unmasked flag XORs plus optional known-name annotations. Absence
from D alone never proves equality. INCOMPLETE explicitly marks missing,
unexpected or oversized observations. An instrumented pass is not a fix.
"""

from __future__ import annotations

import json
import os
import sys
import termios
from types import CodeType


_LIMIT = 4096
_PREFIX = b"TTY_ATTRS_DIAGNOSTICS="
_MISSING = object()
_PHASES = ("B", "S", "R", "A", "F")
_FLAG_NAMES = (
    ("IGNBRK", "BRKINT", "IGNPAR", "PARMRK", "INPCK", "ISTRIP", "INLCR", "IGNCR",
     "ICRNL", "IXON", "IXOFF", "IXANY", "IMAXBEL", "IUTF8"),
    ("OPOST", "ONLCR", "OCRNL", "ONOCR", "ONLRET", "OXTABS"),
    ("CSTOPB", "CREAD", "PARENB", "PARODD", "HUPCL", "CLOCAL", "CSIZE"),
    ("ECHO", "ECHOE", "ECHOK", "ECHONL", "ICANON", "ISIG", "IEXTEN", "NOFLSH",
     "TOSTOP", "ECHOCTL", "ECHOKE", "FLUSHO", "PENDIN", "EXTPROC"),
)


def _argument(args, kwargs, index, name):
    return args[index] if len(args) > index else kwargs.get(name)


class AttrsDiagnostics:
    def __init__(self, tui, alias_fds, alias_before):
        self.stages = {phase: [] for phase in _PHASES}
        self.counts = dict.fromkeys(_PHASES, 0)
        self.snapshots: list[tuple] = []
        self.issues: set[str] = set()
        self.emitted = False
        self.alias_fds = tuple(alias_fds)
        self.getattr_original = termios.tcgetattr
        self.setattr_original = termios.tcsetattr
        self.terminal_original = tui._terminal_attrs
        self.save_code = tui.run_curses_on_streams.__code__
        candidates = [item for item in self.save_code.co_consts
                      if isinstance(item, CodeType) and item.co_name == "restore_attrs"]
        self.restore_code = candidates[0] if len(candidates) == 1 else None
        if self.restore_code is None:
            self.issues.add("restore-callsite")
        if len(self.alias_fds) != 2 or len(alias_before) != 2:
            self.issues.add("B-count")
        for fd, attrs in zip(self.alias_fds[:2], alias_before[:2]):
            row = self.record("B", fd, attrs)
            try:
                info = os.fstat(fd)  # Only the two owned baseline alias identities.
                row["id"] = [info.st_dev, info.st_ino, info.st_rdev, info.st_mode]
            except OSError as exc:
                row["id_error"] = self.error(exc)
                self.issues.add("B-identity")

    def error(self, exc: BaseException) -> list:
        number = getattr(exc, "errno", None)
        if type(number) is not int:
            number = exc.args[0] if exc.args and type(exc.args[0]) is int else None
        name = type(exc).__name__
        if len(name) > 64:
            self.issues.add("exception-type-overflow")
        return [name[:64], number]  # Never exception text.

    def reference(self, phase: str, attrs):
        # Bound diagnostic memory without normalizing any observed bit/value.
        if (type(attrs) not in (list, tuple) or len(attrs) != 7
                or any(type(item) is not int or item.bit_length() > 128 for item in attrs[:6])
                or type(attrs[6]) not in (list, tuple) or len(attrs[6]) > 64):
            self.issues.add(phase + "-shape")
            return None
        if len(attrs[6]) != termios.NCCS:
            self.issues.add(phase + "-cc-count")
        cc = []
        for value in attrs[6]:
            if type(value) is bytes and len(value) <= 16:
                cc.append("b" + value.hex())
                if len(value) != 1:
                    self.issues.add(phase + "-cc-width")
            elif type(value) is int and value.bit_length() <= 128:
                cc.append("i" + str(value))
            else:
                self.issues.add(phase + "-shape")
                return None
        frozen = (*attrs[:6], tuple(cc))
        if frozen not in self.snapshots:
            if len(self.snapshots) == 10:
                self.issues.add("snapshot-overflow")
                return None
            self.snapshots.append(frozen)
        return self.snapshots.index(frozen)

    def record(self, phase: str, fd, attrs=_MISSING, **facts):
        self.counts[phase] += 1
        if self.counts[phase] > 2:
            self.issues.add(phase + "-count")
            return None
        if type(fd) is not int:
            self.issues.add(phase + "-fd-shape")
            fd = None
        row = dict(fd=fd, **facts)
        if attrs is not _MISSING:
            row["a"] = self.reference(phase, attrs)
        self.stages[phase].append(row)
        return row

    def attach(self, tui) -> None:
        observer = self

        def terminal_attrs(*args, **kwargs):
            # Only the direct call in run_curses_on_streams's saved_attrs loop.
            # Retain the code object, never the frame or its locals.
            if sys._getframe(1).f_code is not observer.save_code:
                return observer.terminal_original(*args, **kwargs)
            fd = _argument(args, kwargs, 0, "fd")
            try:
                result = observer.terminal_original(*args, **kwargs)
            except BaseException as exc:
                observer.record("S", fd, e=observer.error(exc))
                observer.issues.add("S-error")
                raise
            observer.record("S", fd, result)
            return result  # The exact object consumed by the product save loop.

        def tcsetattr(*args, **kwargs):
            if sys._getframe(1).f_code is not observer.restore_code:
                return observer.setattr_original(*args, **kwargs)
            fd = _argument(args, kwargs, 0, "fd")
            action = _argument(args, kwargs, 1, "when")
            attrs = _argument(args, kwargs, 2, "attributes")
            row = observer.record("R", fd, attrs)
            if row is not None:
                row["when"] = action if type(action) is int else None
            if type(action) is not int:
                observer.issues.add("R-action-shape")
            try:
                result = observer.setattr_original(*args, **kwargs)
            except BaseException as exc:
                if row is not None:
                    row.update(ok=False, e=observer.error(exc))
                raise
            if row is not None:
                row["ok"] = True
                if type(fd) is int and fd in (0, 1):
                    try:
                        actual = observer.getattr_original(fd)  # The only extra termios read.
                    except (OSError, termios.error) as exc:
                        observer.record("A", fd, e=observer.error(exc))
                        observer.issues.add("A-readback-error")
                    else:
                        observer.record("A", fd, actual)
                else:
                    observer.issues.add("A-unowned-fd")
            return result  # Expected readback errors cannot replace setter success.

        tui._terminal_attrs = terminal_attrs
        termios.tcsetattr = tcsetattr

    def final_getattr(self, *args, **kwargs):
        fd = _argument(args, kwargs, 0, "fd")
        try:
            result = self.getattr_original(*args, **kwargs)
        except BaseException as exc:
            self.record("F", fd, e=self.error(exc))
            self.issues.add("F-error")
            self.emit()
            raise
        self.record("F", fd, result)
        return result  # Reused, not re-read or normalized, by the original ==.

    def phase_reference(self, phase, slot):
        fd = self.alias_fds[slot] if phase in ("B", "F") and slot < len(self.alias_fds) else slot
        return next((row.get("a") for row in self.stages[phase] if row["fd"] == fd), None)

    def differences(self) -> list:
        differences = []
        for slot in (0, 1):
            for first, last in (("B", "S"), ("S", "R"), ("R", "A"), ("B", "F"), ("A", "F")):
                before, after = self.phase_reference(first, slot), self.phase_reference(last, slot)
                if before is None or after is None:
                    continue
                left, right = self.snapshots[before], self.snapshots[after]
                indices = [index for index in range(6) if left[index] != right[index]]
                indices.extend([6, index] for index in range(max(len(left[6]), len(right[6])))
                               if index >= len(left[6]) or index >= len(right[6]) or left[6][index] != right[6][index])
                if not indices:
                    continue
                row = {"p": [first + str(slot), last + str(slot)], "i": indices}
                flags = []
                for index in range(4):
                    xor = left[index] ^ right[index]
                    if xor:
                        # Annotations only: all bits remain in the snapshots/XOR.
                        names = [name for name in _FLAG_NAMES[index] if xor & getattr(termios, name, 0)]
                        flags.append([index, xor, names])
                if flags:
                    row["x"] = flags
                differences.append(row)
        return differences

    def document(self) -> dict:
        for phase in ("B", "S", "R", "F"):
            if self.counts[phase] != 2:
                self.issues.add(phase + "-count")
        successful = [row["fd"] for row in self.stages["R"] if row.get("ok") is True]
        if self.counts["A"] != len(successful):
            self.issues.add("A-count")
        for phase in ("S", "R"):
            if [row["fd"] for row in self.stages[phase]] != [0, 1]:
                self.issues.add(phase + "-fds")
        if [row["fd"] for row in self.stages["A"]] != successful:
            self.issues.add("A-fds")
        if [row["fd"] for row in self.stages["F"]] != list(self.alias_fds):
            self.issues.add("F-fds")
        cc_tables, attrs = [], []
        for snapshot in self.snapshots:
            if snapshot[6] not in cc_tables:
                cc_tables.append(snapshot[6])
            attrs.append([*snapshot[:6], cc_tables.index(snapshot[6])])
        return {"schema": 1, "status": "INCOMPLETE" if self.issues else "COMPLETE",
                "layout": "attrs=[iflag,oflag,cflag,lflag,ispeed,ospeed,cc_ref]; cc=bHEX|iDEC",
                "counts": dict(self.counts), "issues": sorted(self.issues),
                "records_omitted": sum(max(0, count - 2) for count in self.counts.values()),
                "cc": cc_tables, "attrs": attrs, **self.stages, "D": self.differences()}

    def emit(self) -> None:
        if self.emitted:
            return
        self.emitted = True
        report = self.document()

        def encode(document):
            return _PREFIX + json.dumps(document, separators=(",", ":")).encode("ascii") + b"\n"

        payload = encode(report)
        if len(payload) > _LIMIT:
            original_size = len(payload)
            report.update(status="INCOMPLETE", issues=sorted(self.issues | {"record-overflow"}),
                          full_bytes=original_size, omitted=["D"])
            del report["D"]
            payload = encode(report)
            if len(payload) > _LIMIT:
                report = {"schema": 1, "status": "INCOMPLETE",
                          "issues": sorted(self.issues | {"record-overflow"}),
                          "counts": dict(self.counts), "full_bytes": original_size,
                          "records_omitted": sum(max(0, count - 2) for count in self.counts.values()),
                          "snapshot_count": len(self.snapshots),
                          "omitted": ["cc", "attrs", *_PHASES, "D"]}
                payload = encode(report)
        try:
            os.write(2, payload)  # One existing stderr write, before the fixture's exact assert.
        except OSError:
            pass  # Diagnostic output cannot replace the fixture's original assertion.


def install(tui, alias_streams, alias_before) -> AttrsDiagnostics:
    observer = AttrsDiagnostics(tui, [stream.fileno() for stream in alias_streams], alias_before)
    observer.attach(tui)
    return observer


def pipe_summary(pipe_output: bytes) -> str:
    size = len(pipe_output)
    if size > 8192:
        observer_at = pipe_output.rfind(_PREFIX)
        end = min(size, observer_at + _LIMIT) if observer_at >= 0 else size
        shown = pipe_output[:2048] + b"\n...<pipe truncated>...\n" + pipe_output[max(0, end - 6144):end]
    else:
        shown = pipe_output
    return f"pipe_bytes={size}; pipe_truncated={size > 8192}; pipe_output={shown!r}"
