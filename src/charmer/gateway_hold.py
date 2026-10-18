"""The persistence hold: no product path stops or restarts the gateway while a
credential save may be unresolved.

A gateway that failed to persist a refreshed OAuth credential may hold the
only valid copy in memory; stopping it would lose that credential. Every stop
and restart path evaluates :func:`evaluate` first and refuses while it holds.
The hold is computed by ``gateway_events.recovery_hold`` over the backend's
log source:

* on-demand: every retained instance log file (``platform.file_log``), each
  read from the position the user last cleared it to;
* systemd: the unit's journal from the earlier of 24 hours ago and the
  running instance's start; while an instance with a start nonce runs, the
  last clear only filters which saves count, otherwise the read starts at
  the clear. A running instance's failure therefore never ages out of the
  evaluation. The running instance's start line must be in the read, or,
  when the last clear is at or after that start, the journal must still
  reach back to the clear (asked after the read: journald removes the
  oldest entries first); history rotated or vacuumed away holds, and so
  does a readable journald rate-limit notice for the user's services after
  the last clear. The journal is not ours to keep: every unresolved failure
  an evaluation sees, in a complete read or not, becomes a recorded hold at
  once, so a later vacuum cannot take the evidence away. Such a hold
  resolves itself only while its failure is still in a complete window that
  shows the repairing save; once the failure is gone from the journal, only
  the user's clear removes it.

Both read only the evidence (``gateway_events.is_save_line``: the lines of
patch 8's credential-save emitter) and the running instance's start line,
so how long the gateway ran and how much it logged do not matter; the caps
count kept evidence, and one deadline bounds an evaluation. Anything that
keeps the evaluation from being complete (an unlistable log directory, a log
that cannot be examined or read, an unfinished last line, a read cut by a
cap or its deadline, a credential-save line that does not parse, an
unreadable hold document) holds: missing evidence is never an empty
collection, and the reason says what could not be read.
Logs are safety evidence: the running instance's log is never truncated or
rewritten. At an instance start, :func:`prune_at_start` removes the oldest
previous instances' logs beyond a byte budget, and only after persisting
each one's unresolved failures (with the coverage of that evaluation) as
holds in the hold document (``<state root>/gateway/persistence-hold.json``);
recorded holds veto like live evidence. Only the user clears a hold
(``charmer gateway clear-hold``, a typed confirmation): the clear records
how far each log was evaluated, so later failures hold again, and it removes
only the recorded holds the user was shown; one recorded after that report
(for example by a pruning start meanwhile) stays.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from charmer import errors, gateway_events, service, state, strict_json
from charmer.platform import file_log, observation

HOLD_FILE = "persistence-hold.json"
HOLD_VERSION = 1
# Credential-save bytes kept from one instance log.
FILE_BUDGET = 8 << 20
# Log bytes examined by one on-demand evaluation or prune.
TOTAL_BUDGET = 4 << 30
# Kept evidence (journal JSON) bytes of one journal read.
JOURNAL_BUDGET = 32 << 20
# One evaluation's deadline, a prune's, and the events one evaluation keeps
# (years at the measured rate of about 39 saves a week).
HOLD_TIMEOUT = 30.0
PRUNE_TIMEOUT = 10.0
HOLD_MAX_EVENTS = 16384
# Previous instances' logs kept at a start (newest first); the running and
# the starting instance never count against it and are never pruned.
PRUNE_BUDGET = 32 << 20
COVERAGES = ("bounded", "truncated", "incomplete", "unavailable")
JOURNAL_WINDOW = datetime.timedelta(hours=24)
# How far before the recorded start the journal is read (clock and logging skew).
START_MARGIN = datetime.timedelta(minutes=1)
_HOLD_KEYS = frozenset({"instance", "reason", "recorded_at"})
# Optional: the coverage of a pruned log's evaluation, and the identity of a
# journal failure (what lets a later complete window resolve that hold).
_HOLD_OPTIONAL = frozenset({"coverage", "evidence"})
REMEDY = (
    "keep the gateway running, free disk space and wait for a persisted save of that "
    "credential; sign in again only if none appears; once you have verified the "
    "credentials: charmer gateway clear-hold"
)
# A hold whose every reason is evidence that could not be read (the journal
# no longer reaching back, a rate-limit notice, a cut, timed-out or
# unfinished read, a line that does not parse) and no failed save in what
# was read.
COVERAGE_ONLY = ("held because part of the log could not be evaluated (no failed credential save was "
                 "seen in the part read)")
COVERAGE_REMEDY = (
    "keep the gateway running and check the credentials (charmer providers list); the reason names "
    "what could not be read (journald retention shorter than the gateway's run, a journald rate-limit "
    "notice, a read that timed out, a credential-save line that does not parse); then clear it on the "
    "command line: charmer gateway clear-hold (it records that you checked them, and the logs are "
    "evaluated again from that point on)"
)


class HoldError(errors.CharmerError, RuntimeError):
    """The hold document is unreadable or invalid."""


@dataclass(frozen=True)
class HoldReport:
    held: bool
    reasons: tuple[str, ...]
    source: str  # instance logs | journal
    # What a clear records: per instance nonce, the offset evaluated through.
    cursors: Mapping[str, int] = field(default_factory=dict)
    evaluated_at: str = ""
    # The recorded holds this report showed (a clear removes only these).
    recorded: tuple[Mapping[str, str], ...] = ()
    # Every reason is incomplete coverage: no failed save was seen.
    coverage_only: bool = False
    # Where the evaluation started and how many credential saves it read
    # (shown when nothing holds).
    evaluated: str = ""


def remedy(report: HoldReport) -> str:
    """The fix for ``report``: the clear-hold recovery of a coverage-only
    hold, otherwise :data:`REMEDY`."""

    return COVERAGE_REMEDY if getattr(report, "coverage_only", False) else REMEDY


def describe(report: HoldReport) -> str:
    """``report``'s reasons, led by the coverage-only cause when that is all
    the hold is."""

    reasons = "; ".join(report.reasons) or "the persistence hold"
    return f"{COVERAGE_ONLY}: {reasons}" if getattr(report, "coverage_only", False) else reasons


def credential_save_providers(document: Mapping[str, Any]) -> set[str]:
    """The names the gateway logs credential saves under, from a providers
    document: catalog provider ids plus the OAuth transports' pool names."""

    providers = document["providers"]
    return {*providers, *(entry["transport"]["pool"] for entry in providers.values()
                          if entry.get("transport", {}).get("kind") == "oauth-pool")}


def _coverage_text(coverage: str) -> str:
    return f"the log could not be evaluated completely (coverage {coverage})"


def _recorded_coverage(item: Mapping[str, str]) -> bool:
    """A recorded hold that only says a log could not be read completely."""

    coverage = item.get("coverage", "bounded")
    return coverage != "bounded" and item["reason"] in (
        _coverage_text(coverage), _coverage_text(coverage) + " (log pruned)")


def _only_coverage(kinds: list[bool]) -> bool:
    return bool(kinds) and all(kinds)


def hold_path(state_root: Path) -> Path:
    return service.gateway_workdir(state_root) / HOLD_FILE


def _empty() -> dict[str, Any]:
    return {"version": HOLD_VERSION, "cleared": {}, "cleared_through": None, "holds": []}


def _valid(document: Any) -> bool:
    if not isinstance(document, dict) or set(document) != {"version", "cleared", "cleared_through", "holds"}:
        return False
    if document["version"] != HOLD_VERSION:
        return False
    cleared = document["cleared"]
    if not isinstance(cleared, dict) or not all(
            isinstance(key, str) and file_log.NONCE.fullmatch(key) and type(value) is int and value >= 0
            for key, value in cleared.items()):
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
        isinstance(item, dict) and _HOLD_KEYS <= set(item) <= _HOLD_KEYS | _HOLD_OPTIONAL
        and all(isinstance(item[key], str) and item[key] for key in item)
        and item.get("coverage", "bounded") in COVERAGES
        for item in holds)


def read_document(state_root: Path) -> dict[str, Any]:
    path = hold_path(state_root)
    try:
        raw = state.read_private(path)
    except state.StateError as exc:
        if exc.errno == 2:
            return _empty()
        raise HoldError(f"persistence hold record unreadable: {exc}") from exc
    try:
        document = strict_json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise HoldError(f"persistence hold record is not valid JSON: {exc}") from exc
    if not _valid(document):
        raise HoldError("persistence hold record is invalid")
    return document


def _failure_text(saves: gateway_events.CredentialSaves, cause: str = "") -> str:
    if saves.coverage != "bounded":
        text = _coverage_text(saves.coverage) + (f"; {cause}" if cause else "")
        seen = sorted({event.provider for event in gateway_events.unrepaired_failures(saves)})
        if seen:
            text += f"; a credential save failed in the part read ({', '.join(seen)})"
        return text
    failed = sorted({event.provider for event in saves.events if event.result == "failed"})
    return f"a credential save failed and no later save repaired it ({', '.join(failed) or 'unknown provider'})"


def _coverage_kind(saves: gateway_events.CredentialSaves) -> bool:
    """Whether a holding evaluation is incomplete coverage alone (no
    unrepaired failure in the part that was read)."""

    return saves.coverage != "bounded" and not gateway_events.unrepaired_failures(saves)


def _failure_reason(label: str, saves: gateway_events.CredentialSaves, cause: str = "") -> str:
    return f"{label}: {_failure_text(saves, cause)}"


def _saves_cause(saves: gateway_events.CredentialSaves) -> str:
    if saves.coverage == "truncated":
        return "more credential saves than one evaluation keeps"
    if saves.coverage == "incomplete":
        return "a credential-save line did not parse (an unknown provider name or a damaged line)"
    return ""


def _journal_cause(window: observation.LogWindow, saves: gateway_events.CredentialSaves) -> str:
    """Why a journal evaluation is not complete, in the user's terms."""

    if window.timed_out:
        return f"the journal read did not finish within {HOLD_TIMEOUT:g} s"
    if window.coverage == "unavailable":
        return "the unit's journal could not be read"
    if window.coverage == "truncated":
        return "more credential-save lines than one read keeps"
    if window.coverage == "incomplete":
        return "the journal returned a record that could not be read"
    return _saves_cause(saves)


def _file_cause(evidence: file_log.Evidence, saves: gateway_events.CredentialSaves) -> str:
    if evidence.window.timed_out:
        return f"the log read did not finish within {HOLD_TIMEOUT:g} s"
    return evidence.cause or _saves_cause(saves)


def _count(saves: int) -> str:
    return f"{saves} credential save" + ("" if saves == 1 else "s")


def _recorded_reason(item: Mapping[str, str]) -> str:
    coverage = item.get("coverage")
    suffix = f", coverage {coverage}" if coverage and coverage != "bounded" else ""
    return f"instance {item['instance']}: {item['reason']} (recorded {item['recorded_at']}{suffix})"


def evaluate_files(state_root: Path, providers: Iterable[str], *,
                   now: Callable[[], str] = service.utc_now,
                   file_budget: int | None = None, total_budget: int | None = None,
                   deadline: float | None = None) -> HoldReport:
    """The on-demand hold over every retained instance log: each log's
    credential-save lines from the position last cleared, at most
    ``total_budget`` bytes examined in all and ``file_budget`` kept per log,
    within ``deadline`` (``time.monotonic``; default :data:`HOLD_TIMEOUT`
    from now)."""

    file_budget = FILE_BUDGET if file_budget is None else file_budget
    total_budget = TOTAL_BUDGET if total_budget is None else total_budget
    deadline = time.monotonic() + HOLD_TIMEOUT if deadline is None else deadline
    allowed = frozenset(providers)
    at = now()
    reasons: list[str] = []
    cursors: dict[str, int] = {}
    try:
        document = read_document(state_root)
    except HoldError as exc:
        return HoldReport(True, (str(exc),), "instance logs", {}, at)
    total = 0
    recorded = tuple(dict(item) for item in document["holds"])
    # Per reason: True when it is incomplete coverage alone.
    kinds: list[bool] = []
    for item in recorded:
        reasons.append(_recorded_reason(item))
        kinds.append(_recorded_coverage(item))
    logs_dir = service.gateway_workdir(state_root) / service.GATEWAY_LOGS
    logs, problems = file_log.scan_instance_logs(logs_dir)
    reasons.extend(f"{problem}; the persistence hold cannot be evaluated" for problem in problems)
    kinds.extend(False for _problem in problems)
    remaining = total_budget
    for log in logs:
        label = f"instance {log.nonce}"
        start = document["cleared"].get(log.nonce, 0)
        if remaining <= 0:
            if log.size <= start:
                continue  # nothing past the cleared position
            reasons.append(f"{label}: not evaluated (the logs exceed the read budget)")
            kinds.append(True)
            # A clear covers what this report showed: the log as it is now.
            cursors[log.nonce] = max(start, log.size)
            continue
        evidence = file_log.read_evidence(log, start=start, max_bytes=file_budget,
                                          keep=gateway_events.SAVE_LINE_BYTES, scan_limit=remaining,
                                          deadline=deadline)
        remaining -= max(0, evidence.through - start)
        cursors[log.nonce] = evidence.through
        saves = gateway_events.collect_credential_saves(evidence.window, providers=allowed,
                                                        max_events=HOLD_MAX_EVENTS)
        total += len(saves.events)
        if gateway_events.recovery_hold(saves):
            reasons.append(_failure_reason(label, saves, _file_cause(evidence, saves)))
            kinds.append(_coverage_kind(saves))
    return HoldReport(bool(reasons), tuple(reasons), "instance logs", cursors, at, recorded,
                      coverage_only=_only_coverage(kinds),
                      evaluated=f"{len(logs)} instance log(s): {_count(total)}")


Drops = tuple[tuple[datetime.datetime, int | None], ...]


def journal_sources(unit: str, marker: str | None) -> tuple[
        Callable[[datetime.datetime], observation.LogWindow],
        Callable[[datetime.datetime], bool | None],
        Callable[[datetime.datetime], Drops | None]]:
    """The unit journal's ``(reader, reaches, drops)`` for
    :func:`evaluate_journal`: the evidence read (credential-save lines and
    the start line ``marker``), the reach-back probe and journald's drop
    notices for the user's services."""

    def reader(since: datetime.datetime) -> observation.LogWindow:
        return service.read_hold_journal(unit=unit, since=f"@{int(since.timestamp())}",
                                         evidence=gateway_events.SAVE_LINE_PATTERN, marker=marker,
                                         max_bytes=JOURNAL_BUDGET)

    def reaches(at: datetime.datetime) -> bool | None:
        return service.journal_reaches(unit=unit, at=at)

    def drops(since: datetime.datetime) -> Drops | None:
        return service.journal_drops(since=since)

    return reader, reaches, drops


def _utc(moment: datetime.datetime) -> datetime.datetime:
    return moment.astimezone(datetime.timezone.utc)


def evaluate_journal(state_root: Path, providers: Iterable[str], *,
                     reader: Callable[[datetime.datetime], observation.LogWindow],
                     now: Callable[[], str] = service.utc_now,
                     running: tuple[str | None, datetime.datetime] | None = None,
                     marker: Callable[[str], str] = service.start_marker_prefix,
                     record: bool = True,
                     reaches: Callable[[datetime.datetime], bool | None] | None = None,
                     drops: Callable[[datetime.datetime], Drops | None] | None = None) -> HoldReport:
    """The systemd backend's hold over the unit journal.

    ``reader(since)`` reads the evidence from ``since``: the earlier of 24
    hours ago and the start of the ``running`` instance (``(nonce,
    started)``). While an instance with a nonce runs, the last clear only
    filters which saves count; otherwise the read starts at the clear. The
    running instance's start line (``marker(nonce)``: the line the gateway's
    launcher wrote) must be in the read; when it is not and the last clear
    is at or after that start, ``reaches(clear)`` (asked after the read)
    must say the journal still reaches back to the clear. Otherwise the
    journal does not cover the instance (rotated or vacuumed) and that
    holds. ``drops(since)`` gives journald's rate-limit notices for the
    user's services (None: not readable here, which is said, not held); one
    after the last clear holds. A missing ``reaches`` or ``drops`` answers
    None. ``record`` False evaluates without writing the hold document (an
    unresolved failure then holds as seen, unrecorded).
    """

    allowed = frozenset(providers)
    at = now()
    try:
        document = read_document(state_root)
    except HoldError as exc:
        return HoldReport(True, (str(exc),), "journal", {}, at)
    recorded = tuple(dict(item) for item in document["holds"])
    since = datetime.datetime.fromisoformat(at) - JOURNAL_WINDOW
    if running is not None:
        since = min(since, running[1] - START_MARGIN)
    through = document["cleared_through"]
    cutoff = datetime.datetime.fromisoformat(through) if through is not None else None
    stamped = running is not None and bool(running[0])
    if cutoff is not None and cutoff > since and not stamped:
        since = cutoff
    window = reader(since)  # first: the probes below must follow the read
    coverage: list[str] = []
    if running is not None and running[0] and window.coverage == "bounded":
        start = marker(running[0])
        if not any(line.message.startswith(start) for line in window.records):
            if cutoff is None or cutoff < running[1]:
                coverage.append(f"service journal: the start of the running instance {running[0]} is not in the "
                                "journal (rotated or vacuumed); its credential saves cannot be evaluated completely")
            else:
                reached = reaches(cutoff) if reaches is not None else None
                clear = _utc(cutoff).isoformat(timespec="seconds")
                if reached is False:
                    coverage.append(f"service journal: the journal no longer reaches back to the last clear-hold "
                                    f"({clear}); it was rotated or vacuumed since, so the credential saves after it "
                                    "cannot be evaluated completely")
                elif reached is not True:
                    coverage.append(f"service journal: whether the journal still reaches back to the last clear-hold "
                                    f"({clear}) could not be read, so the credential saves after it cannot be "
                                    "evaluated completely")
    dropped: Drops | None = None
    drops_checked = window.coverage == "bounded"
    if drops_checked:
        dropped = drops(since) if drops is not None else None
        notices = [(moment, count) for moment, count in dropped or ()
                   if cutoff is None or moment > cutoff]
        if notices:
            counts = [count for _moment, count in notices]
            total = ("some" if any(count is None for count in counts)
                     else str(sum(count for count in counts if count is not None)))
            last = _utc(max(moment for moment, _count in notices)).isoformat(timespec="seconds")
            coverage.append(f"service journal: journald's rate limit for your user services, which the gateway "
                            f"shares, dropped {total} log lines (last notice {last}); a credential save among them "
                            "cannot be ruled out")
    if cutoff is not None:
        window = observation.LogWindow(
            tuple(record for record in window.records
                  if record.timestamp is None or record.timestamp > cutoff),
            window.coverage, window.timed_out)
    saves = gateway_events.collect_credential_saves(window, providers=allowed, max_events=HOLD_MAX_EVENTS)
    shown = max(since, cutoff) if cutoff is not None else since
    evaluated = f"from {_utc(shown):%Y-%m-%d %H:%M} UTC: {_count(len(saves.events))}"
    if drops_checked and dropped is None:
        evaluated += "; journald's drop notices are not readable for this user"
    if saves.coverage != "bounded":
        # A failure seen in an incomplete read is recorded at once (a later
        # vacuum cannot take it away); an incomplete read resolves nothing.
        unrepaired = gateway_events.unrepaired_failures(saves)
        problems: list[str] = []
        if record and unrepaired:
            try:
                recorded = _record_journal_evidence(state_root, [_journal_hold(event, at) for event in unrepaired],
                                                    set())
            except (OSError, errors.CharmerError) as exc:
                problems.append(f"service journal: the failure evidence could not be recorded ({exc})")
        reasons = [*map(_recorded_reason, recorded), *coverage, *problems,
                   _failure_reason("service journal", saves, _journal_cause(window, saves))]
        kinds = [*map(_recorded_coverage, recorded), *(True for _line in coverage), *(False for _p in problems),
                 _coverage_kind(saves)]
        return HoldReport(True, tuple(reasons), "journal", {}, at, recorded,
                          coverage_only=_only_coverage(kinds), evaluated=evaluated)
    # Unresolved failures become recorded holds before the journal can lose
    # them; a recorded failure still in this complete window that a later
    # save repaired is resolved.
    unrepaired = gateway_events.unrepaired_failures(saves)
    open_keys = {_evidence(event) for event in unrepaired}
    resolved = {key for key in (_evidence(event) for event in saves.events if event.result == "failed")
                if key is not None and key not in open_keys}
    observed = [_journal_hold(event, at) for event in unrepaired]
    if observed and not record:
        reasons = [*map(_recorded_reason, recorded), *coverage, _failure_reason("service journal", saves)]
        return HoldReport(True, tuple(reasons), "journal", {}, at, recorded, evaluated=evaluated)
    if record and (observed or any(item.get("evidence") in resolved for item in recorded)):
        try:
            recorded = _record_journal_evidence(state_root, observed, resolved)
        except (OSError, errors.CharmerError) as exc:
            reasons = [*map(_recorded_reason, recorded), *coverage,
                       f"service journal: the failure evidence could not be recorded ({exc})"]
            if observed:
                reasons.append(_failure_reason("service journal", saves))
            return HoldReport(True, tuple(reasons), "journal", {}, at, recorded, evaluated=evaluated)
    reasons = [*map(_recorded_reason, recorded), *coverage]
    return HoldReport(bool(reasons), tuple(reasons), "journal", {}, at, recorded,
                      coverage_only=_only_coverage([*map(_recorded_coverage, recorded), *(True for _line in coverage)]),
                      evaluated=evaluated)


def _evidence(event: gateway_events.CredentialSave) -> str | None:
    """A journal failure's identity, or None when the record lacks one."""

    if event.gateway_instance is None or event.timestamp is None or event.auth_index == "invalid":
        return None
    return (f"journal {event.gateway_instance} {event.provider} {event.auth_index} "
            f"{event.timestamp.astimezone(datetime.timezone.utc).isoformat()}")


def _journal_hold(event: gateway_events.CredentialSave, at: str) -> dict[str, str]:
    item = {"instance": event.gateway_instance or "unknown",
            "reason": f"a credential save failed and no later save repaired it ({event.provider}; service journal)",
            "recorded_at": at}
    key = _evidence(event)
    if key is not None:
        item["evidence"] = key
    return item


def _record_journal_evidence(state_root: Path, observed: list[dict[str, str]],
                             resolved: set[str]) -> tuple[Mapping[str, str], ...]:
    """Persist the unresolved journal failures not recorded yet and drop the
    recorded journal holds a complete window shows repaired; returns the
    recorded holds now in effect."""

    path = hold_path(state_root)
    state.ensure_private_dir(path.parent)
    with state.FileLock(path):
        document = read_document(state_root)
        holds = [dict(item) for item in document["holds"] if item.get("evidence") not in resolved]
        for item in observed:
            if not any(_same_hold(item, held) for held in holds):
                holds.append(item)
        if holds != [dict(item) for item in document["holds"]]:
            state.atomic_write(path, strict_json.pretty_file_bytes({**document, "holds": holds}))
    return tuple(holds)


def _same_hold(item: Mapping[str, str], held: Mapping[str, str]) -> bool:
    if "evidence" in item:
        return held.get("evidence") == item["evidence"]
    return "evidence" not in held and held["instance"] == item["instance"] and held["reason"] == item["reason"]


def clear(state_root: Path, report: HoldReport) -> tuple[Mapping[str, str], ...]:
    """Record the user's clear of what ``report`` showed: each evaluated log's
    position, the journal instant, and the removal of the recorded holds it
    listed. Later failures hold again.

    Returns the recorded holds kept because they were recorded after the
    report (evidence the user has not seen); the hold then still applies.
    """

    path = hold_path(state_root)
    state.ensure_private_dir(path.parent)
    with state.FileLock(path):
        try:
            document = read_document(state_root)
        except HoldError:
            document = _empty()  # the user is replacing an unreadable record deliberately
        shown = [dict(item) for item in report.recorded]
        kept = [item for item in document["holds"] if dict(item) not in shown]
        cleared = dict(document["cleared"])
        cleared.update({nonce: offset for nonce, offset in report.cursors.items()
                        if not any(item["instance"] == nonce for item in kept)})
        document = {
            "version": HOLD_VERSION, "cleared": cleared,
            "cleared_through": report.evaluated_at if report.source == "journal" else document["cleared_through"],
            "holds": kept,
        }
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
    return tuple(kept)


@dataclass(frozen=True)
class PruneReport:
    removed: tuple[str, ...] = ()  # instance nonces whose logs were deleted
    persisted: tuple[str, ...] = ()  # instances whose unresolved evidence became holds
    skipped: str | None = None  # why nothing was pruned
    retained_bytes: int = 0


def prune_at_start(state_root: Path, providers: Iterable[str], *, keep: Iterable[str] = (),
                   budget: int | None = None, file_budget: int | None = None,
                   now: Callable[[], str] = service.utc_now, deadline: float | None = None) -> PruneReport:
    """At an instance start: drop the oldest previous logs beyond ``budget``.

    ``keep`` names instances that must stay (a running one). Each log to be
    removed is evaluated from its cleared position first (its credential-save
    lines, :data:`TOTAL_BUDGET` bytes examined in all); an unresolved
    failure, or an evaluation that is not complete, becomes a recorded hold
    with that coverage. A log whose read does not finish by ``deadline``
    (``time.monotonic``; default :data:`PRUNE_TIMEOUT` from the start of the
    reads), and every later one, is neither recorded nor removed: it stays
    for a later start. The hold document is written (atomically) before any
    file is removed, and an unreadable hold document prunes nothing.
    """

    budget = PRUNE_BUDGET if budget is None else budget
    file_budget = FILE_BUDGET if file_budget is None else file_budget
    kept = frozenset(keep)
    logs_dir = service.gateway_workdir(state_root) / service.GATEWAY_LOGS
    logs, problems = file_log.scan_instance_logs(logs_dir)
    if problems:
        # The budget cannot be computed over logs that cannot be examined.
        return PruneReport(skipped="; ".join(problems), retained_bytes=file_log.total_size(logs))
    previous = [log for log in logs if log.nonce not in kept]
    total, victims = 0, []
    for log in previous:  # newest first
        total += log.size
        if total > budget:
            victims.append(log)
    retained = sum(log.size for log in previous) - sum(log.size for log in victims)
    if not victims:
        return PruneReport(retained_bytes=retained)
    allowed = frozenset(providers)
    path = hold_path(state_root)
    with state.FileLock(path):
        try:
            document = read_document(state_root)
        except HoldError as exc:
            return PruneReport(skipped=str(exc), retained_bytes=retained + sum(log.size for log in victims))
        at = now()
        holds = list(document["holds"])
        cleared = dict(document["cleared"])
        persisted = []
        deadline = time.monotonic() + PRUNE_TIMEOUT if deadline is None else deadline
        remaining = TOTAL_BUDGET
        for index, log in enumerate(victims):
            start = cleared.get(log.nonce, 0)
            evidence = file_log.read_evidence(log, start=start, max_bytes=file_budget,
                                              keep=gateway_events.SAVE_LINE_BYTES, scan_limit=remaining,
                                              deadline=deadline)
            if evidence.window.timed_out:
                # Kept for a later start: neither recorded nor removed.
                retained += sum(late.size for late in victims[index:])
                victims = victims[:index]
                break
            remaining -= max(0, evidence.through - start)
            saves = gateway_events.collect_credential_saves(evidence.window, providers=allowed,
                                                            max_events=HOLD_MAX_EVENTS)
            if gateway_events.recovery_hold(saves):
                item = {"instance": log.nonce, "reason": _failure_text(saves) + " (log pruned)",
                        "recorded_at": at, "coverage": saves.coverage}
                if not any(held["instance"] == log.nonce and held["reason"] == item["reason"] for held in holds):
                    holds.append(item)
                persisted.append(log.nonce)
        if not victims:
            return PruneReport(retained_bytes=retained)
        # The holds are durable before any file goes; a crash in between
        # leaves the file and its cleared position, never a lost failure.
        try:
            state.atomic_write(path, strict_json.pretty_file_bytes(
                {**document, "cleared": cleared, "holds": holds}))
        except (OSError, errors.CharmerError) as exc:
            return PruneReport(skipped=f"the hold record could not be written: {exc}",
                               retained_bytes=retained + sum(log.size for log in victims))
        removed = []
        for log in victims:
            try:
                file_log.remove_instance_log(log)
            except (OSError, ValueError):
                retained += log.size
                continue
            removed.append(log.nonce)
            cleared.pop(log.nonce, None)
        if removed:
            try:  # best effort: a stale cleared position names no file
                state.atomic_write(path, strict_json.pretty_file_bytes(
                    {**document, "cleared": cleared, "holds": holds}))
            except (OSError, errors.CharmerError):
                pass
    return PruneReport(tuple(removed), tuple(persisted), None, retained)
