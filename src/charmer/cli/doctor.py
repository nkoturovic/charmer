"""Doctor reports: every read-only doctor check and radar.

Moved verbatim from the CLI module; report order, status and remedies are
unchanged. Gateway and journal observations come from ``gateway_facts``.
"""

from __future__ import annotations

from charmer import catalog
from charmer import compiler
from charmer import continuity as continuity_mod
from charmer import custom
from charmer import endpoint
from charmer import errors as cli_errors
from charmer import hooks
from charmer import legacy
from charmer import gateway_events
from charmer import launch
from charmer import managed
from charmer import management
from charmer import operator as operator_mod
from charmer import observations, lineup_log
from charmer import paths
from charmer import paths as product_paths
from charmer import portability
from charmer import profile as profile_mod
from charmer import proxy as proxy_mod
from charmer import quota
from charmer import readiness as readiness_mod
from charmer import render
from charmer import retention
from charmer import scope as scope_mod
from charmer import served_plan
from charmer import secret_store
from charmer import service
from charmer import sessions
from charmer import settings as settings_mod
from charmer import state
from charmer import strict_json
from charmer import termtext
from charmer import tls
from charmer import views
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
import charmer.cli.gateway_facts as gateway_facts
import charmer.cli.runtime as runtime_mod
import charmer.cli.session_facts as session_facts
import charmer.cli.text as cli_text
import json
import os
import re
import stat
from typing import TYPE_CHECKING
from typing import Mapping

if TYPE_CHECKING:
    from charmer.transition import ExpectedPlan
    from typing import Any
    from typing import Callable


# ------------------------------------------------------------------ presentation
#
# Each collector tags its lines (``gateway_facts.Line``) where it makes them:
# the area doctor lists them under, an action code for the findings the TUI
# findings list acts on, ``advisory`` for optional evidence, and the
# identity repeated lines share. Plain doctor and H present the same rows
# (:func:`present_report`); nothing here reads a line's text to decide anything.

# The areas, in report order (a held gateway leads Gateway).
AREAS = ("Gateway", "Installation", "Providers", "Sessions", "Profiles", "Models")
# The area names as the collectors pass them to ``gateway_facts.in_area``.
GATEWAY, INSTALLATION, PROVIDERS, SESSIONS, PROFILES, MODELS = AREAS
# A line no collector tagged (an injected check) is listed last.
OTHER_AREA = "Other"
# The findings list's fixed actions (``cli.screens.findings``); every other
# row shows its exact command.
ACTIONS = frozenset({
    "hold", "hold-coverage", "missing-key", "earlier-scopes", "scope-repair", "unused-alias", "prune",
    "dead-sessions", "seed-update", "advisory-model", "needs-choice",
})
ADVISORY = "advisory"
# Optional evidence: it never changes the verdict (Ready can stand beside
# it). Admission and qualification attest a definition; the rest are
# recommendations an explicit binding overrides. A context overflow risk
# and an ignored lead-only environment stay Attention (they change what a
# session can do).
ADVISORY_CODES = frozenset({
    "admission", "qualification", "exact-client", "tool-evidence", "capability-recommendation",
    "role-recommendation", "effort-unverified", "family-unknown", "independence-unknown", "companion-grade",
    "explore-replacement-unbound",
})
# The model finding codes the optional diagnostics (M → Q) answer.
_QUALIFY_CODES = {"qualification": "--agents", "exact-client": "--agents", "tool-evidence": "--tools"}
ADVISORIES_TITLE = "Advisories (optional evidence; does not change the verdict)"
# How many session ids a collapsed row names before "(doctor -v lists them)".
SHOWN_IDS = 3
VERBOSE_HINT = "(doctor -v lists them)"
_LEVEL_ORDER = {"block": 0, "attention": 1, ADVISORY: 2}
# Repairable scopes the earlier name compiled share one row, with one
# original finding per session. Deferred scopes are grouped by their remedy.
EARLIER_SCOPES = ("session", "earlier-scope")
_EARLIER_SCOPES_SUMMARY = (f"{{n}} session scope(s) were compiled by {legacy.NAME} (their hooks name its shims, "
                           f"which the move to charmer replaced): {cli_text.EARLIER_SCOPES_FIX}")


def _area_index(area: str | None) -> int:
    return AREAS.index(area) if area in AREAS else len(AREAS)


@dataclass(frozen=True)
class Row:
    """One finding as plain doctor and H show it: its level (``block``,
    ``attention`` or ``advisory``), its area, the text, every original line
    it stands for (``-v`` prints them), its action code and target, and the
    exact command a typed finding names."""

    level: str
    area: str
    text: str
    originals: tuple[str, ...]
    action: str | None = None
    subject: str | None = None
    remedy: str | None = None

    @property
    def collapsed(self) -> bool:
        return self.originals != (self.text,)


@dataclass(frozen=True)
class Presentation:
    """The rows in report order: BLOCKED, then Attention by area (a held
    gateway first), then the advisories."""

    rows: tuple[Row, ...]

    def of(self, level: str) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.level == level)

    @property
    def status(self) -> str:
        return observations.status_of(row.level for row in self.rows)

    def areas(self, level: str) -> list[tuple[str, tuple[Row, ...]]]:
        """``(area, rows)`` of one level, in area order."""

        found: dict[str, list[Row]] = {}
        for row in self.of(level):
            found.setdefault(row.area, []).append(row)
        return [(area, tuple(rows)) for area, rows in found.items()]


def _listed(subjects: tuple[str, ...]) -> str:
    shown = ", ".join(subjects[:SHOWN_IDS])
    return shown + (f"… {VERBOSE_HINT}" if len(subjects) > SHOWN_IDS else "")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def advisory_command(code: str, key: str | None) -> str | None:
    """The command of an advisory model finding's optional next step, or
    None when it has none (a recommendation, a lineup-wide finding)."""

    if not key:
        return None
    if code == "admission":
        return f"charmer models admit {key}"
    if code in _QUALIFY_CODES:
        return f"charmer models qualify {key} {_QUALIFY_CODES[code]}"
    return None


def advisory_remedy(code: str, key: str | None) -> str:
    """The optional next step of an advisory model finding."""

    command = advisory_command(code, key)
    if not key:
        return "optional; nothing to do"
    if code == "admission":
        return f"optional: {command}, or M → Enter (sends no request)"
    if command is not None:
        return f"optional diagnostics: {command}, or M → Q ({cli_text.OPTIONAL_DIAGNOSTICS})"
    return "a recommendation; the explicit binding stays allowed"


def _shared_wording(messages: list[str]) -> str:
    """One wording for the variants of one model finding (one per slot or
    effort, say): the words they all begin and end with, the differing
    middle as "…"; the first variant when they share too little."""

    words = [message.split(" ") for message in messages]
    shortest = min(len(split) for split in words)
    start = 0
    while start < shortest and all(split[start] == words[0][start] for split in words):
        start += 1
    end = 0
    while end < shortest - start and all(split[-1 - end] == words[0][-1 - end] for split in words):
        end += 1
    if start + end < 3:
        return f"{messages[0]}; and {_plural(len(messages) - 1, 'other wording')}"
    head, tail = " ".join(words[0][:start]), " ".join(words[0][len(words[0]) - end:]) if end else ""
    return " ".join(part for part in (head, "…", tail) if part)


def _model_text(level: str, members: list[str]) -> str:
    """One row per (model, code): the finding (its variants' shared wording
    when they differ, by slot or effort) with how many profiles and slots
    bind the model and, for an advisory, its optional next step."""

    first = members[0]
    key = first.subject if first.group[2] else None
    messages = list(dict.fromkeys(getattr(member, "short", None) or str(member) for member in members))
    text = messages[0] if len(messages) == 1 else _shared_wording(messages)
    if key and not text.startswith(f"{key}:"):
        text = f"{key}: {text}"
    # Distinct bindings: a profile slot that words the finding twice counts once.
    bindings = {(member.owner, getattr(member, "slot", None)) for member in members
                if getattr(member, "owner", None)}
    counts = []
    if bindings:
        counts.append(_plural(len({owner for owner, _slot in bindings}), "profile"))
    slots = sum(1 for _owner, slot in bindings if slot)
    if slots:
        counts.append(_plural(slots, "slot"))
    notes = [", ".join(counts)] if counts else []
    if len(messages) > 1:
        notes.append(VERBOSE_HINT.strip("()"))
    if notes:
        text += f" ({'; '.join(notes)})"
    if level == ADVISORY:
        text += " — " + advisory_remedy(first.group[1], key)
    return text


def present_report(problems: list[str], attention: list[str]) -> Presentation:
    """The rows plain doctor and H show, from the collected lines.

    Lines collapse only when they share a known identity: the group a
    collector gave them (one row per (model, code) with its profile and
    slot counts; per-session kinds with a count and the first ids), or the
    exact same text. A line's ``short`` form stands for it; ``-v`` prints
    every original. Order: BLOCKED, Attention, advisories; by area inside
    each (the persistence hold first in Gateway), then as collected."""

    order: list[tuple[str, str, tuple]] = []
    members: dict[tuple[str, str, tuple], list[str]] = {}
    for level, line in [*(("block", line) for line in problems),
                        *((getattr(line, "level", None) or "attention", line) for line in attention)]:
        level = level if level in _LEVEL_ORDER else "attention"
        area = getattr(line, "area", None) or OTHER_AREA
        group = getattr(line, "group", None)
        key = (level, area, ("group", *group) if group else ("text", str(line)))
        if key not in members:
            members[key] = []
            order.append(key)
        members[key].append(line)
    rows = []
    for index, key in enumerate(order):
        level, area, _identity = key
        lines = members[key]
        first = lines[0]
        group = getattr(first, "group", None)
        subjects = tuple(dict.fromkeys(subject for line in lines for subject in getattr(line, "subjects", ())))
        if group and group[0] == "model":
            text = _model_text(level, lines)
        elif len(lines) > 1 and getattr(first, "summary", None):
            # A literal placeholder: the summary may name a path or a value.
            text = first.summary.replace("{n}", str(len(subjects) or len(lines))) + " — " + _listed(subjects)
        else:
            text = getattr(first, "short", None) or str(first)
        hold = group == ("gateway", "persistence-hold") or getattr(first, "action", None) in ("hold", "hold-coverage")
        # The command any member names (a group's lines may come from collectors that name it once),
        # else the command of an advisory model row's optional next step.
        remedy = next((line.remedy for line in lines if getattr(line, "remedy", None)), None)
        if group == ("session", "earlier-scope-deferred") and len(subjects) > 1:
            remedy = "charmer -r <id>"
        if remedy is None and level == ADVISORY and group and group[0] == "model" and group[2]:
            remedy = advisory_command(group[1], getattr(first, "subject", None))
        rows.append((_LEVEL_ORDER[level], _area_index(area), 0 if hold else 1, index,
                     Row(level, area, text, tuple(map(str, lines)), getattr(first, "action", None),
                         getattr(first, "subject", None), remedy)))
    return Presentation(tuple(row for *_order, row in sorted(rows, key=lambda item: item[:4])))


def report_lines(problems: list[str], info_lines: list[str], attention: list[str], *, verbose: bool,
                 say: Callable[[str], str] = str) -> list[tuple[str, str]]:
    """``(role, line)`` of the report as plain doctor prints it (and H's
    ``v``): BLOCKED, Attention by area, the advisories, Ready, the detail
    lines (``-v`` only), the hidden-detail hint and the closing line.
    ``say`` words each finding for its surface (the card's key route)."""

    presentation = present_report(problems, attention)
    status = presentation.status
    out: list[tuple[str, str]] = []

    def items(rows: tuple[Row, ...], indent: str, role: str) -> None:
        for row in rows:
            for text in (row.originals if verbose else (row.text,)):
                out.append((role, f"{indent}- {termtext.visible_text(say(text))}"))

    if presentation.of("block"):
        out.append(("error", "BLOCKED"))
        items(presentation.of("block"), "  ", "error")
    if presentation.of("attention"):
        out.append(("warn", "Attention"))
        for area, rows in presentation.areas("attention"):
            count = sum(len(row.originals) for row in rows) if verbose else len(rows)
            out.append(("warn", f"  {area} ({count})"))
            items(rows, "    ", "warn")
    if presentation.of(ADVISORY):
        out.append(("dim", ADVISORIES_TITLE))
        items(presentation.of(ADVISORY), "  ", "dim")
    if status == "ready":
        out.append(("ok", "Ready"))
    shown = info_lines if verbose else [line for line in info_lines if not detail_line(line)]
    out.extend(("normal", termtext.visible_text(say(line))) for line in shown)
    hidden = len(info_lines) - len(shown)
    if hidden:
        out.append(("normal", cli_text.CARD_DOCTOR_HIDDEN.format(n=hidden, s="s" if hidden != 1 else "")))
    out.append(("normal", DOCTOR_SUMMARY[status].rstrip("\n")))
    return out


def _tag_model(line: str, finding: Any, *, key: str | None, owner: str | None = None,
               slot: str | None = None) -> gateway_facts.Line:
    """A model finding (``profile.Finding``) of one profile slot, or of a
    declared line: listed under Models, advisory for optional evidence, one
    row per (model, code) whatever each slot's wording. A lineup-wide
    finding (no model: ``key`` None) is one row per (code, wording)."""

    code = finding.code
    advisory = code in ADVISORY_CODES
    identity = ("model", code, key) if key else ("model", code, "", finding.message)
    return gateway_facts.tagged(
        line, area="Models", level=ADVISORY if advisory else None, group=identity,
        short=finding.message, subject=key, owner=owner, slot=slot,
        action="advisory-model" if advisory and code in _QUALIFY_CODES and key is not None else None)


def _per_session(line: str, mid: str, kind: str, summary: str, *, action: str | None = None,
                 area: str = "Sessions") -> gateway_facts.Line:
    """A per-session line of one kind: collapsed with the others of its kind
    into ``summary`` (``{n}`` sessions) and their first ids."""

    return gateway_facts.tagged(line, area=area, group=("session", kind), summary=summary, subjects=(mid[:8],),
                                action=action)


def _doctor_served_report(runtime: runtime_mod.Runtime, token: str) -> tuple[list[str], list[str]]:
    """``(problems, info)`` form of :func:`_doctor_served_checks`.

    The injectable ``doctor_served_callback`` contract; Attention lines
    (continuity) are appended to ``info`` here. Doctor itself
    calls ``_doctor_served_checks`` and files them under Attention.
    """

    problems, info, attention = _doctor_served_checks(runtime, token)
    return problems, [*info, *attention]


def pending_served_plan(runtime: runtime_mod.Runtime) -> served_plan.ServedChangePlan | None:
    """The declared render against the published one:
    pure and read-only (no render, lock or write; presence-only secrets).
    None when nothing is published yet or the plan cannot be built."""

    published = proxy_mod.published_identity(runtime.home)
    if published.routes is None:
        return None
    try:
        document = proxy_mod.candidate_document(
            runtime.home, environ=runtime.gateway_environ(), asset_root=runtime.asset_root,
            state_root=runtime.session_store.root,
        )
        scan = continuity_mod.scan_records(runtime.session_store.root)
    except (cli_errors.CharmerError, OSError, ValueError, KeyError, TypeError):
        return None
    return served_plan.build_plan(
        published.routes, served_plan.routes_from_document(document),
        references=served_plan.references_from_scan(scan), state_root=str(runtime.session_store.root),
        gateway=published.gateway,
    )


def _pending_served_change(runtime: runtime_mod.Runtime) -> str | None:
    """The read-only doctor Attention fact "Pending served change: …"."""

    plan = pending_served_plan(runtime)
    return None if plan is None else plan.doctor_fact()


def _doctor_served_checks(
    runtime: runtime_mod.Runtime, token: str,
) -> tuple[list[str], list[str], list[str]]:
    """Served-vs-rendered selector cross-check. Loopback only.

    The running gateway's /v1/models is its in-process registry — with
    --local-model it mirrors the config it last loaded (the patched 7.3.15
    watcher hot-reloads a rename-replaced config.yaml). Diagnoses, in
    order: on-disk config drift (init not run), a stale render sentinel
    (the gateway did not reload the current render — restart required),
    unserved rendered selectors (or, for OAuth pools without a credential
    record, login missing), and stale charmer-shaped served selectors
    (removed aliases). Embedded-registry extras (unrelated Anthropic/Codex
    models) and render sentinels are ignored by design. Wire-name
    remappings are invisible to /v1/models — the config-drift byte check
    covers those.

    Continuity: a corrupt ``continuity.json`` is one BLOCK
    (the render and this check both fall back to the seed set, so it never
    shows as drift); a set extended from another state root, and a
    continuity alias the gateway does not serve, are Attention with the
    exact command. A missing file is lazy state and says nothing.
    """

    snap = gateway_facts._gateway_snapshot(runtime, token)
    problems: list[str] = []
    info: list[str] = []
    attention: list[str] = []
    session_root = str(runtime.session_store.root)
    if snap.continuity_status.startswith("corrupt"):
        reason = snap.continuity_status.removeprefix("corrupt: ")
        problems.append(
            f"gateway continuity set unreadable ({reason}): move "
            "~/.config/charmer/continuity.json aside and run "
            "charmer-proxy init (it is re-seeded from retired.json and "
            "session records)"
        )
    elif (
        snap.continuity_status == "persisted"
        and snap.continuity_state_root is not None
        and snap.continuity_state_root != session_root
    ):
        # The persisted root is the gateway's
        # authority; served changes and new sessions from this root refuse.
        problems.append(served_plan.ROOT_BLOCK.format(
            managed=snap.continuity_state_root, requested=session_root))
    elif snap.continuity_status == "persisted" and snap.continuity_state_root is None:
        attention.append(
            "continuity was extended from no session state root, but "
            f"sessions live under {session_root}; run charmer-proxy init "
            f"--state-root {session_root}"
        )
    pending = _pending_served_change(runtime)
    if pending is not None:
        attention.append(pending)
    if snap.gateway_down:
        # Readiness passed moments ago but the models probe failed — a
        # restart is likely in flight. Say the radar never ran (silence
        # would read as a clean bill).
        return problems, [
            "the gateway changed state after the readiness check (a restart "
            "in flight?); the served-selector cross-check was skipped — "
            "rerun doctor when it settles"
        ], attention
    if snap.key_problem is not None:
        # The gateway's own render (cmd_run at every start) reads the same
        # slots: an unusable slot means the gateway cannot restart.
        problems.append(
            f"gateway key slots unusable: {snap.key_problem} — the gateway "
            "cannot re-render its config on restart; inspect "
            "~/.config/charmer (names/modes only; an interrupted "
            "rotation's malformed previous-key can be removed), then rerun "
            "`charmer doctor --rotate-token`"
        )
    if snap.config_note is not None:
        info.append(snap.config_note)
    if snap.rotation_phase is not None:
        # A paused rotation (Attention via _token_rotation_attention) is
        # not drift; running init here could undo the retirement phase.
        info.append(
            f"{snap.rotation_phase} — finish with `charmer doctor "
            "--rotate-token` (not `charmer-proxy init`)"
        )
    if snap.render_error is not None:
        info.append(f"served-selector cross-check skipped: {snap.render_error}")
        return problems, info, attention
    if snap.config_drift:
        problems.append(
            "the on-disk gateway config differs from a fresh render of the "
            "installed catalog (a catalog edit or secret rotation is not "
            f"applied): run {cli_text.APPLY_GATEWAY_COMMAND}"
        )
        # Selector classification stops here (not independently actionable
        # under drift) — but a missing OAuth credential record is its own
        # action, so it still surfaces.
        for pool, count in sorted(snap.oauth_records.items()):
            if count == 0:
                info.append(
                    f"the {pool} OAuth pool has no credential record: sign in "
                    f"with `{gateway_facts.sign_in_command(pool)}`"
                )
        return problems, info, attention
    if snap.served is None:
        if snap.models_status == 401:
            # The running daemon holds a different token than the rendered
            # config (rotated token + init without restart): every session
            # would 401 while healthz stays green. This is a problem, not
            # an advisory skip.
            problems.append(
                "the running gateway rejected the local token (401 on "
                "/v1/models): token mismatch — the daemon serves an older "
                "config; run `charmer-proxy init` (verifies the reload), "
                f"or restart with {service.hint_code('restart')}"
            )
            return problems, info, attention
        info.append(
            "local gateway: /v1/models returned a non-200 status; the "
            "served-selector cross-check was skipped"
        )
        return problems, info, attention
    served_sentinels = {
        selector
        for selector in snap.served
        if selector.startswith(render.SENTINEL_PREFIX)
    }
    if (
        snap.sentinel is not None
        and served_sentinels
        and snap.sentinel not in served_sentinels
    ):
        # The on-disk config IS the current render (no drift) but the
        # gateway still serves an older one: the hot reload did not apply.
        # Selector-level diagnoses would all restate this one cause.
        problems.append(
            "gateway did not reload the current render (sentinel missing) — "
            f"restart required: {service.hint_code('restart')}"
        )
        return problems, info, attention
    missing: list[str] = []
    for selector in sorted(snap.expected - snap.served):
        pool = snap.oauth_alias_pools.get(selector)
        if selector in snap.continuity_aliases and (
            pool is None or snap.oauth_records.get(pool, 0) > 0
        ):
            # A continuity alias only keeps old sessions resumable; its
            # upstream may be gone for good. Lazy state, never damage.
            attention.append(gateway_facts.tagged(
                f"continuity alias {selector} is not served (its upstream "
                "route may be gone); once no live session uses it: "
                f"charmer doctor --prune-aliases {selector}",
                area="Gateway", action="unused-alias", subject=selector,
            ))
        else:
            missing.append(selector)
    oauth_missing: dict[str, list[str]] = {}
    direct_missing: list[str] = []
    for selector in missing:
        pool = snap.oauth_alias_pools.get(selector)
        if pool is None:
            direct_missing.append(selector)
        else:
            oauth_missing.setdefault(pool, []).append(selector)
    restart_note = (
        f"{service.hint_code('restart')} (the running registry lacks "
        "it although the current render is loaded; a restart re-registers "
        "every route)"
    )
    for selector in direct_missing[:3]:
        problems.append(
            f"the running gateway does not serve rendered selector "
            f"{selector!r}: {restart_note}"
        )
    if len(direct_missing) > 3:
        problems.append(
            f"…and {len(direct_missing) - 3} more unserved rendered selectors: "
            f"{service.hint_code('restart')}"
        )
    for pool, selectors in sorted(oauth_missing.items()):
        shown = ", ".join(selectors[:3]) + ("…" if len(selectors) > 3 else "")
        if snap.oauth_records.get(pool, 0) == 0:
            info.append(
                f"the {pool} OAuth pool has no credential record: rendered "
                f"selector(s) {shown} stay unserved until "
                f"`{gateway_facts.sign_in_command(pool)}`"
            )
        else:
            problems.append(
                f"the running gateway does not serve rendered selector(s) "
                f"{shown} although a {pool} credential record exists: {restart_note}"
            )
    stale = [
        selector
        for selector in sorted(snap.served - snap.expected)
        # charmer's two prefixes, and the one its selectors had before
        # catalog 39 renamed them (an older gateway may still serve those).
        if selector.startswith(("charmer-", "gpt-multi-", legacy.LEGACY_SELECTOR_PREFIX))
        and not selector.startswith(render.SENTINEL_PREFIX)
    ]
    if stale:
        info.append(
            "gateway serves selector(s) absent from the current render: "
            + ", ".join(stale[:3])
            + ("…" if len(stale) > 3 else "")
            + f" — run {cli_text.APPLY_GATEWAY_COMMAND}; if they persist afterwards, "
            f"{service.hint_code('restart')}"
        )
    # Routable, uncataloged ids from the same
    # /v1/models read — Info only; served is local registration, never
    # upstream access, and absence is never retirement.
    try:
        info.extend(gateway_facts.served_extras_info(runtime, snap))
    except (cli_errors.CharmerError, OSError, ValueError):
        pass
    return problems, info, attention


def _doctor_lan_report(
    runtime: runtime_mod.Runtime, attention: list[str] | None = None,
    lan_observed: dict[str, gateway_facts.LanReachability] | None = None,
) -> list[str]:
    """One short-timeout reachability observation per keyless LAN
    provider with declared lines. Network-scoped Info, never BLOCK; with
    ``attention`` given, a live session bound to an unreachable
    provider's line adds one Attention line per session. Each observation
    is recorded in ``lan_observed`` (base URL -> observation) for the
    readiness report of the same refresh (no second probe)."""

    try:
        layer = runtime.operator_snapshot().layer
    except (cli_errors.CharmerError, OSError, ValueError):
        return []
    info: list[str] = []
    for pid in sorted(layer.providers):
        resolved = layer.providers[pid]
        if resolved.kind != operator_mod.LAN_KIND:
            continue
        keys = {key for key, line in layer.lines.items() if line.provider_id == pid}
        if not keys:
            continue
        observation = (lan_observed or {}).get(resolved.base_url)
        if observation is None:
            observation = gateway_facts.lan_reachability(runtime, resolved.base_url)
            if lan_observed is not None:
                lan_observed[resolved.base_url] = observation
        if observation.state == "unreachable":
            info.append(f"provider {pid}: {observation.text()} — {gateway_facts.LAN_UNREACHABLE_REMEDY} "
                        "(network-scoped; its lines are unusable from here)")
            if attention is not None:
                attention.extend(_lan_live_bindings(runtime, pid, keys, observation.text()))
    return info


def _lan_live_bindings(runtime: runtime_mod.Runtime, pid: str, keys: set[str], text: str) -> list[str]:
    """Live sessions (last event not ``end``; the lock-free record scan
    the operator report uses) bound to a line of an unreachable LAN
    provider: one Attention line per session, never BLOCK."""

    try:
        layer = runtime.operator_snapshot().layer
    except (cli_errors.CharmerError, OSError, ValueError):
        return []
    scan = continuity_mod.scan_records(runtime.session_store.root)
    bound: dict[str, set[str]] = {}
    for key in sorted(keys):
        line = layer.lines.get(key)
        if line is None:
            continue
        for base in operator_mod.line_aliases(line.core_entry):
            for session_id in scan.refs.get(base, frozenset()) & scan.live:
                bound.setdefault(session_id, set()).add(key)
    return [
        f"session {session_id[:8]}: bound to {', '.join(sorted(found))} on provider {pid}, {text} — "
        f"{gateway_facts.LAN_UNREACHABLE_REMEDY} (network-scoped; the session keeps its binding)"
        for session_id, found in sorted(bound.items())
    ]


def _doctor_readiness_report(
    runtime: runtime_mod.Runtime, gateway_reached: bool,
    lan_observed: dict[str, gateway_facts.LanReachability] | None = None,
) -> tuple[list[str], list[str]]:
    """Next-launch readiness of every profile. Readiness never BLOCKs: a bound slot whose local readiness
    fact fails (unserved, no OAuth credential record) is one Attention line
    per profile with its first reason and remedy; unobserved facts (gateway
    not reached, no auth directory, management disabled, no journal, no
    quota) are unknown, never a finding. Authority failures are reported by
    the profile report; a LAN provider's unreachability Attention by the
    LAN report (its observations, shared through ``lan_observed``, also
    make a reachable LAN slot ready here)."""

    try:
        eff = runtime.current_effective()
        bindings = runtime.bindings.bindings()
        names = runtime.profiles.names()
    except (cli_errors.CharmerError, OSError, ValueError, KeyError):
        return [], []
    lcat = runtime.lineup_catalog()
    lineups: list[tuple[str, profile_mod.ResolvedLineup]] = []
    for name in names:
        try:
            document = runtime.profiles.load(name)
        except (profile_mod.ProfileError, OSError):
            continue
        evaluation = profile_mod.evaluate(document, lcat, bindings=bindings, effective=eff, ad_hoc=False)
        if evaluation.errors or evaluation.lineup is None:
            continue
        lineups.append((name, evaluation.lineup))
    try:
        observations = runtime.readiness_observations(
            [lineup for _name, lineup in lineups], served=gateway_reached, lan_observed=lan_observed,
        )
    except (cli_errors.CharmerError, OSError, ValueError, KeyError):
        return [], []
    attention: list[str] = []
    ready = total = 0
    for name, lineup in lineups:
        total += 1
        rows = runtime.lineup_readiness(lineup, observations)
        if readiness_mod.satisfiable(rows):
            ready += 1
        texts = [row.text() for row in rows if row.state == readiness_mod.BLOCKED and row.first is not None
                 and not row.first.authority and row.first.code != "lan"]
        if texts:
            more = f" (+{len(texts) - 1} more)" if len(texts) > 1 else ""
            attention.append(f"profile {name}: next launch: {texts[0]}{more} (readiness; never a block)")
    info = [f"Readiness: {ready} of {total} profile(s) locally ready. {readiness_mod.LEGEND}"]
    return attention, info


def _doctor_gateway_workdir(runtime: runtime_mod.Runtime) -> list[str]:
    """A ``.env`` in the gateway's working directory.

    Existence only (``os.path.lexists``: a regular file, an unreadable one
    or a dangling symlink); the file is never opened. Independent of the
    gateway's readiness, so it shows while the gateway refuses to start.
    """

    workdir = proxy_mod.gateway_workdir(runtime.session_store.root)
    if not proxy_mod.gateway_dotenv_present(workdir):
        return []
    return [
        f"gateway working directory {workdir} holds a .env file: charmer-proxy "
        "refuses to start the gateway while it exists (CLIProxyAPI would load it: "
        "HOME_JWT, DEPLOY, WRITABLE_PATH and the store variables override the "
        "gateway's config); move it aside unless you put it there"
    ]


def _earlier_shim(path: Path, environ: Mapping[str, str]) -> bool:
    """Whether ``path`` is one of the earlier release's hook shims in its
    state root (``charmer.legacy``): what a scope compiled before ``migrate
    from-legacy`` moved the roots names."""

    return path.parent == legacy.state_root(environ) / "bin" and path.name in legacy.LEGACY_SHIMS


def _compiled_by_earlier_release(settings: Mapping[str, Any], environ: Mapping[str, str]) -> bool:
    """Whether a scope's hooks all name the earlier release's shims."""

    targets = scope_mod.hook_targets(settings)
    return bool(targets) and all(_earlier_shim(path, environ) for path in targets)


def _doctor_hook_targets(
    runtime: runtime_mod.Runtime, records: list[dict[str, Any]], *,
    plans: dict[str, ExpectedPlan | str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """``(problems, attention, info)``: a compiled hook command that is
    missing is BLOCKED, except the earlier release's own shims in its state
    root (``charmer.legacy``): a scope compiled before ``migrate
    from-legacy`` moved the roots, recompiled at its next resume (Attention)."""

    problems: list[str] = []
    attention: list[str] = []
    stale = 0
    checked = 0
    for record in records:
        if record.get("version") != 4 or record.get("lineup_generation", 0) < 1:
            continue
        mid = sessions.managed_id(record)
        settings = managed.read_settings(scope_mod.scope_dir(runtime.session_store.root, mid) / "settings.json")
        if not settings:
            continue  # the scope-integrity report owns unreadable settings
        checked += 1
        missing = scope_mod.missing_hook_targets(settings)
        if missing and all(_earlier_shim(path, runtime.environ) for path in missing):
            stale += 1
            # The scope check owns the remedy, including catalog deferrals.
            # Reuse its plan during a report rather than compiling each of
            # these sessions again for the hook-target summary.
            _problems, findings, _info = _check_scope_integrity(runtime, record, plans=plans, earlier_hooks=True)
            attention.extend(findings)
            continue
        for path in missing:
            problems.append(gateway_facts.tagged(
                f"session {mid[:8]}: compiled hook target {paths.display(path, runtime.environ)} is missing or not executable — "
                "its /model fence and lineup hooks silently do not run; restore it: "
                f"charmer doctor --repair {mid}",
                area="Sessions", action="scope-repair", subjects=(mid[:8],),
            ))
    info = [] if problems else [f"Hook targets: every compiled hook command exists ({checked - stale} "
                                f"session(s))."]
    return problems, attention, info


def _doctor_managed_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """Managed policy (names only): what refuses a launch, or locks the
    model, is BLOCKED; the rest is Attention. Denied models are judged per
    launch (the lineup is known there); here they are Attention."""

    problems: list[str] = []
    attention: list[str] = []
    for lock in runtime.managed_findings():
        if lock.kind in managed.UNREADABLE_KINDS:
            if lock.blocks:
                problems.append(
                    f"managed policy {lock.path} {lock.effect} — "
                    "ask the policy owner, or use plain claude"
                )
            else:
                attention.append(f"managed policy {lock.path} {lock.effect}")
        elif lock.blocks or lock.kind == "model":
            problems.append(
                f"managed policy {lock.path} sets {lock.key}: {lock.effect} — "
                "ask the policy owner, or use plain claude"
            )
        else:
            attention.append(f"managed policy {lock.path} sets {lock.key}: {lock.effect}")
    return problems, attention


def _doctor_install_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str], list[str]]:
    """The release channel, the installations, the supervised service and the
    gateway inhibition: ``(problems, attention, info)``."""

    from charmer import release_update

    problems, info = gateway_facts.install_report(runtime)
    attention, service_info = gateway_facts.service_report(runtime)
    try:
        release_info = release_update.doctor_lines(runtime)
    except (cli_errors.CharmerError, OSError, ValueError) as exc:
        release_info = [f"Installed release: unknown ({exc})"]
    release_info = [_trust_line(runtime) if line.startswith("HTTPS trust (") else line for line in release_info]
    held, holding, held_info = gateway_facts.inhibition_report(runtime)
    from charmer import legacy_migration

    remnants = [gateway_facts.Finding(f"{text} — {remedy}", code="earlier-installation",
                                      public="the installation of charmer's earlier name left files here",
                                      remedy=remedy)
                for text, remedy in legacy_migration.remnants(runtime.environ)]
    area = gateway_facts.in_area
    return (
        [*area(problems, INSTALLATION), *area(held, GATEWAY)],
        [*area(attention, GATEWAY), *area(holding, GATEWAY), *area(remnants, INSTALLATION)],
        [*info, *release_info, *service_info, *held_info],
    )


def _doctor_pin_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """``(attention, info)``: the pinned Claude Code and the user's own one.

    Info: the pin, its verification date and the evidence class for this
    platform; the user's ``claude`` on PATH, found by real path and never
    run. Attention when the pin is stale (verified more than 30 days ago, or
    the user's Claude Code is newer): a fact, with no command to run."""

    from charmer import errors as errors_mod, pin as pin_mod

    if not runtime.pin_report:
        return [], []
    contract = runtime.catalog.docs["native-contract"]
    version = pin_mod.version(contract)
    try:
        platform = pin_mod.host_platform()
    except errors_mod.CharmerError as exc:
        return [], [f"Claude Code pin: {version}; this platform is not supported ({exc})."]
    evidence = pin_mod.evidence_class(contract, platform)
    verified = pin_mod.entry(contract).get("verified_at", "unknown")
    proof = f"evidence on {platform}: {evidence}" if evidence else f"no verified build for {platform}"
    info = [f"Claude Code pin: {version} (verified {verified}; {proof})."]
    client = pin_mod.installed_client(runtime.environ)
    if client is None:
        info.append(f"Your Claude Code: none on PATH; this charmer runs its own copy of {version}.")
    else:
        shown = paths.display(client.real, runtime.environ)
        if client.version is None:
            info.append(f"Your Claude Code: {shown} (its path names no version; it is never run "
                        f"to ask); this charmer runs {version}.")
        else:
            info.append(f"Your Claude Code is {client.version} ({shown}); this charmer runs {version}.")
    stale = pin_mod.staleness(contract, gateway_facts._doctor_now().date(),
                              client.version if client is not None else None)
    return ([stale] if stale else []), info


def _doctor_proxy_report(runtime: runtime_mod.Runtime, records: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    info: list[str] = []
    contexts = [(runtime.cwd, None)] + [(r["cwd"], r) for r in records if isinstance(r.get("cwd"), str)]
    for cwd, record in contexts:
        layers = managed.env_layers({**runtime.environ, "HOME": str(runtime.home)}, cwd, runtime.managed_root)
        # Flag settings outrank user/project settings; managed policy outranks flags.
        # env_layers deduplicates paths (e.g. cwd == HOME), so index 3 is not
        # always the policy boundary.
        policy_paths = set(managed.settings_paths(runtime.managed_root))
        flag_index = next((i for i, (path, _env) in enumerate(layers) if path in policy_paths), len(layers))
        scope_path = None
        scope_env = {}
        if record is not None and record.get("lineup_generation", 0) >= 1:
            scope_path = scope_mod.scope_dir(runtime.session_store.root, sessions.managed_id(record)) / "settings.json"
            scope_env = managed.settings_env(scope_path)
            layers.insert(flag_index, (scope_path, scope_env))
        else:
            # No compiled scope yet: audit the flags a managed launch would
            # actually compile, not an unprotected shell-only environment.
            bypass = compiler.loopback_no_proxy(runtime.environ, *(env for _path, env in layers))
            layers.insert(flag_index, (None, bypass))
        effective = dict(runtime.environ)
        sources = {key: "launch environment" for key in effective}
        for path, env in layers:
            effective.update(env)
            source = paths.display(path, runtime.environ) if path is not None else "compiled flag settings"
            sources.update({key: source for key in env})
        if not compiler.proxy_effective(effective):
            continue
        # Both spellings must protect clients that prefer either one; absent
        # aliases inherit the other. Never render a proxy URL or its credentials.
        for name in ("NO_PROXY", "no_proxy"):
            other = "no_proxy" if name == "NO_PROXY" else "NO_PROXY"
            selected = name if name in effective else other
            hosts = {host.strip().lower() for host in effective.get(selected, "").split(",")}
            if "*" in hosts or set(compiler.LOOPBACK_HOSTS) <= hosts:
                continue
            # Attribute an unsafe override to the layer that supplied it;
            # absent bypass values belong to the effective proxy's source.
            proxy_name = next(key for key, value in effective.items()
                              if compiler.proxy_effective({key: value}))
            source = sources.get(selected, sources[proxy_name])
            repair = ""
            compiled_hosts = {host.strip().lower() for host in
                              scope_env.get(name, scope_env.get(other, "")).split(",")}
            if scope_path is not None and not ("*" in compiled_hosts or set(compiler.LOOPBACK_HOSTS) <= compiled_hosts):
                repair = (f"; recompile the bypass in {paths.display(scope_path, runtime.environ)}: "
                          f"charmer doctor --repair {sessions.managed_id(record)}")
            problems.append(
                f"{source}: env.{name} leaves out the loopback addresses while a proxy is set — "
                "charmer sessions would send the gateway token to the proxy; "
                "add 127.0.0.1,localhost,::1 to env.NO_PROXY and env.no_proxy" + repair
            )
        if compiler.proxy_effective(runtime.environ):
            info.append("A proxy variable is set in this shell; managed launches compile a sticky "
                        "127.0.0.1,localhost,::1 bypass into NO_PROXY/no_proxy for daemon relaunches too.")
    return list(dict.fromkeys(problems)), list(dict.fromkeys(info))


def _init_problem_line(text: str) -> str:
    """The BLOCK line of a launcher that could not prepare its state (the
    report itself ran read-only)."""

    what, _sep, remedy = text.partition("\n  fix: ")
    return gateway_facts.Finding(
        f"charmer could not prepare its state: {what}" + (f" — {remedy}" if remedy else ""),
        code="state-unprepared", public="charmer could not prepare its state; this report ran read-only",
        remedy=remedy or None,
    )


def _trust_line(runtime: runtime_mod.Runtime) -> str:
    """The certificate trust of the launcher's own requests, with every
    ``SSL_CERT_FILE``/``SSL_CERT_DIR`` value shown home-relative like every
    other path (a relative one from the working directory, where the
    launcher opens it)."""

    environ = {**runtime.environ, "HOME": str(runtime.home)}
    _cafile, _capath, description = tls.source(environ, show=lambda value: paths.display(value, environ))
    return f"HTTPS trust (update, Claude Code download): {description}"


def _collect_doctor_reports(runtime, *, reload_failed=None):
    with runtime.report_snapshot():
        return _collect_doctor_report_lines(runtime, reload_failed=reload_failed)


def _collect_doctor_report_lines(
    runtime: runtime_mod.Runtime, *, reload_failed: Callable[[], service.ExecReloadResult | bool | None] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """(problems, info, attention) for doctor — shared by CLI and the TUI.

    Same checks, same lines: composition validation and secrets, binary
    verification, daemon, contract source, gateway readiness, scope/session
    integrity, collisions, the re-pin early-warning.
    """

    try:
        marker = sessions.check_state_marker(runtime.session_store.root)
    except sessions.StateMarkerError as exc:
        # Do not parse newer records/custom registries with the legacy schemas
        # or report one misleading corruption problem per migrated record.
        # The census counts UUID records only, never the
        # sessions/<id>.v3.json backups.
        count = len(runtime.session_store.scan_uuid_records())
        return [gateway_facts.tagged(f"state marker: {exc}", area=SESSIONS)], [
            f"Sessions: {count} recorded; not inspected by this launcher."
        ], []

    area = gateway_facts.in_area
    problems: list[str] = []
    if runtime.init_problem is not None:
        problems.extend(area([_init_problem_line(runtime.init_problem)], INSTALLATION))
    custom_conflicts = runtime.custom_conflicts()
    if custom_conflicts:
        # Ignored by the merge (catalog wins) but a real misconfiguration:
        # attention with the exact fix, never silent.
        scope_attention_extra = area([
            "custom registry entries shadow catalog ids and were IGNORED: "
            + ", ".join(custom_conflicts)
            + f" — remove or rename them in {paths.display(custom.registry_path(runtime.environ), runtime.environ)}"
        ], PROVIDERS)
    else:
        scope_attention_extra = []
    registry = paths.display(custom.registry_path(runtime.environ), runtime.environ)
    for provider, header in runtime.custom_header_violations():
        scope_attention_extra.extend(area([
            f"custom provider {provider} was IGNORED: auth header {header!r} — this gateway build "
            f'honors only x-api-key (set "header": "x-api-key" in {registry}, or remove it: '
            f"charmer custom remove-provider {provider}); its models are not served"
        ], PROVIDERS))
    if runtime.broken_override_error is not None:
        # The operator contract override could not be removed and was
        # ignored (the packaged contract is in effect): remove the file.
        override = paths.display(sessions.config_root(runtime.environ) / "native-contract.json", runtime.environ)
        problems.extend(area([
            "the contract override is invalid and was IGNORED (packaged "
            f"contract in effect): {runtime.broken_override_error}; remove it: rm {override}"
        ], INSTALLATION))
    # The config and state roots and the secret files first (before the pin
    # and the gateway): a damaged root otherwise surfaces as a later error
    # (a gateway key error, say) whose remedy cannot work.
    files_problems, flagged_files = gateway_facts.local_files_report(runtime)
    problems.extend(area(files_problems, INSTALLATION))
    # Profiles and named bindings replace the legacy composition
    # loop (the composition store is not read by doctor).
    profile_problems, profile_attention = _doctor_profile_report(runtime)
    problems.extend(area(profile_problems, PROFILES))
    scope_attention_extra.extend(area(profile_attention, PROFILES))
    binary_problems, binary_info = runtime.doctor_binary_callback(
        runtime.catalog.docs["native-contract"]
    )
    problems.extend(area(binary_problems, INSTALLATION))
    retained_attention, retained_info = retention.retention_report(
        runtime.catalog.docs["native-contract"], runtime_mod._packaged_contract(runtime), runtime.environ
    )
    scope_attention_extra.extend(area(retained_attention, INSTALLATION))
    binary_info.extend(retained_info)
    pin_attention, pin_info = _doctor_pin_report(runtime)
    scope_attention_extra.extend(area(pin_attention, INSTALLATION))
    binary_info.extend(pin_info)
    scope_attention_extra.extend(area(runtime.settings_skew(runtime.cwd), INSTALLATION))
    daemon = runtime.doctor_daemon_callback()
    info_lines = [*binary_info, f"Shared daemon: {daemon.summary}."]
    from charmer import pin as pin_mod

    stale_override_attention = pin_mod.override_attention(
        runtime.catalog.contract_source, runtime.catalog.contract_override_detail,
        pin_mod.version(runtime.catalog.docs["native-contract"]),
        paths.display(sessions.config_root(runtime.environ) / "native-contract.json", runtime.environ),
    )
    # Before the readiness branch: the line matters most while the gateway
    # refuses to start because of it.
    scope_attention_extra.extend(area(_doctor_gateway_workdir(runtime), GATEWAY))
    managed_problems, managed_attention = _doctor_managed_report(runtime)
    problems.extend(area(managed_problems, INSTALLATION))
    scope_attention_extra.extend(area(managed_attention, INSTALLATION))
    operator_token: str | None = None
    platform_problems, platform_info = gateway_facts.platform_report(runtime)
    problems.extend(area(platform_problems, INSTALLATION))
    info_lines.extend(platform_info)
    install_problems, install_attention, install_info = _doctor_install_report(runtime)
    problems.extend(install_problems)
    scope_attention_extra.extend(install_attention)
    info_lines.extend(install_info)
    if runtime.doctor_callback is not None:
        problems.extend(area(runtime.doctor_callback(runtime), GATEWAY))
    else:
        refresh_failures = frozenset()
        try:
            gateway_token = runtime.check_readiness()
        except launch.GatewayKeyError as exc:
            if not flagged_files & {proxy_mod.config_dir(runtime.home), proxy_mod.config_dir(runtime.home) / "api-key"}:
                problems.extend(area([f"local gateway key file: {exc} — {exc.remedy}"], GATEWAY))
        except launch.LaunchError as exc:
            # While it is stopped or failed after a gateway-changing update,
            # the way back too.
            rollback = gateway_facts.rollback_remedy(runtime)
            problems.extend(area([f"local gateway: {exc} — {exc.remedy or launch.gateway_unit_remedy(runtime.home)}"
                                  + (f"; {rollback}" if rollback and rollback not in str(exc.remedy) else "")],
                                 GATEWAY))
        else:
            scope_attention_extra.extend(area(service.pending_restart_lines(
                proxy_mod.gateway_workdir(runtime.session_store.root),
                signature=proxy_mod.exec_signature(), launcher_version=runtime.launcher_version,
                reload_failed=(reload_failed if reload_failed is not None else
                               None if runtime.health_get is None and runtime.served_models_callback is None
                               else lambda: False),
                # The manager is asked about the recorded unit only (none on demand).
                backend=service.backend_of(runtime.home),
            ), GATEWAY))
            operator_token = gateway_token
            runtime.gateway_attention = []
            quota_attention, quota_info, refresh_failures = _doctor_quota_report(runtime)
            scope_attention_extra.extend(area(quota_attention, PROVIDERS))
            info_lines.extend(quota_info)
            try:
                launch.check_listener(endpoint.gateway_endpoint(runtime.catalog.docs["gateway"]).base_url,
                                      owner_check=runtime.listener_owner,
                                      attention=runtime.gateway_attention.append)
            except launch.GatewayOwnerError as exc:
                runtime.gateway_attention.append(str(exc).removeprefix("BLOCK: "))
                info_lines.append("Gateway listener ownership: the served-selector check was skipped; token not sent.")
            else:
                if runtime.doctor_served_callback is not None:
                    served_problems, served_info = runtime.doctor_served_callback(runtime, gateway_token)
                    served_attention: list[str] = []
                else:
                    served_problems, served_info, served_attention = _doctor_served_checks(runtime, gateway_token)
                problems.extend(area(served_problems, GATEWAY))
                info_lines.extend(served_info)
                scope_attention_extra.extend(area(served_attention, GATEWAY))
            scope_attention_extra.extend(area(dict.fromkeys(runtime.gateway_attention), GATEWAY))
        # One bounded journal read, even when readiness failed: a save failure's
        # keep-running hold must not disappear behind an unrelated key/health error.
        scope_attention_extra.extend(area(_doctor_journal_report(
            runtime, refresh_failures=refresh_failures, info_lines=info_lines,
        ), PROVIDERS))
        facts_attention, facts_info = gateway_facts.gateway_runtime_report(runtime)
        scope_attention_extra.extend(area(facts_attention, GATEWAY))
        info_lines.extend(facts_info)
    # The operator layer (B01-B05 / A01-A08 / I01-I02).
    operator_blocks, operator_attention, operator_info = _doctor_operator_report(runtime, operator_token)
    problems.extend(area(operator_blocks, PROVIDERS))
    scope_attention_extra.extend(area(operator_attention, PROVIDERS))
    info_lines.extend(operator_info)
    lan_attention: list[str] = []
    lan_observed: dict[str, gateway_facts.LanReachability] = {}
    info_lines.extend(_doctor_lan_report(runtime, lan_attention, lan_observed))
    scope_attention_extra.extend(area(lan_attention, PROVIDERS))
    readiness_attention, readiness_info = _doctor_readiness_report(
        runtime, operator_token is not None, lan_observed,
    )
    scope_attention_extra.extend(area(readiness_attention, PROFILES))
    info_lines.extend(readiness_info)
    # Report-local only: a later doctor pass must observe the new catalog,
    # records and scopes. The hook-target check shares these evaluations.
    plans: dict[str, ExpectedPlan | str] = {}
    scope_info, scope_problems, scope_attention = _doctor_scope_report(runtime, plans=plans)
    scope_attention = area(scope_attention, SESSIONS)
    info_lines.extend(scope_info)
    problems.extend(area(scope_problems, SESSIONS))
    marker_info, marker_attention = _doctor_marker_report(runtime, marker)
    info_lines.extend(marker_info)
    scope_attention.extend(area(marker_attention, SESSIONS))
    scope_attention.extend(scope_attention_extra)
    records = session_facts._session_records(runtime)
    liveness = _session_liveness(runtime)
    hook_problems, hook_attention, hook_info = _doctor_hook_targets(runtime, records, plans=plans)
    problems.extend(area(hook_problems, SESSIONS))
    # Missing earlier shims repeat the scope check's per-session finding.
    # Drop only exact repeats; a distinct hook finding must remain visible.
    already_reported = set(scope_attention)
    scope_attention.extend(area([line for line in hook_attention if line not in already_reported], SESSIONS))
    info_lines.extend(hook_info)
    scope_attention.extend(area(_doctor_client_report(runtime, records), SESSIONS))
    record_attention, record_info = _doctor_record_report(runtime, records)
    scope_attention.extend(area(record_attention, SESSIONS))
    info_lines.extend(record_info)
    scope_attention.extend(area(_doctor_dead_sessions(records, liveness), SESSIONS))
    # The settings radar (user + every record cwd's project
    # and local settings) replaces _subagent_model_override_paths; the
    # accepted CLAUDE_CODE_SUBAGENT_MODEL wording is kept below.
    proxy_problems, proxy_info = _doctor_proxy_report(runtime, records)
    problems.extend(area(proxy_problems, INSTALLATION))
    info_lines.extend(proxy_info)
    radar = _settings_radar(runtime, records)
    scope_attention.extend(area(radar.attention, INSTALLATION))
    info_lines.extend(radar.info)
    parity = custom.registry_parity(runtime.environ)
    if parity is not None:
        scope_attention.extend(area([parity], PROVIDERS))
    if runtime.environ.get("CHARMER_SECRET_ENV"):
        try:
            service_file = paths.display(secret_store.service_key_file(runtime.environ).path, runtime.environ)
        except secret_store.SecretStoreError as exc:
            service_file = f"nothing until its pointer is fixed ({exc})"
        info_lines.append(
            "CHARMER_SECRET_ENV is set in this shell; the gateway service ignores it and reads "
            + service_file
        )
    _feedback, preferences_problem = settings_mod.feedback_drafts_preference(runtime.environ)
    if preferences_problem is not None:
        scope_attention.extend(area([
            f"{preferences_problem}: managed sessions use the default claude_feedback_drafts "
            f"{settings_mod.FEEDBACK_DRAFTS_DEFAULT}; fix or remove {settings_mod.preferences_path(runtime.environ)}"
        ], INSTALLATION))
    user_attention, user_info = _doctor_user_settings_report(runtime, records)
    scope_attention.extend(area(user_attention, INSTALLATION))
    info_lines.extend(user_info)
    scope_attention.extend(area(_doctor_config_dir_report(runtime), INSTALLATION))
    keep_attention, keep_info = _doctor_env_keep_report(runtime)
    scope_attention.extend(area(keep_attention, INSTALLATION))
    info_lines.extend(keep_info)
    scope_attention.extend(area(_doctor_new_lines_report(runtime), MODELS))
    scope_attention.extend(area(_doctor_retirement_radar(runtime), MODELS))
    scope_attention.extend(area(_doctor_hook_errors(runtime, records), SESSIONS))
    scope_attention.extend(area(_doctor_leftovers(runtime, liveness), SESSIONS))
    # User/project settings are merged after process-environment cleanup, so
    # both subagent-model controls need a read-only radar. Agent
    # frontmatter wins over CLAUDE_CODE_SUBAGENT_MODEL on the pinned client
    # (an observed client fact), so the default control reaches workflow agents without
    # an agentType and native general-purpose spawns, never native Explore or
    # Plan (the settings-environment placement probe); the compiled Settings
    # workflow_default_binding replaces it.
    installation_from = len(scope_attention)
    default_override_paths = list(radar.subagent_paths["CLAUDE_CODE_SUBAGENT_MODEL"])
    if default_override_paths:
        scope_attention.append(
            "CLAUDE_CODE_SUBAGENT_MODEL is set in "
            + ", ".join(default_override_paths)
            + " — it becomes the model of workflow agents started without a "
            "cm-* agentType and of native general-purpose agents (cm-* agents "
            "keep their bindings; native Explore and Plan are not affected); a "
            "value outside the session fence silently "
            "falls back to the lead. Use Settings workflow_default_binding "
            "instead (compiled into the session's own settings) and remove it here"
        )
    force_override_paths = list(radar.subagent_paths["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"])
    if force_override_paths:
        scope_attention.append(
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set in "
            + ", ".join(force_override_paths)
            + " — Claude 2.1.257+ treats this flag as a global override, "
            "applying CLAUDE_CODE_SUBAGENT_MODEL (or the main model) to every "
            "subagent while ignoring per-spawn and agent-definition models. "
            "Remove it to keep composition routing intact"
        )
    if stale_override_attention is not None:
        scope_attention.append(stale_override_attention)
    if runtime.contract_notice is not None:
        # The legacy override removal healed the state; say so, never BLOCK.
        scope_attention.append(f"contract override removed: {runtime.contract_notice}")
    scope_attention[installation_from:] = area(scope_attention[installation_from:], INSTALLATION)
    rotation_attention = _token_rotation_attention(runtime)
    if rotation_attention is not None:
        scope_attention.extend(area([rotation_attention], GATEWAY))
    key_attention, key_info = _management_key_attention(runtime)
    if key_attention is not None:
        scope_attention.extend(area([key_attention], GATEWAY))
    if key_info is not None:
        info_lines.append(key_info)
    # The confirmed-export Info line (read-only; only
    # `export --out` writes the receipt, after a fsynced file).
    try:
        info_lines.append(portability.receipt_info_line(
            portability.read_receipt(runtime.session_store.root, asset_root=runtime.asset_root),
            now=gateway_facts._doctor_now()))
    except portability.PortabilityError as exc:
        info_lines.append(portability.receipt_error_line(exc))
    collision_info, collision_problems = _doctor_collision_report(runtime)
    info_lines.append(collision_info)
    problems.extend(area(collision_problems, INSTALLATION))
    info_lines.append(f"Evidence: {cli_text.EVIDENCE_ADD_DIR_CARRY}")
    return problems, info_lines, scope_attention


def _doctor_operator_report(
    runtime: runtime_mod.Runtime, gateway_token: str | None,
) -> tuple[list[str], list[str], list[str]]:
    """Operator diagnostics from one explicit snapshot (the render's
    own inputs), the lock-free record scan and the profile/binding metadata.

    Nothing is reported (and nothing extra is read) without operator state
    or a legacy registry. The served set and the overlay conflicts come from
    the shared gateway snapshot only when readiness already reached the
    gateway (``gateway_token``); otherwise served checks are skipped.
    """

    try:
        snapshot = runtime.operator_snapshot()
        legacy = custom.load_registry(runtime.environ)
    except (cli_errors.CharmerError, OSError, ValueError) as exc:
        return [f"operator layer unreadable: {exc}"], [], []
    layer = snapshot.layer
    legacy_present = bool(legacy.get("providers") or legacy.get("models"))
    if not (layer.providers or layer.lines or layer.problems or layer.displaced or layer.pending
            or snapshot.ledger_present or legacy_present or layer.marker):
        return [], [], []
    env = runtime.gateway_environ()
    served: frozenset[str] | None = None
    conflicts: tuple[str, ...] = ()
    if gateway_token is not None:
        snap = gateway_facts._gateway_snapshot(runtime, gateway_token)
        served = snap.served
        conflicts = tuple(conflict.text() for conflict in snap.overlay_conflicts)
    try:
        plan: operator_mod.RenderPlan | None = runtime.operator_render_plan()
    except (cli_errors.CharmerError, ValueError):
        plan = None
    scan = continuity_mod.scan_records(runtime.session_store.root)
    try:
        admitted = frozenset(runtime.settings_store.load().get("admitted_lines", []))
    except settings_mod.SettingsError:
        admitted = frozenset()
    schemas = snapshot.schemas or operator_mod.load_schemas(runtime.asset_root)
    try:
        evidence = operator_mod.load_evidence(env, schemas)
    except operator_mod.OperatorError:
        evidence = None
    legacy_sha: str | None = None
    try:
        registry = custom.registry_path(runtime.environ)
        if os.path.lexists(registry):
            legacy_sha = strict_json.sha256_hex(state.read_private(registry))
    except state.StateError:
        legacy_sha = None
    marker_doc = marker_error = None
    try:
        marker_doc = operator_mod.read_marker(env)
    except operator_mod.OperatorError as exc:
        marker_error = str(exc)
    unset: list[str] = []
    for selection in (plan.transports if plan is not None else {}).values():
        if selection.alternative is not None and selection.problem is None:
            try:
                if not secret_store.default_store(env).is_set(selection.alternative.secret_name):
                    unset.append(selection.alternative.secret_name)
            except secret_store.SecretStoreError:
                unset.append(selection.alternative.secret_name)
    agent_attention, agent_eligible = runtime.operator_agent_findings(snapshot, scan.live)
    gate = runtime.agent_gate(snapshot)
    agent_qualified = sum(facts.evidence == "current" and (not facts.pool or facts.exact_client == "current")
                          for facts in gate.facts.values())
    findings = operator_mod.doctor_findings(
        runtime.catalog.docs, snapshot, plan=plan, admitted=admitted, refs=scan.refs, live=scan.live,
        unreadable=(*scan.unreadable, *(("sessions directory",) if scan.directory_error else ())),
        key_refs=runtime.operator_key_references(), served=served, evidence=evidence, legacy=legacy,
        legacy_sha=legacy_sha, marker_doc=marker_doc, marker_error=marker_error, overlay_conflicts=conflicts,
        unset_secrets=unset, agent_eligible=agent_eligible, agent_qualified=agent_qualified,
    )
    warnings = _operator_agent_warnings(runtime, snapshot, gate)
    agent_lines = [_tag_model(message, warnings[message][0], key=warnings[message][1]) if message in warnings
                   else gateway_facts.tagged(message, area="Models") for message in agent_attention]
    return list(findings.blocks), [*findings.attention, *agent_lines], list(findings.info)


def _operator_agent_warnings(
    runtime: runtime_mod.Runtime, snapshot: operator_mod.OperatorSnapshot, gate: Any,
) -> dict[str, tuple[Any, str]]:
    """message -> (finding, line key) of the warnings
    ``Runtime.operator_agent_findings`` words, from the same gate and
    eligibility evaluation (each declared line at its default effort, and
    each live session's recorded agent effort), so its lines keep their
    codes."""

    found: dict[str, tuple[Any, str]] = {}
    try:
        agent_efforts = tuple(runtime.catalog.docs["native-contract"]["agent_efforts"]["values"])
    except (KeyError, TypeError):
        return found
    for key, facts in sorted(gate.facts.items()):
        line = snapshot.layer.lines.get(key)
        if line is None:
            continue
        entry = line.core_entry
        efforts = {entry.get("default_effort")}
        for record in session_facts._session_records(runtime):
            for binding in ((record.get("applied") or {}).get("agents") or {}).values():
                if isinstance(binding, dict) and binding.get("key") == key:
                    efforts.add(binding.get("effort"))
        for effort in efforts:
            try:
                verdict = profile_mod.agent_eligibility(entry, key=key, slot=None, effort=str(effort), facts=facts,
                                                        agent_efforts=agent_efforts)
            except (KeyError, TypeError, ValueError):
                continue
            for finding in verdict.warnings:
                found.setdefault(finding.message, (finding, key))
    return found


def _doctor_client_report(runtime: runtime_mod.Runtime, records: list[dict[str, Any]]) -> list[str]:
    """Known executable mismatches are Attention only."""

    from charmer import pin as pin_mod

    contract = runtime.catalog.docs["native-contract"]
    version = pin_mod.version(contract)
    try:
        owned = pin_mod.owned_path(runtime.environ, version, pin_mod.host_platform())
    except cli_errors.CharmerError:
        return []
    clients = sessions.proc_client_mismatches(
        (rid for record in records for rid in sessions.record_session_ids(record)),
        owned,
        retained=retention.retained_path(paths.retained_root(runtime.environ), version),
        proc_root=runtime.proc_root,
    )
    attention: list[str] = []
    for record in records:
        mismatches = sorted({clients[rid] for rid in sessions.record_session_ids(record) if rid in clients})
        for client in mismatches:
            mid = sessions.managed_id(record)
            m8 = mid[:8]
            attention.append(_per_session(
                f"session {m8} runs Claude {client}, not the pinned {version} "
                "(it started before a re-pin, or the daemon adopted it after an auto-update); "
                f"resume it with charmer -r {mid} after it exits",
                mid, f"client:{client}",
                f"{{n}} sessions run Claude {client}, not the pinned {version} (they started before a re-pin, or "
                "the daemon adopted them after an auto-update); resume each with charmer -r <id> after it exits",
            ))
    return attention


def _doctor_marker_report(runtime: runtime_mod.Runtime, marker: int) -> tuple[list[str], list[str]]:
    """State-marker health: (info, attention); records read lock-free, never raising."""

    store = runtime.session_store
    root = store.root
    info: list[str] = []
    attention: list[str] = []
    legacy = 0
    missed: list[str] = []
    for path in store.scan_uuid_records():
        try:
            raw, _raw_bytes = store.load_raw(path.stem)
        except sessions.SessionError:
            continue  # the scope report BLOCKs on unreadable records
        if raw.get("version") != sessions.RECORD_VERSION:
            legacy += 1
            continue
        migration = raw.get("migration")
        if (
            migration is not None
            and raw.get("last_event_source") != "end"
            and str(raw.get("last_seen_at", "")) <= str(migration.get("migrated_at", ""))
        ):
            missed.append(path.stem)
    if legacy and marker < sessions.SUPPORTED_STATE_VERSION:
        attention.append(gateway_facts.Finding(
            f"state not migrated yet: {legacy} legacy record(s); run charmer "
            "migrate --dry-run, then charmer migrate",
            code="legacy-records", public="legacy session records are not migrated yet",
            remedy="charmer migrate --dry-run, then charmer migrate",
        ))
    elif legacy:
        attention.append(gateway_facts.Finding(
            f"{legacy} legacy record(s) (version ≤ 3) remain: rerun charmer migrate",
            code="legacy-records", public="legacy session records remain", remedy="charmer migrate",
        ))
    for managed in missed:
        attention.append(_per_session(
            f"session {managed[:8]} was live while it was migrated and no session "
            "event has reached it since; if it was /clear-ed or resumed then, check "
            f"its runtime id (charmer sessions show {managed}; charmer "
            f"sessions relink-runtime {managed} <runtime-id>)",
            managed, "missed-migration",
            "{n} sessions were live while they were migrated and no session event has reached them since; if one "
            "was /clear-ed or resumed then, check its runtime id (charmer sessions show <id>; charmer sessions "
            "relink-runtime <id> <runtime-id>)",
        ))
    if hooks.migration_lock_held(root):
        info.append("migration or restore in progress (session hooks skip record writes)")
    backups = len(list(store.sessions_dir.glob(f"*{sessions.BACKUP_SUFFIX}")))
    if backups:
        info.append(f"{backups} legacy backup(s) kept for charmer restore-2x")
    for name in ("quarantine-3x", "restored-2x"):
        if os.path.lexists(root / name):
            info.append(f"{paths.display(root / name, runtime.environ)} holds files moved aside by "
                        "charmer restore-2x")
    return info, attention


def _provider_credential_gaps(
    runtime: runtime_mod.Runtime, lineup: profile_mod.ResolvedLineup
) -> list[tuple[str, str]]:
    """``(provider id, problem)`` per lineup provider without a credential.

    §13.1 / sc[7]: the problems are exactly those of
    ``Runtime.lineup_secret_problems``, the function the launch path blocks
    on; each names its provider as ``provider <display> (<id>): ...``.
    """

    docs = runtime.ordinary_docs
    lines = (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]
    provider_ids = {
        lines[key]["provider"]
        for key in [lineup.lead.binding.key, *(a.binding.key for a in lineup.agents.values())]
        if key in lines
    }
    try:
        problems = runtime.lineup_secret_problems(lineup)
    except KeyError:
        return []
    gaps: list[tuple[str, str]] = []
    for problem in problems:
        provider_id = next(
            (pid for pid in sorted(provider_ids) if f" ({pid}): " in problem), None
        )
        if provider_id is not None:
            gaps.append((provider_id, problem))
    return gaps


def _doctor_profile_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """Every profile evaluated with current Settings; ``(problems, attention)``."""

    problems: list[str] = []
    attention: list[str] = []
    lcat = runtime.lineup_catalog()
    try:
        eff = runtime.current_effective()
    except cli_errors.CLIError as exc:
        return [f"settings: {exc}"], []
    binding_map: dict[str, Any] = {}
    try:
        store = runtime.bindings
        binding_map = store.bindings()
        attention.extend(f"named bindings: {line}" for line in store.conflicts(lcat))
    except (profile_mod.BindingError, profile_mod.ProfileError, catalog.CatalogError) as exc:
        problems.append(f"named bindings: {exc}")
    for name in sorted(binding_map):
        model = binding_map[name].get("model") if isinstance(binding_map[name], dict) else None
        if not isinstance(model, str):
            continue
        try:
            notice = lcat.resolve_key(model).notice
        except catalog.CatalogError:
            notice = f"unknown model {model!r}"
        if notice is not None:
            # A stored binding on a retired key is tolerated
            # on load and refused on the next write of that name.
            attention.append(f"bindings.{name}.model: {notice}; bind the live line")
    profiles = runtime.profiles
    try:
        names = profiles.names()
    except (profile_mod.ProfileError, OSError) as exc:
        return problems + [f"profiles: {exc}"], attention
    for name in names:
        try:
            document = profiles.load(name)
        except (profile_mod.ProfileError, OSError) as exc:
            problems.append(f"profile {name}: {exc}")
            continue
        evaluation = profile_mod.evaluate(
            document, lcat, bindings=binding_map, effective=eff, ad_hoc=False
        )
        if evaluation.errors or evaluation.lineup is None:
            problems.extend(f"profile {name}: {error}" for error in evaluation.errors)
            continue
        lineup = evaluation.lineup
        for finding in lineup.notices:
            if finding.code in {"retired-successor", "retired-unbound"}:
                attention.append(f"profile {name}: {finding.message}")
        attention.extend(_tag_model(f"profile {name}: {finding.message}", finding, key=_slot_key(lineup, finding.slot),
                                    owner=name, slot=finding.slot)
                         for finding in lineup.warnings if finding.code in profile_mod.MODEL_WARNING_CODES)
        try:
            workflow_warnings = scope_mod.workflow_default_warnings(
                lcat, eff, policy=lineup.policy or profile_mod.default_policy(eff))
        except scope_mod.ScopeError as exc:
            problems.append(f"profile {name}: {exc}")
        else:
            workflow_key = _workflow_key(lcat, eff)
            attention.extend(_tag_model(f"profile {name}: {finding.message}", finding, key=workflow_key, owner=name,
                                        slot="workflow default") for finding in workflow_warnings)
        gaps = _provider_credential_gaps(runtime, lineup)
        # A shipped profile for providers not set up at all is simply not
        # connected here, not a credential gap to fix.
        from charmer.setup import defaults as setup_defaults

        if gaps and setup_defaults.seed_not_in_use(runtime, name, lineup,
                                                   {provider_id for provider_id, _problem in gaps}):
            continue
        for provider_id, _problem in gaps:
            hint = gateway_facts._connect_hint(runtime, provider_id)
            keyed = _keyed_provider(runtime, provider_id)
            attention.append(gateway_facts.tagged(
                f"profile {name}: provider {provider_id} has no credential ({hint})",
                area="Providers", action="missing-key" if keyed else None, subject=provider_id,
                group=("profiles", "credential", provider_id), subjects=(name,),
                summary=f"provider {provider_id} has no credential ({hint}); {{n}} profiles use it",
            ))
    # The card and the editor already show a newer shipped seed;
    # doctor names it too, so a script-only operator sees it.
    try:
        seed_updates = profiles.seed_updates()
    except (profile_mod.ProfileError, OSError):
        seed_updates = []
    for name, installed, shipped in seed_updates:
        attention.append(gateway_facts.tagged(
            f"profile {name}: a newer shipped version of this seed exists "
            f"(v{installed if installed is not None else '?'} → v{shipped}): "
            f"charmer profile reseed {name} (your version is kept as a backup)",
            action="seed-update", subject=name,
        ))
    return problems, attention


def _slot_key(lineup: profile_mod.ResolvedLineup, slot: str | None) -> str | None:
    """The line key bound to ``slot`` (the lead or an agent), or None for a
    lineup-wide finding."""

    if slot is None:
        return None
    if slot == catalog.LEAD_ROLE:
        return lineup.lead.binding.key
    agent = lineup.agents.get(slot)
    return agent.binding.key if agent is not None else None


def _workflow_key(lcat: profile_mod.LineupCatalog, eff: settings_mod.Effective) -> str | None:
    binding = eff.workflow_default_binding
    if not isinstance(binding, dict) or not isinstance(binding.get("model"), str):
        return None
    try:
        return lcat.resolve_key(binding["model"]).key
    except catalog.CatalogError:
        return None


def _keyed_provider(runtime: runtime_mod.Runtime, provider_id: str) -> bool:
    """Whether ``provider_id`` connects with an API key (G → K sets it)."""

    try:
        transport = runtime.effective_transport_docs()["providers"]["providers"][provider_id]["transport"]
    except (KeyError, TypeError, cli_errors.CharmerError):
        return False
    return isinstance(transport.get("auth"), dict) and str(transport["auth"].get("secret_ref", "")).startswith("env:")


def _doctor_record_report(
    runtime: runtime_mod.Runtime, records: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Record health over v4 records; ``(attention, info)``; never raises."""

    attention: list[str] = []
    info: list[str] = []
    lcat = runtime.lineup_catalog()
    try:
        profile_names = set(runtime.profiles.names())
    except (profile_mod.ProfileError, OSError):
        profile_names = None
    try:
        eff: settings_mod.Effective | None = runtime.current_effective()
    except cli_errors.CLIError:
        eff = None  # the profile report BLOCKs on unreadable Settings
    needs_choice: list[str] = []
    pending_choices: dict[str, int] = {}
    # Sessions on an externalized provider's retired key (its sample
    # is not declared) — a notice only while something references it.
    externalized: dict[str, list[str]] = {}
    for record in records:
        if record.get("version") != sessions.RECORD_VERSION:
            continue
        mid = sessions.managed_id(record)
        m8 = mid[:8]
        rid = sessions.runtime_session_id(record)
        applied = record["applied"]
        lead_key = applied["lead"]["key"]
        retired_entry = lcat.retired.get(lead_key) if isinstance(lead_key, str) else None
        if (isinstance(retired_entry, dict) and isinstance(retired_entry.get("externalized"), dict)
                and retired_entry.get("provider") not in lcat.providers):
            externalized.setdefault(retired_entry["provider"], []).append(m8)
        choice = runtime.resume_choice(record, lcat=lcat, effective=eff)
        if choice.needs_choice:
            needs_choice.append(m8)
        elif not sessions.lead_needs_choice(record, lcat):
            note = views.retired_lead_note(lcat, lead_key)
            if note is not None:
                attention.append(_per_session(f"session {m8}: {note}", mid, f"lead-note:{note}",
                                              f"{{n}} sessions: {note}"))
        for agent_id, binding in applied["agents"].items():
            key = binding.get("key")
            try:
                removed = lcat.resolve_key(key).key is None
            except catalog.CatalogError:
                removed = True
            if removed:
                what = f"{profile_mod.label(agent_id)} {key} was removed — unbound at the next resume"
                attention.append(_per_session(f"session {m8}: {what}", mid, f"removed-agent:{agent_id}:{key}",
                                              f"{{n}} sessions: {what}"))
        profile_name = record.get("profile")
        if (
            record.get("follow")
            and profile_name is not None
            and profile_names is not None
            and profile_name not in profile_names
        ):
            attention.append(
                f"session {m8} follows profile {profile_name!r}, which no longer exists; it "
                f"keeps its applied lineup — pin it (charmer lineup --session {rid} pin) "
                f"or choose one (charmer lineup --session {rid} profile <name>)"
            )
        if eff is not None:
            for line in settings_mod.drift(applied["settings"], eff):
                info.append(f"session {m8}: settings drift: {line}")
        pending = record.get("pending")
        if isinstance(pending, dict):
            if choice.problem:
                info.append(f"session {m8}: pending relaunch change is invalid ({choice.problem}); not applied")
            else:
                reasons = pending.get("reasons")
                text = ", ".join(reasons) if isinstance(reasons, list) and reasons else "relaunch-only"
                info.append(f"session {m8}: pending relaunch change ({text}); applies at the next resume")
                if choice.lineup is not None:
                    target = (f"profile {pending['profile']}" if pending.get("profile")
                              else f"direct {choice.lineup.lead.binding.key}")
                    pending_choices[target] = pending_choices.get(target, 0) + 1
        target = record.get("lead_target")
        if isinstance(target, dict):
            entry = lcat.lines.get(target.get("key")) or lcat.retired.get(target.get("key")) or {}
            display = (entry.get("display") if isinstance(entry, dict) else None) or target.get("key")
            info.append(
                f"session {m8}: requested lead {display} · {target.get('effort')} "
                "not switched yet (/model in the session, or the next resume)"
            )
    if needs_choice:
        head = (f"{len(needs_choice)} session(s) need a profile or model choice (lead removed): "
                "save without launching: charmer sessions choose --needs-choice --profile <name> "
                "(S banner), or resume interactively with charmer -r <id> — ")
        attention.append(gateway_facts.tagged(
            head + ", ".join(needs_choice), action="needs-choice", subjects=tuple(needs_choice),
            short=head + _listed(tuple(needs_choice)) if len(needs_choice) > SHOWN_IDS else None,
        ))
    for target, count in sorted(pending_choices.items()):
        info.append(f"{count} session(s) switch to {target} at their next resume")
    for provider_id, ids in sorted(externalized.items()):
        attention.append(
            f"{len(ids)} session(s) lead on provider {provider_id}, which left the catalog: to keep it, "
            f"declare its reviewed sample (charmer providers add --preset {provider_id} --base-url <your "
            "server>) and admit its line, then choose it at the resume; otherwise choose another model — "
            + ", ".join(ids)
        )
    return attention, info


# key -> (reason, severity). Severity "attention"
# or "info"; a credential key also blocks `doctor --rotate-token`.
_RADAR_SETTINGS_KEYS: dict[str, tuple[Callable[[Any], bool], str, str]] = {
    "disableAllHooks": (
        lambda value: value is True,
        "disables every hook: session records, lineup notices and the /model fence stop "
        "working in charmer sessions",
        "attention",
    ),
    "maxEffortLevel": (
        lambda value: value is not None,
        "caps every session's effort below its lineup's (the profile effort is not applied)",
        "attention",
    ),
    "alwaysThinkingEnabled": (
        lambda value: value is False,
        "turns extended thinking off for every session (lead and agents)",
        "attention",
    ),
    "switchModelsOnFlag": (
        lambda value: value is False,
        "disables the refusal model fallback (switchModelsOnFlag: false)",
        "info",
    ),
    "disableSkillShellExecution": (
        lambda value: value is True,
        "disables skill shell execution; /cm cannot run",
        "info",
    ),
}


_RADAR_SUBAGENT_KEYS = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE")


def _radar_credential_keys() -> frozenset[str]:
    return frozenset(catalog.CREDENTIAL_ENV_KEYS) | {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}


@dataclass(frozen=True)
class SettingsRadar:
    """The settings radar: file + key names only, never values."""

    attention: tuple[str, ...]
    info: tuple[str, ...]
    credential_hits: tuple[str, ...]  # "<path>: env.<KEY>" — blocks --rotate-token
    subagent_paths: dict[str, tuple[str, ...]]  # key -> files that set it


def _read_settings_file(path: Path) -> Any:
    """Tolerant, bounded (≤ 1 MiB), symlink-following read; None when unreadable."""

    try:
        raw = hooks.read_settings_bytes(path, 1 << 20)
    except OSError:
        return None
    if raw is None or len(raw) > (1 << 20):
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _radar_settings_files(runtime: runtime_mod.Runtime, records: list[dict[str, Any]]) -> list[Path]:
    """User settings, then each cwd's project and local settings (deduplicated).

    The user layer is the managed client's ``$HOME/.claude``, never
    ``$CLAUDE_CONFIG_DIR``: managed sessions run without it, so a credential or
    policy key there never reaches them and one in HOME's file always does
    (the ``--rotate-token`` credential gate reads these hits)."""

    user = product_paths.claude_settings_dir(runtime.environ) / "settings.json"
    paths: list[Path] = [user]
    cwds: list[str] = [runtime.cwd]
    for record in records:
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd not in cwds:
            cwds.append(cwd)
    for cwd in cwds:
        for name in ("settings.json", "settings.local.json"):
            candidate = Path(cwd) / ".claude" / name
            if candidate not in paths:
                paths.append(candidate)
    return paths


def _settings_radar(runtime: runtime_mod.Runtime, records: list[dict[str, Any]]) -> SettingsRadar:
    """The settings radar over user + per-cwd project/local Claude settings (read-only)."""

    attention: list[str] = []
    info: list[str] = []
    credential_hits: list[str] = []
    subagent: dict[str, list[str]] = {key: [] for key in _RADAR_SUBAGENT_KEYS}
    policy_keys = frozenset(compiler.POLICY_DEFEATING_ENV_KEYS)
    credential_keys = _radar_credential_keys()
    for path in _radar_settings_files(runtime, records):
        document = _read_settings_file(path)
        if not isinstance(document, dict):
            continue
        env = document.get("env")
        if isinstance(env, dict):
            for key in sorted(env):
                if key in credential_keys and env[key] not in (None, ""):
                    credential_hits.append(f"{path}: env.{key}")
                    attention.append(
                        f"{path}: env.{key} sets a gateway credential; it outranks the "
                        "session's apiKeyHelper (token rotation would break those sessions) "
                        "— remove it (charmer doctor --rotate-token refuses while it is set)"
                    )
                elif key in policy_keys and env[key] not in (None, ""):
                    attention.append(
                        f"{path}: env.{key} defeats the charmer session policy (claude "
                        "merges it after the launcher's env cleanup) — remove it"
                    )
                elif key in subagent and env[key]:
                    subagent[key].append(str(path))
        for key, (predicate, reason, severity) in _RADAR_SETTINGS_KEYS.items():
            if key in document and predicate(document[key]):
                line = f"{path}: {key} {reason}"
                (attention if severity == "attention" else info).append(line)
    # Names only, never values.
    docs = runtime.ordinary_docs
    names = sorted(
        {
            provider["transport"]["auth"]["secret_ref"].removeprefix("env:")
            for provider in docs["providers"]["providers"].values()
            if isinstance(provider.get("transport", {}).get("auth"), dict)
            and str(provider["transport"]["auth"].get("secret_ref", "")).startswith("env:")
        }
    )
    present = [name for name in names if runtime.environ.get(name)]
    if present:
        info.append(gateway_facts.Finding(
            f"{len(present)} provider secret name(s) present in the launcher env "
            f"({', '.join(present)}); sessions unset them and the gateway reads the secret "
            "file, so nothing needs them exported — stop exporting them where your environment "
            "sets them to keep them from other programs too",
            code="provider-secret-exported",
            public="provider secret names are set in the launcher environment; managed sessions unset them",
        ))
    return SettingsRadar(
        attention=tuple(attention),
        info=tuple(info),
        credential_hits=tuple(credential_hits),
        subagent_paths={key: tuple(paths) for key, paths in subagent.items()},
    )


def _charmer_selector_set(runtime: runtime_mod.Runtime) -> frozenset[str]:
    """Normalised selectors of every non-Anthropic catalog/custom/retired
    line (§13.7), and the earlier names of the renamed ones."""

    docs = runtime.ordinary_docs
    lines = (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]
    selectors: set[str] = set()
    for entry in lines.values():
        if not isinstance(entry, dict) or entry.get("provider") == "anthropic":
            continue
        try:
            selectors.update(selector for _effort, selector, _contract in catalog.line_selectors(entry))
        except (KeyError, TypeError, AttributeError):
            continue
    for entry in runtime.catalog.retired.values():
        if isinstance(entry, dict) and entry.get("provider") != "anthropic":
            selectors.update(entry.get("selectors", {}))
    selectors.update(runtime.catalog.renamed)
    return frozenset(scope_mod.normalize_model(selector) for selector in selectors)


def _is_charmer_selector(value: Any, known: frozenset[str]) -> bool:
    if not isinstance(value, str) or not value:
        return False
    norm = scope_mod.normalize_model(value)
    # A /model choice inside an older session may have saved a selector under
    # the prefix catalog 39 renamed.
    return norm in known or norm.startswith(("charmer-", "gpt-multi-", legacy.LEGACY_SELECTOR_PREFIX))


def _doctor_user_settings_report(
    runtime: runtime_mod.Runtime, records: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """charmer selectors saved in user settings + /cm skill collisions."""

    attention: list[str] = []
    info: list[str] = []
    user_dir = product_paths.claude_settings_dir(runtime.environ)
    path = user_dir / "settings.json"
    document = _read_settings_file(path)
    if isinstance(document, dict):
        for name, effect in (("statusLine", "user-owned display"),
                             ("cleanupPeriodDays", "client cleanup policy"),
                             ("promptCacheTtl", "client cache preference; gateway retention policy still applies")):
            if name in document:
                info.append(f"user setting {name} present ({effect}); effective value/precedence unknown")
    # Presence only (the user's, this directory's project and local layers); never the command.
    layers = runtime.subagent_status_line_layers(str(runtime.cwd))
    if layers.defined:
        info.append("subagent status line: user-owned (subagentStatusLine in settings.json; charmer compiles none)")
    elif layers.unreadable:
        where = "; ".join(f"{product_paths.display(layer, runtime.environ)} {reason}"
                          for layer, reason in layers.unreadable)
        info.append(f"subagent status line: a settings layer could not be read ({where}); charmer's subagent "
                    "status line was not compiled — fix or remove that file, then it applies at the next resume")
    else:
        info.append("subagent status line: charmer (compiled into managed sessions)")
    if isinstance(document, dict):
        known = _charmer_selector_set(runtime)
        hits: list[tuple[str, Any]] = []
        if _is_charmer_selector(document.get("model"), known):
            hits.append(("model", document["model"]))
        model_settings = document.get("modelSettings")
        if isinstance(model_settings, dict):
            for key in sorted(model_settings):
                if _is_charmer_selector(key, known):
                    hits.append((f"modelSettings[{json.dumps(key)}]", key))
        for json_path, value in hits:
            if str(value).startswith(legacy.LEGACY_SELECTOR_PREFIX):
                held = f"holds the {legacy.NAME} selector {value!r} (saved by a /model choice inside a {legacy.NAME} session)"
            else:
                held = f"holds the charmer selector {value!r} (saved by a /model choice inside a charmer session)"
            attention.append(
                f"{path}: {json_path} {held}; plain claude cannot use it. Revert: remove {json_path} from {path}"
            )
    cwds = [runtime.cwd] + [
        record["cwd"]
        for record in records
        if isinstance(record.get("cwd"), str) and record["cwd"] != runtime.cwd
    ]
    reported: set[str] = set()
    for cwd in cwds:
        try:
            hits_found = scope_mod.find_skill_collisions(
                cwd, (), user_skills_dir=user_dir / "skills"
            )
        except (OSError, ValueError):
            continue
        for hit_path, _name in hits_found:
            if str(hit_path) in reported:
                continue
            reported.add(str(hit_path))
            attention.append(f"a skill named cm at {hit_path} may shadow the session's /cm skill")
    return attention, info


_RETIREMENT_WINDOW_DAYS = 30


def _doctor_retirement_radar(runtime: runtime_mod.Runtime) -> list[str]:
    """Upstream retirement dates within 30 days (Attention, never BLOCK).

    Reads the pinned registry's codex metadata for every merged
    catalog/custom line's wire and every continuity alias's wire. The date is
    upstream's; charmer only infers that the wire may be refused after it.
    """

    root = catalog.registry_dir(runtime.environ, runtime.asset_root)
    if root is None:
        return []
    try:
        registry = catalog.load_pinned_registry(root)
    except catalog.CatalogError as exc:
        return [f"pinned registry unreadable ({exc}); the retirement radar did not run"]
    now = gateway_facts._doctor_now()
    horizon = _RETIREMENT_WINDOW_DAYS * 86400

    def check(subject: str, wire: Any) -> str | None:
        meta = registry.codex.get(wire) if isinstance(wire, str) else None
        if not meta or meta.get("retirement_at") is None:
            return None
        when = session_facts._parse_registry_instant(meta["retirement_at"])
        if when is None:
            return None
        delta = (when - now).total_seconds()
        if delta > horizon:
            return None
        days = int(abs(delta) // 86400)
        timing = f"in {days} day(s)" if delta >= 0 else f"{days} day(s) ago"
        upgrade = meta.get("upgrade")
        suggestion = f"; upstream names {upgrade} as its upgrade" if upgrade else ""
        return (
            f"{subject}: wire {wire} has an upstream retirement date {meta['retirement_at']} "
            f"({timing}; inferred from the pinned registry — upstream may refuse it after)"
            f"{suggestion}"
        )

    lines_out: list[str] = []
    docs = runtime.ordinary_docs
    lines = (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]
    lcat = profile_mod.LineupCatalog.from_docs(docs)
    for key in sorted(lines):
        entry = lines[key]
        if not isinstance(entry, dict):
            continue
        # Explicit origin (legacy-custom keeps its "custom" label).
        origin = lcat.origin(key)
        source = "custom" if origin == "legacy-custom" else origin
        line = check(f"line {key} ({source})", entry.get("wire_model"))
        if line is not None:
            lines_out.append(line)
    aliases, _status, _root = gateway_facts._doctor_continuity(runtime)
    for alias in sorted(aliases):
        entry = aliases[alias]
        wire = entry.get("wire") if isinstance(entry, dict) else None
        line = check(f"continuity alias {alias}", wire)
        if line is not None:
            lines_out.append(line)
    return lines_out


def _doctor_quota_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str], frozenset[str]]:
    pool = runtime.pool_status()
    # Local key state is reported once, outside the readiness branch, so it
    # also explains quota being off while the gateway cannot start.
    if pool.state in {"no-key", "disabled", "key-unusable", "pending", "unavailable"}:
        return [], [], frozenset()
    return quota.doctor_report(
        pool, provider_by_pool=runtime.pool_providers(), login_commands=gateway_facts._OAUTH_LOGIN_COMMANDS,
        restart_hint=gateway_facts.gateway_service_hint("restart"), now=quota._now(),
        backend=_health_backend(runtime),
    )


def _doctor_journal_report(runtime: runtime_mod.Runtime, text: gateway_events.LogWindow | None = None, *,
                           refresh_failures: frozenset[str] = frozenset(),
                           info_lines: list[str] | None = None) -> list[str]:
    """Dead OAuth refresh and per-model 429/402/403 counts (Attention).

    The facts come from :func:`_journal_facts`; this renders them.
    Save outcomes go to Info, failures to Attention; coverage is explicit.
    Counts/validated metadata only; no body or message text is ever printed.
    """

    facts = (gateway_facts.report_journal_facts(runtime) if text is None else
             gateway_facts._journal_facts(runtime, text))
    lines: list[str] = []
    saves = facts.credential_saves
    if info_lines is not None:
        coverage = saves.coverage
        info_lines.append(
            f"gateway credential saves: coverage={coverage}; last 24 h, bounded observation "
            "only — not backup-retirement evidence"
        )
    # Latest outcome AND latest failure per identity within each invocation.
    # A later success never silently hides a persistence failure in this window;
    # it only changes the remedy.
    def repairs_failure(failed: gateway_events.CredentialSave, event: gateway_events.CredentialSave) -> bool:
        # Repair evidence: persisted, or unverified with credentials_changed=true
        # that no newer outcome superseded, from a known instance and auth index, at
        # the failed save's epoch/generation or newer. It changes only the remedy;
        # backup retirement still needs persisted receipts (the runbook).
        if event.gateway_instance is None or event.auth_index == "invalid":
            return False
        if (event.epoch, event.generation) < (failed.epoch, failed.generation):
            return False
        return event.result == "persisted" or (
            event.result == "unverified" and event.credentials_changed and event.category != "superseded")

    latest = {}
    failures = {}
    repairs = {}
    for event in saves.events:
        identity = (event.gateway_instance, event.provider, event.auth_index)
        latest[identity] = event
        if event.result == "failed":
            failures[identity] = event
            repairs.pop(identity, None)
        elif identity in failures and repairs_failure(failures[identity], event):
            repairs[identity] = event
    for event in dict.fromkeys([*latest.values(), *failures.values()]):
        line = (
            f"gateway credential save: {event.result} provider={event.provider} "
            f"auth_index={event.auth_index} bytes={event.bytes}/{event.size} errno={event.errno} "
            f"stage={event.stage} category={event.category} operation={event.operation} "
            f"credentials_changed={'true' if event.credentials_changed else 'false'}"
        )
        identity = (event.gateway_instance, event.provider, event.auth_index)
        if event.result == "failed" and identity in repairs:
            if info_lines is not None:
                repair = repairs[identity]
                evidence = ("a persisted save" if repair.result == "persisted" else
                            "an unverified save with credentials_changed=true (repair evidence, "
                            "not a verified save)")
                info_lines.append(line + f"; followed by {evidence} of this credential in the same "
                                  "gateway instance — no re-authentication needed; retain auth.pre-* backups")
        elif event.result == "failed":
            remedy = gateway_facts._OAUTH_LOGIN_COMMANDS.get(event.provider) or "the provider's login command"
            lines.append(gateway_facts.tagged(
                line + "; keep the gateway running, free space, then wait for a persisted save "
                f"of this credential; re-authenticate with {remedy} only if none appears, "
                "before any restart or rollback; retain auth.pre-* backups", area="Gateway"))
        elif info_lines is not None:
            info_lines.append(line + ("; verified save observed" if event.result == "persisted"
                                      else "; not a verified save"))
    qualifier = f"; {facts.partial}" if facts.partial else ""
    for pool in sorted(facts.dead_pools):
        if pool in refresh_failures:
            continue
        count, last = facts.dead_pools[pool]
        lines.append(
            f"gateway journal: {count} invalid_grant line(s) since the {pool} pool "
            f"credential was written (last {last:%Y-%m-%d %H:%M}{qualifier}); the refresh token "
            f"is dead — sign in again: {gateway_facts.sign_in_command(pool)}"
        )
    if facts.unattributed is not None:
        count, last, possible = facts.unattributed
        commands = " or ".join(gateway_facts.sign_in_command(n) for n in possible)
        lines.append(
            f"gateway journal: {count} invalid_grant line(s) naming no pool "
            f"(last {last:%Y-%m-%d %H:%M}{qualifier}); one OAuth refresh token is dead — "
            f"candidates: {', '.join(possible)}; sign in again to the failing one: {commands}"
        )
    for (model, code), count in facts.quota.items():
        window = "in the last hour" if code == "429" else "in the last 24 h" + (
            f" ({facts.partial})" if facts.partial else "")
        what = {"429": "rate limit / quota", "402": "payment required", "403": "forbidden"}[code]
        lines.append(
            f"gateway journal: model {model} got {count}× {code} ({what}) {window} — "
            "quota pressure on its provider; " + quota.guidance_action("doctor")
            + (" — a session may sit silently in a Retry-After wait of up to 6 h (watchdog); "
               "a foreground agent then blocks its lead" if code == "429" else "")
        )
    for substitution in facts.substitutions:
        lines.append(gateway_facts.tagged(
            f"gateway substitution: {substitution.label} was answered by {substitution.served} "
            f"({substitution.count}× in 24 h{qualifier}, logged per attempt); "
            "the provider serves another model under this wire — "
            "check the line (charmer models) and move agents off it if that matters", area="Models"))
    return lines


def _last_launch_time(runtime: runtime_mod.Runtime, records: list[dict[str, Any]]) -> float | None:
    """The newest scope ``settings.json`` mtime (written by every launch)."""

    newest: float | None = None
    for record in records:
        if record.get("version") != sessions.RECORD_VERSION:
            continue
        path = scope_mod.scope_dir(runtime.session_store.root, sessions.managed_id(record))
        try:
            mtime = (path / "settings.json").stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return newest


HOOK_LOG_UNAVAILABLE = ("hook-error log unreadable/not a regular file; hook failures since the last launch are unknown; "
                        "inspect and remove or replace the path by hand")


def _hook_error_observation(runtime, records):
    def collect():
        path = runtime.session_store.root / hooks.HOOK_ERRORS_LOG
        def fact(status, count, reason, classification="info", coverage="partial"):
            return observations.Fact("hook-errors", "source", "hook-errors", status, count,
                                     source="hook-error-log", coverage=coverage, reason=reason,
                                     classification=classification)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return fact("known", 0, "absent", coverage="absent"), []
        except OSError:
            return fact("unavailable", None, "unreadable", "attention"), [HOOK_LOG_UNAVAILABLE]
        try:
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not regular")
            raw = lineup_log.read_regular(path, max_bytes=hooks.HOOK_ERRORS_MAX_BYTES * 2)
        except OSError:
            return fact("unavailable", None, "unreadable", "attention"), [HOOK_LOG_UNAVAILABLE]
        since = _last_launch_time(runtime, records)
        count, events = 0, {}
        damaged = False
        for line in raw.splitlines(keepends=True):
            try:
                entry = json.loads(line)
                when = session_facts._parse_registry_instant(entry.get("time")) if isinstance(entry, dict) else None
                if when is None or not line.endswith(b"\n"):
                    damaged = True
                    continue
                if since is not None and when.timestamp() < since:
                    continue
                count += 1
                event = entry.get("event")
                event = event if isinstance(event, str) and event in {"start", "end", "prompt", "premodel", "postmodel", "subagent"} else "unknown"
                events[event] = events.get(event, 0) + 1
            except (ValueError, TypeError, UnicodeError):
                damaged = True
        lines = []
        if count:
            detail = ", ".join(f"{event} {n}" for event, n in sorted(events.items()))
            lines.append(gateway_facts.tagged(
                f"{count} protocol-3 hook failure(s) logged since the last launch ({detail}) in {path} "
                "(metadata only); charmer doctor --prune rotates the log once read", action="prune"))
        return fact("known", count, "malformed-lines" if damaged else "observed",
                    "attention" if count else "info", coverage="partial"), lines
    return runtime.report_value("hook-errors", collect)


def _doctor_hook_errors(runtime: runtime_mod.Runtime, records: list[dict[str, Any]]) -> list[str]:
    return _hook_error_observation(runtime, records)[1]


def _session_liveness(runtime: runtime_mod.Runtime) -> tuple[Any, Any]:
    """Daemon and process-table liveness, each with its uncertainty (the
    forms ``sessions mark-ended`` and the prune use), read once per report."""

    return runtime.report_value("session-liveness", lambda: (
        runtime.background_liveness(), sessions.proc_session_scan(runtime.proc_root)))


DEAD_SESSIONS_SUMMARY = ("{n} sessions do not run and have no recorded end (each exited without one), so commands "
                         "that refuse while a session may run count them: charmer sessions mark-ended --all-dead")


def dead_session_ids(records: list[dict[str, Any]], liveness: tuple[Any, Any]) -> list[str]:
    """The managed ids of the sessions without a recorded end that no check
    finds running (the ones ``sessions mark-ended --all-dead`` marks); none
    while whether sessions run cannot be told."""

    daemon, scan = liveness
    if not daemon.known or not scan.known:
        return []
    dead: list[str] = []
    for record in records:
        if record.get("last_event_source") == "end":
            continue
        try:
            mid = sessions.managed_id(record)
            if session_facts._record_view_is_live(record, prefixes=daemon.prefixes, proc_ids=scan.ids):
                continue
        except (KeyError, TypeError, sessions.SessionError):
            continue
        dead.append(mid)
    return dead


def _doctor_dead_sessions(records: list[dict[str, Any]], liveness: tuple[Any, Any]) -> list[str]:
    """One line per session :func:`dead_session_ids` names, collapsed into one row."""

    return [_per_session(
        f"session {mid[:8]}: does not run and has no recorded end (it exited without one), so commands that "
        f"refuse while a session may run count it: charmer sessions mark-ended {mid}",
        mid, "dead", DEAD_SESSIONS_SUMMARY, action="dead-sessions") for mid in dead_session_ids(records, liveness)]


# The kinds of generated file the cleanup preview names, as doctor counts them.
LEFTOVER_KINDS = (("scope", "scope"), ("prompt", "lead prompt"), ("notice", "notice marker"),
                  ("lineup-log", "lineup log"))
LEFTOVERS_REMEDY = "charmer doctor --prune --preview, then charmer doctor --prune"


@dataclass(frozen=True)
class Leftovers:
    """The generated files ``doctor --prune --preview`` names, each
    ``(path, kind, reason)`` (``LEFTOVER_KINDS``); ``protected`` is the line
    the preview prints instead when ownership or metadata cannot be told."""

    candidates: tuple[tuple[Path, str, str], ...] = ()
    protected: str | None = None


def prune_candidates(runtime: runtime_mod.Runtime, *, liveness: tuple[Any, Any] | None = None) -> Leftovers:
    """The cleanup preview's inventory: recognized charmer-generated files
    of forgotten records (scopes and staging, lead prompts, notice markers,
    lineup logs) and the superseded lead prompts of ended, process-absent
    records. Metadata only: no lock, chmod, directory creation or I/O
    outside generated state; existing records are protected, not garbage.
    ``liveness`` is :func:`_session_liveness`'s pair (read here when None)."""

    root = runtime.session_store.root
    try:
        sessions.check_state_marker(root)
        # Resolve no symlinked directory: even a listing must not wander into a
        # transcript-bearing or backup root supplied as a generated directory.
        for directory in (root, root / "sessions", root / "scopes", root / "notice", root / "lineup-log"):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise state.StateError("unsafe generated-state directory")
        records, problems, ids = session_facts._session_record_scan(runtime)
        if problems:
            return Leftovers(protected="Protected: unreadable records; ownership is unknown.")
        runtime_ids = {rid for record in records for rid in sessions.record_session_ids(record)}
        candidates: list[tuple[Path, str, str]] = []
        scopes = root / "scopes"
        if scopes.exists():
            for entry in sorted(scopes.iterdir()):
                name = entry.name
                mid = name
                for suffix in (".new", ".prev"):
                    if name.startswith(".") and name.endswith(suffix):
                        mid = name[1:-len(suffix)]
                if sessions.UUID4.fullmatch(mid) and mid not in ids and not entry.is_symlink():
                    candidates.append((entry, "scope", "scope/staging for forgotten record; recheck at commit"))
        for entry in sorted(root.glob("lead-prompt-*.md")):
            stem = entry.name.removeprefix("lead-prompt-").removesuffix(".md")
            mid = stem[-36:]
            digest = stem[:-37] if sessions.UUID4.fullmatch(mid) else stem
            if len(digest) == 16 and all(c in "0123456789abcdef" for c in digest):
                if "-" not in stem or mid not in ids:
                    candidates.append((entry, "prompt", "obsolete generated lead prompt"))
        # Superseded prompts are candidates only for ended, process-absent
        # records with a readable fence. This is a lock-free preview; the
        # action revalidates under runtime-index -> lifecycle before unlink.
        if records:
            daemon, scan = liveness if liveness is not None else (
                runtime.background_liveness(), sessions.proc_session_scan(runtime.proc_root))
            unreadable = set(continuity_mod.scan_records(root).fence_unreadable)
            if daemon.known and scan.known:
                for record in records:
                    mid = sessions.managed_id(record)
                    if (record.get("last_event_source") != "end" or mid in unreadable or
                            session_facts._record_view_is_live(record, prefixes=daemon.prefixes, proc_ids=scan.ids)):
                        continue
                    prompts = sorted((p for p in root.glob(f"lead-prompt-*-{mid}.md") if not p.is_symlink()),
                                     key=lambda p: (p.stat().st_mtime, p.name))
                    candidates.extend((p, "prompt", "superseded generated lead prompt; recheck at commit")
                                      for p in prompts[:-1])
        notice = root / "notice"
        if notice.exists():
            for entry in sorted(notice.iterdir()):
                rid = entry.name.removesuffix(".seen")
                if entry.name.endswith(".seen") and sessions.UUID4.fullmatch(rid) and rid not in runtime_ids:
                    candidates.append((entry, "notice", "unknown runtime notice; recheck at commit"))
        logs = root / "lineup-log"
        if logs.exists():
            for entry in sorted(logs.iterdir()):
                mid = entry.name.split(".log", 1)[0]
                if entry.name in {mid + ".log", mid + ".log.1"} and sessions.UUID4.fullmatch(mid) and mid not in ids:
                    candidates.append((entry, "lineup-log", "forgotten session lineup log"))
        return Leftovers(tuple(item for item in candidates if not item[0].is_symlink()))
    except (OSError, state.StateError, sessions.SessionError):
        return Leftovers(protected="Protected: metadata unavailable or unsafe; no cleanup inferred.")


def _doctor_leftovers(runtime: runtime_mod.Runtime, liveness: tuple[Any, Any]) -> list[str]:
    """One line for the generated files the cleanup preview names, by kind
    (none while ownership or metadata cannot be told: the record and state
    checks report that)."""

    found = prune_candidates(runtime, liveness=liveness)
    if not found.candidates:
        return []
    counts: dict[str, int] = {}
    for _path, kind, _reason in found.candidates:
        counts[kind] = counts.get(kind, 0) + 1
    detail = ", ".join(_plural(counts[kind], noun) for kind, noun in LEFTOVER_KINDS if kind in counts)
    total = len(found.candidates)
    head, them = ((f"{total} generated files left by forgotten or ended sessions", "them") if total != 1 else
                  ("1 generated file left by a forgotten or ended session", "it"))
    text = (f"{head} ({detail}): charmer doctor --prune --preview lists {them}, charmer doctor --prune removes "
            f"{them} (records, transcripts and backups are never among them)")
    return [gateway_facts.tagged(gateway_facts.Finding(text, code="generated-leftovers", public=text,
                                                       remedy=LEFTOVERS_REMEDY),
                                 area=SESSIONS, action="prune")]


def doctor_report(runtime, lines):
    """JSON severity includes every old producer until it becomes a typed fact."""
    problems, info, attention = lines
    diagnostics = tuple(_diagnostic(level, text)
                        for level, values in (("block", problems), ("attention", attention), ("info", info))
                        for text in values)
    try:
        sessions.check_state_marker(runtime.session_store.root)
    except sessions.StateMarkerError:
        return observations.Report("doctor", gateway_facts._doctor_now(),
                                   {"records": "unavailable", "journal": "unavailable", "management": "unavailable"},
                                   diagnostics=diagnostics)
    records, unreadable, _ids = session_facts._session_record_scan(runtime)
    hook = _hook_error_observation(runtime, records)[0]
    facts = [hook]
    # Presence only; commands and user-owned values never enter a report.
    user = _read_settings_file(product_paths.claude_settings_dir(runtime.environ) / "settings.json")
    if isinstance(user, dict):
        for name in ("statusLine", "cleanupPeriodDays", "promptCacheTtl"):
            if name in user:
                facts.append(observations.Fact("user-setting-present", "setting", name, "known", True,
                             source="user-settings", reason="effective-precedence-unknown"))
    events = runtime._report_cache.get(("journal", "-24h")) if runtime._report_cache is not None else None
    pool = runtime._pool_cache[1] if runtime._pool_cache is not None else None
    return observations.Report("doctor", gateway_facts._doctor_now(),
               {"records": "partial" if unreadable else "complete",
                "journal": events[0].coverage if events else "unavailable",
                "management": pool.state if pool else "unavailable"}, tuple(facts), diagnostics)


def _management_key_attention(runtime: runtime_mod.Runtime) -> tuple[str | None, str | None]:
    """Optional management state is never a launch block; never print key bytes.

    Whether management can exist is the release channel's policy, whatever
    the gateway's backend; the restart remedy follows the backend."""
    restart = gateway_facts.gateway_service_hint("restart", runtime)
    backend = _health_backend(runtime)
    if not management.management_channel(runtime.environ):
        # Key files left by another install change nothing here: no remedy.
        return None, quota.state_text(quota.PoolStatus("unavailable"), restart_hint=restart, backend=backend)
    key = management.key_state(runtime.home)
    if key.disabled:
        return None, quota.state_text(quota.PoolStatus("disabled"), restart_hint=restart, backend=backend)
    if key.active == "unusable":
        pool = quota.PoolStatus("key-unusable", key_problem=key.unusable_reason)
        try:
            management.read_active(runtime.home)
        except management.ManagementKeyError as exc:
            pool = quota.PoolStatus("key-unusable", key_problem=exc.reason, remedy=exc.remedy,
                                    key_file=exc.filename)
        return quota.state_text(pool, restart_hint=restart, backend=backend), None
    if key.staged:
        # A corrupt staged slot never promotes; a restart alone cannot clear it.
        try:
            management.check_staged(runtime.home)
        except management.ManagementKeyError as exc:
            pool = quota.PoolStatus("key-unusable", key_problem=exc.reason, remedy=exc.remedy,
                                    key_file=exc.filename)
            return quota.state_text(pool, restart_hint=restart, backend=backend), None
        since = (datetime.fromtimestamp(key.staged_since, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                 if key.staged_since is not None else "an unknown time")
        return (f"management key rotation staged since {since}: "
                f"restart the gateway to apply it: {restart}"), None
    if key.pending:
        return quota.state_text(quota.PoolStatus("pending"), restart_hint=restart, backend=backend), None
    if key.active == "absent":
        return None, quota.state_text(quota.PoolStatus("no-key"), restart_hint=restart, backend=backend)
    return None, None


def _token_rotation_attention(runtime: runtime_mod.Runtime) -> str | None:
    """Attention while a token rotation's previous-key slot exists.

    A dual-key window is by-design state (the gateway accepts both keys and
    doctor's drift check renders the same slots), never damage — but it is
    unfinished work with one exact command. Names and mtime only.
    """

    previous = proxy_mod.config_dir(runtime.home) / "previous-key"
    try:
        info = previous.lstat()
    except OSError:
        return None
    since = datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    try:
        published = len(proxy_mod.gateway_api_keys(runtime.home)) == 2
    except proxy_mod.ProxyError:
        published = None  # the served report BLOCKs on unusable slots
    phase = {
        True: "dual-key window open",
        False: "interrupted before publication; the current key is unchanged",
        None: "key slots unusable",
    }[published]
    return (
        f"gateway token rotation in progress ({phase}; previous-key since "
        f"{since}): finish with `charmer doctor --rotate-token`"
    )


_AGENT_DRIFT = re.compile(r"(\.claude/agents/(cm-[a-z-]+)\.md) content differs")


def _agent_frontmatter(data: bytes) -> dict[str, str] | None:
    """The frontmatter of a generated agent file, or None when it is not one.

    The whole block must match the generated grammar (strict UTF-8, ``---``
    fences followed by the blank line, the known keys in the writer's order
    and each once, every value in its field's syntax, the description a
    well-formed double-quoted scalar ending in the managed sentinel): any
    other YAML (an unterminated quote, a flow collection, an unknown or
    duplicate key) is not a generated file and stays BLOCK."""

    # The writer's grammar (scope._agent_file_v2_bytes): these keys, in this
    # order, each once; the optional two only after the required four.
    keys = ("name", "description", "model", "effort", "isolation", "disallowedTools")
    required = 4
    syntax = {
        "name": r"cm-[a-z]+(?:-[a-z]+)*",
        # A YAML double-quoted scalar with only the two escapes the writer emits.
        "description": r'"(?:[^"\\\x00-\x1f\x7f-\x84\x86-\x9f\ufffe\uffff]|\\[\\"])*"',
        "model": r"[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]*",
        "effort": r"[a-z]+",
        "isolation": r"[a-z]+",
        "disallowedTools": r"[A-Za-z][A-Za-z0-9_]*(?:, [A-Za-z][A-Za-z0-9_]*)*",
    }

    try:
        lines = data.decode("utf-8").split("\n")
    except UnicodeDecodeError:
        return None
    if len(lines) < 3 or lines[0] != "---":
        return None
    fields: dict[str, str] = {}
    position = 0
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            if position < required or index + 1 >= len(lines) or lines[index + 1] != "":
                return None
            return fields
        key, sep, value = line.partition(": ")
        if not sep or key not in keys[position:]:
            return None
        order = keys.index(key)
        if order >= required and position < required:
            return None  # an optional key before every required one
        if order != position and order < required:
            return None  # a required key skipped or out of order
        if re.fullmatch(syntax[key], value) is None:
            return None
        if key == "description":
            decoded = re.sub(r"\\(.)", r"\1", value[1:-1])
            if not decoded.endswith(" " + scope_mod.SENTINEL_SUFFIX):
                return None
        fields[key] = value
        position = order + 1
    return None


def _catalog_agent_drift(
    runtime: runtime_mod.Runtime, record: Mapping[str, Any], live: Path, drifted: list[str]
) -> list[str]:
    """The drifted agent files when
    the ONLY drift is agent-file content, the installed catalog differs from
    the one the record launched under (its catalog hash), and every drifted
    file parses as a cm-* agent file whose name, model and effort match the
    record's binding. Anything else (another file, a mode, a missing or
    unexpected file, a malformed or rebound agent file, an unchanged
    catalog) returns [] and stays BLOCK: that is corruption, not lazy state."""

    if not drifted or record.get("catalog_hash") == runtime.catalog.bundle_sha256:
        return []
    agents = (record.get("applied") or {}).get("agents") or {}
    files: list[str] = []
    for line in drifted:
        match = _AGENT_DRIFT.fullmatch(line)
        if match is None:
            return []
        relpath, name = match.group(1), match.group(2)
        binding = agents.get(name)
        if not isinstance(binding, Mapping):
            return []
        try:
            fields = _agent_frontmatter(state.read_private(live / relpath))
        except (OSError, state.StateError):
            return []
        if fields is None or (fields.get("name"), fields.get("model"), fields.get("effort")) != (
                name, binding.get("selector"), binding.get("effort")):
            return []
        files.append(name)
    return files


def _check_scope_integrity(
    runtime: runtime_mod.Runtime, record: dict[str, Any], *,
    plans: dict[str, ExpectedPlan | str] | None = None, earlier_hooks: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """A v4 gen >= 1 scope vs the record-authoritative compile (read-only).

    Returns ``(problems, attention, info)``. Any compile failure is an
    Attention line: doctor never crashes on a record.
    """

    mid = sessions.managed_id(record)
    m8 = mid[:8]
    live = scope_mod.scope_dir(runtime.session_store.root, mid)
    earlier = managed.read_settings(live / "settings.json") if live.is_dir() else None
    earlier_hooks = earlier_hooks or bool(earlier and _compiled_by_earlier_release(earlier, runtime.environ))
    transition = runtime_mod._transition_module()
    try:
        ep = plans.get(mid) if plans is not None else None
        if ep is None:
            parts = runtime.converge_parts()
            ep = transition.expected_plan(
                record,
                docs=parts.docs,
                prompt_bodies=parts.prompt_bodies,
                state_root=runtime.session_store.root,
                hook_command=parts.hook_command,
                token_helper_command=parts.token_helper_command,
                live=live if os.path.lexists(live) else None,
                worktree_available=parts.worktree_available(record["cwd"]) if parts.worktree_available else None,
                environ=parts.environ,
                managed_root=parts.managed_root,
                feedback_drafts=parts.feedback_drafts,
            )
    except (
        transition.TransitionError,
        profile_mod.ProfileError,
        compiler.CompilerError,
        scope_mod.ScopeError,
        catalog.CatalogError,
        settings_mod.SettingsError,
        KeyError,
    ) as exc:
        ep = str(exc)
    if plans is not None:
        plans[mid] = ep
    if isinstance(ep, str):
        return [], [f"session {m8}: scope not checked ({ep})"], []
    if earlier_hooks:
        # Earlier hooks are lazy state, but a moved or unevaluable binding
        # cannot be rewritten by repair-all even while the session is stopped.
        action = "earlier-scopes"
        remedy = "charmer doctor --repair-all"
        fix = cli_text.EARLIER_SCOPES_FIX
        detail = f"{fix} (charmer -r {mid})"
        group = EARLIER_SCOPES
        summary = _EARLIER_SCOPES_SUMMARY
        if ep.plan is None:
            if runtime.resume_choice(record).needs_choice:
                action = "needs-choice"
                remedy = "charmer sessions choose --needs-choice --profile <name>"
                fix = cli_text.EARLIER_SCOPES_CHOICE_FIX
                detail = f"{fix} (charmer -r {mid})"
                group = ("session", "earlier-scope-choice")
            else:
                action = None
                remedy = f"charmer -r {mid}"
                fix = cli_text.EARLIER_SCOPES_DEFERRED_FIX
                detail = f"repair-all defers it: {ep.reason}"
                group = ("session", "earlier-scope-deferred")
            summary = f"{{n}} session scope(s) were compiled by {legacy.NAME}: {fix}"
        line = gateway_facts.Finding(
            f"session {m8}: scope compiled by {legacy.NAME} before the move to charmer "
            f"(stale; its hooks name the earlier shims); {detail}",
            code="earlier-scopes", public=f"session scope compiled by charmer's earlier name: {fix}", remedy=remedy)
        return [], [gateway_facts.tagged(line, area="Sessions", action=action, group=group,
                                         summary=summary, subjects=(m8,))], []
    if ep.plan is None:
        return [], [f"session {m8}: {ep.reason}"], []
    problems: list[str] = []
    attention: list[str] = []
    info: list[str] = []

    def repair(text: str) -> gateway_facts.Line:
        return gateway_facts.tagged(text, area="Sessions", action="scope-repair", subjects=(m8,))

    def lazy(text: str, kind: str, summary: str, *, repairable: bool = False) -> gateway_facts.Line:
        return _per_session(text, mid, kind, summary, action="scope-repair" if repairable else None)

    default_mode = (cli_text.DEFAULT_MODE_DRIFT.format(m8=m8, mid=mid), "default-mode", cli_text.DEFAULT_MODE_DRIFTS)
    status_line = (
        f"session {m8}: its subagent status line differs from this release's compile (a scope another release "
        "compiled, or your own settings since launch: a subagentStatusLine added or removed, or a layer that "
        f"cannot be read; lazy state); applies at the next resume (charmer -r {mid}) — or charmer doctor "
        f"--repair {mid} now",
        "status-line",
        "{n} sessions: the subagent status line differs from this release's compile (lazy state); each applies "
        "at its next resume — or charmer doctor --repair-all now (stopped sessions)",
    )

    def lazy_alongside() -> None:
        if scope_mod.default_mode_differs(live, ep.plan):
            attention.append(lazy(*default_mode))
        if scope_mod.status_line_differs(live, ep.plan):
            attention.append(lazy(*status_line, repairable=True))

    disk = scope_mod.read_disk_plan(live, ep.plan) if live.is_dir() else None
    # Expected content alone does not prove the tree's structure or modes.
    try:
        drifted = scope_mod.live_drift(live, ep.plan) if disk is not None else []
    except OSError:
        # A concurrent resume can swap the scope between the read and walk.
        disk = None
        drifted = []
    if disk is None:
        problems.append(repair(f"session {m8}: scope unreadable; charmer doctor --repair {mid}"))
    elif drifted:
        agent_files = _catalog_agent_drift(runtime, record, live, drifted)
        if scope_mod.skill_policy_drift(live, ep.plan, drifted):
            # The managed skill policy of another
            # release (with or without the skill deny): lazy, Attention, with the repair path.
            attention.append(lazy(cli_text.SKILL_POLICY_DRIFT.format(m8=m8, mid=mid), "skill-policy",
                                  cli_text.SKILL_POLICY_DRIFTS, repairable=True))
            lazy_alongside()
        elif scope_mod.fork_policy_drift(live, ep.plan, drifted):
            # The fork deny and the model fence's failure policy of another
            # release: lazy, Attention, with the repair path.
            attention.append(lazy(cli_text.FORK_POLICY_DRIFT.format(m8=m8, mid=mid), "fork-policy",
                                  cli_text.FORK_POLICY_DRIFTS, repairable=True))
            lazy_alongside()
        elif scope_mod.status_line_drift(live, ep.plan, drifted):
            # The subagent status line another release compiled, or one a
            # user-owned key now replaces: lazy, Attention, with the repair path.
            attention.append(lazy(*status_line, repairable=True))
            if scope_mod.default_mode_differs(live, ep.plan):
                attention.append(lazy(*default_mode))
        elif scope_mod.default_mode_drift(live, ep.plan, drifted):
            # Decided once per launch; never BLOCK, repair never
            # adds or removes the key (the plan keeps the on-disk presence).
            attention.append(lazy(*default_mode))
        elif agent_files:
            # Only validated agent content moved,
            # and only because the installed catalog did: lazy, Attention.
            attention.append(lazy(
                f"session {m8}: agent definitions differ from the installed catalog "
                f"({', '.join(agent_files)}; catalog content changed since launch, bindings "
                f"unchanged); applies at the next resume (charmer -r {mid}) — or "
                f"charmer doctor --repair {mid} now",
                "agent-definitions",
                "{n} sessions: agent definitions differ from the installed catalog (catalog content changed since "
                "launch, bindings unchanged); each applies at its next resume — or charmer doctor --repair-all "
                "now (stopped sessions)",
                repairable=True))
        elif (deny_note := _deny_drift_note(runtime, live, ep.plan, m8, mid, drifted)) is not None:
            attention.append(deny_note)
        else:
            drift = "; ".join(drifted)
            problems.append(repair(
                f"session {m8}: scope differs from the record-authoritative compile ({drift}); "
                f"run charmer doctor --repair {mid}"
            ))
    else:
        info.append(
            f"Scope: {m8} OK (lineup generation {record['lineup_generation']})."
        )
        if not ep.launch_files_kept and ep.rebuilt_fence_matches is False:
            attention.append(lazy(
                f"session {m8}: launch-time files were rebuilt from the installed catalog "
                f"after launch; live agent changes need a relaunch — charmer -r {mid} "
                "after it exits",
                "launch-files-rebuilt",
                "{n} sessions: launch-time files were rebuilt from the installed catalog after launch; live agent "
                "changes need a relaunch — charmer -r <id> after it exits",
            ))
    if ep.diagnostics_kept:
        attention.append(lazy(
            f"session {m8}: review labels and model diagnostics changed since launch; "
            "the proven scope is kept until the next resume",
            "diagnostics-kept",
            "{n} sessions: review labels and model diagnostics changed since launch; the proven scope is kept "
            "until the next resume",
        ))
    if ep.launch_files_kept and ep.preference_launch_differs:
        # A preference-only launch-item difference is lazy
        # state with its own wording, never BLOCK and never "catalog changed".
        attention.append(lazy(
            f"session {m8}: managed-session preference changed; applies at the next resume",
            "preference", "{n} sessions: managed-session preference changed; each applies at its next resume",
        ))
    elif ep.launch_files_kept and ep.catalog_launch_differs:
        attention.append(lazy(
            f"session {m8}: fence/picker differ from the installed catalog (catalog "
            "changed since launch); applies at the next resume",
            "catalog-launch",
            "{n} sessions: fence/picker differ from the installed catalog (catalog changed since launch); each "
            "applies at its next resume",
        ))
    if ep.lead_switched:
        info.append(
            f"session {m8}: lead switched with /model since the last compile; restated "
            "at the next resume"
        )
    if ep.agent_class_kept:
        # A pending agent class move: the running session keeps its
        # recorded agent selectors; the class the installed catalog and the
        # session window decide applies (with a diff row) at the next
        # resume. Lazy by design: Attention, never BLOCK.
        rows = ", ".join(
            f"{label} {profile_mod.format_tokens(profile_mod.agent_class_window(kept))} → "
            f"{profile_mod.format_tokens(profile_mod.agent_class_window(installed))}"
            for label, kept, installed in ep.agent_class_kept
        )
        attention.append(
            f"session {m8}: agent context class changes at the next resume ({rows}) — "
            f"charmer -r {mid} after it exits"
        )
    # The compiled fence must admit every applied binding.
    if disk is not None:
        available = disk.settings.get("availableModels")
        if isinstance(available, list):
            applied = record["applied"]
            selectors = [
                ("lead", applied["lead"]["selector"]),
                *(
                    (profile_mod.label(rid), binding["selector"])
                    for rid, binding in applied["agents"].items()
                ),
            ]
            gaps = set(
                scope_mod.fence_gaps(available, [sel for _label, sel in selectors if sel])
            )
            for label, selector in selectors:
                if selector in gaps:
                    problems.append(repair(
                        f"session {m8}: {label} is bound to {selector}, which the compiled "
                        "fence does not admit — the agent would silently run on the lead "
                        f"model; run charmer doctor --repair {mid}"
                    ))
    return problems, attention, info


def _doctor_scope_report(
    runtime: runtime_mod.Runtime, *, plans: dict[str, ExpectedPlan | str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Session census plus per-record lines and v4 scope integrity.

    Returns ``(info, problems, attention)``. v1-3 records get one
    catalog-free line (no compiler call: a key removed from the catalog can
    never crash doctor in the post-activation, pre-migrate window, sc[6]);
    v4 records a ``record_summary`` line (``lead_needs_choice`` included,
    never raising) and, at gen >= 1, the §13.4 integrity check.
    """

    all_records, record_problems, record_ids = session_facts._session_record_scan(runtime)
    v4_records = [r for r in all_records if r.get("version") == sessions.RECORD_VERSION]
    legacy_records = [r for r in all_records if r.get("version") != sessions.RECORD_VERSION]
    unreadable = len(record_problems)
    info = [_sessions_line(len(record_ids), len(legacy_records), unreadable)]
    problems: list[str] = list(record_problems)
    attention: list[str] = []
    lcat = runtime.lineup_catalog()

    def identity_checks(record: dict[str, Any], state_label: str) -> None:
        if state_label not in {sessions.IDENTITY_REPAIR_NEEDED, sessions.IDENTITY_PENDING_FORK}:
            return
        if sessions.drop_resolved_pending_forks(record) is not None:
            attention.append(gateway_facts.tagged(
                f"session {sessions.managed_id(record)} holds a fork marker its live "
                "runtime already resolved; `charmer doctor --repair-all` clears it "
                "in place", area="Sessions", action="scope-repair",
            ))
        elif state_label == sessions.IDENTITY_PENDING_FORK:
            problems.append(
                f"session {sessions.managed_id(record)} identity is {state_label}; "
                f"{sessions.pending_fork_message(record)}"
            )
        else:
            problems.append(sessions.relink_message(record))

    for record in legacy_records:
        state_label = record.get("identity_state", sessions.IDENTITY_UNVERIFIED)
        try:
            lead_key = sessions.lead_identity(record)[0]
        except (KeyError, TypeError, sessions.SessionError):
            lead_key = "?"
        info.append(
            f"Session {session_facts._record_identity_label(record)}: legacy record · lead {lead_key} "
            f"(not checked until migrated) · identity {state_label} · cwd {record['cwd']}."
        )
        identity_checks(record, state_label)
    scope_mismatch = 0
    for record in v4_records:
        summary = sessions.record_summary(record, lcat)
        choice = " (needs a lead choice)" if runtime.resume_choice(record, lcat=lcat).needs_choice else ""
        info.append(
            f"Session {session_facts._record_identity_label(record)}: profile {summary.profile_label} · "
            f"lead {summary.lead_key}{choice} · lineup generation "
            f"{summary.lineup_generation} · identity {summary.identity_state} · cwd "
            f"{summary.cwd}."
        )
        identity_checks(record, summary.identity_state)
        m8 = summary.managed_id[:8]
        if record["lineup_generation"] == 0:
            info.append(f"session {m8}: legacy scope (not checked until its next launch)")
            continue
        scope_problems, scope_attention, scope_info = _check_scope_integrity(runtime, record, plans=plans)
        if scope_problems:
            scope_mismatch += 1
        problems.extend(scope_problems)
        attention.extend(scope_attention)
        info.extend(scope_info)
    if scope_mismatch:
        problems.append(gateway_facts.tagged(
            f"{scope_mismatch} scope(s) diverged from record authority; run "
            "`charmer doctor --repair-all` to converge them in one pass",
            area="Sessions", action="scope-repair",
        ))
    return info, problems, attention


def _doctor_collision_report(runtime: runtime_mod.Runtime) -> tuple[str, list[str]]:
    """Exact cm-* collisions against the default seed's bound agent ids."""

    files = session_facts._project_agent_files(runtime.cwd)
    count = len(files)
    noun = "project agent" if count == 1 else "project agents"
    try:
        lineup = runtime._evaluate(
            runtime.profiles.load(catalog.DEFAULT_SEED),
            profile_name=catalog.DEFAULT_SEED,
            effective=runtime.current_effective(),
            lcat=runtime.lineup_catalog(),
        )
    except (
        profile_mod.ProfileError,
        profile_mod.BindingError,
        settings_mod.SettingsError,
        catalog.CatalogError,
        cli_errors.CLIError,
    ) as exc:
        return (
            f"Collisions: not checked (seed {catalog.DEFAULT_SEED} unavailable: {exc}).",
            [],
        )
    collisions = scope_mod.find_cm_collisions(runtime.cwd, [], frozenset(lineup.agents))
    if collisions:
        problems = [
            session_facts._collision_error(path, name, runtime.cwd) for path, name in collisions
        ]
        return (
            f"Collisions: {len(collisions)} blocking ({count} {noun}).",
            problems,
        )
    return f"Collisions: none ({count} {noun}).", []


def _sessions_line(recorded: int, legacy: int, unreadable: int) -> str:
    """Doctor's sessions count; legacy records are named only when there are any."""

    parts = [f"Sessions: {recorded} recorded"]
    if legacy:
        parts.append(f"{legacy} legacy record{'s' if legacy != 1 else ''} (charmer migrate)")
    if unreadable:
        parts.append(f"{unreadable} unreadable")
    return " · ".join(parts) + "."


def _settings_denies(document: Any) -> frozenset[str] | None:
    permissions = document.get("permissions") if isinstance(document, dict) else None
    deny = permissions.get("deny") if isinstance(permissions, dict) else None
    return frozenset(item for item in deny if isinstance(item, str)) if isinstance(deny, list) else None


def _deny_drift_note(runtime: runtime_mod.Runtime, live: Path, plan: Any, m8: str, mid: str,
                     drifted: list[str]) -> str | None:
    """When a scope differs from this shell's compile only in its secret-file
    denies, which the launch environment selects (a key folder an environment
    variable moves, WSL), say so instead of reporting damage; ``None`` for any
    other difference."""

    if drifted != [f"{scope_mod.SETTINGS_RELPATH} content differs"]:
        return None
    try:
        disk = strict_json.loads(state.read_private(live / scope_mod.SETTINGS_RELPATH))
    except (OSError, ValueError, state.StateError):
        return None
    expected = plan.settings
    on_disk, wanted = _settings_denies(disk), _settings_denies(expected)
    if on_disk is None or wanted is None or on_disk == wanted:
        return None

    def without_deny(document: dict[str, Any]) -> dict[str, Any]:
        permissions = {key: value for key, value in document["permissions"].items() if key != "deny"}
        return {**document, "permissions": permissions}

    if without_deny(disk) != without_deny(expected):
        return None
    causes = dict(scope_mod.secret_deny_causes(runtime.environ))
    here, there = sorted(wanted - on_disk), sorted(on_disk - wanted)
    # Only the generated path denies move with the environment: a Read rule
    # (a moved config folder, WSL) or the Read and Edit pair of a key file
    # (scope.secret_path_denies); this shell's own additions must each have
    # a cause it can name.
    if not all(rule in causes for rule in here) or not _generated_path_denies(there):
        return None
    parts = []
    if here:
        parts.append("this shell adds " + "; ".join(f"{rule} ({causes[rule]})" for rule in here))
    if there:
        parts.append(f"the launching shell added {len(there)} rule(s) this shell does not select")
    return gateway_facts.Finding(
        f"session {m8}: its secret-file denies differ from this shell's compile only because the launch "
        f"environment selects them ({'; '.join(parts)}); repair or resume it from the shell you launch from "
        f"(charmer doctor --repair {mid}) so the denies follow that environment",
        code="secret-deny-environment", subject_id=mid,
        public="a session's secret-file denies depend on the environment it was launched from",
        remedy="repair or resume the session from the shell it is launched from",
    )


def _generated_path_denies(rules: list[str]) -> bool:
    """Whether every rule is one ``scope.secret_path_denies`` generates from
    the launch environment: ``Read(<path>)``, or ``Edit(<path>)`` paired with
    its ``Read(<path>)`` among ``rules``."""

    def split(rule: str) -> tuple[str, str] | None:
        verb, opening, rest = rule.partition("(")
        if not opening or not rest.endswith(")") or len(rest) < 2:
            return None
        return verb, rest[:-1]

    parsed = [split(rule) for rule in rules]
    if any(item is None for item in parsed):
        return False
    reads = {path for verb, path in parsed if verb == "Read"}
    return all(verb == "Read" or (verb == "Edit" and path in reads) for verb, path in parsed)


CONFIG_DIR_IGNORED = (
    "CLAUDE_CONFIG_DIR is set ({path}), but managed sessions do not use it: their settings, MCP servers, "
    "memory and resumable sessions come from ~/.claude; plain claude still uses {path}"
)


def _doctor_config_dir_report(runtime: runtime_mod.Runtime) -> list[str]:
    """Attention when the launcher's environment sets ``CLAUDE_CONFIG_DIR``,
    which every managed launch unsets (``compiler.V2_ENV_UNSET``)."""

    value = runtime.environ.get("CLAUDE_CONFIG_DIR")
    if not value:
        return []
    shown = paths.display(value, runtime.environ) if Path(value).is_absolute() else value
    return [gateway_facts.Finding(
        CONFIG_DIR_IGNORED.format(path=shown), code="claude-config-dir-ignored",
        public="CLAUDE_CONFIG_DIR is set, but managed sessions use ~/.claude",
        remedy="move what managed sessions need into ~/.claude, or unset CLAUDE_CONFIG_DIR",
    )]


def _provider_secret_names(runtime: runtime_mod.Runtime) -> frozenset[str]:
    """Every provider API-key name a launch compiles against (the merged
    providers and every providers.d declaration, admitted or not)."""

    return runtime.provider_secret_names()


def _doctor_env_keep_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """``(attention, info)`` of ``session_env_keep`` against this shell: the
    API-key variables managed sessions keep and the ones they do not get
    (names only, never a value)."""

    keep = runtime.session_env_keep()
    present = [name for name in runtime.environ if name.endswith("_API_KEY")]
    if not keep and not present:
        return [], []
    try:
        names = _provider_secret_names(runtime)
    except (KeyError, TypeError, cli_errors.CharmerError):
        names = frozenset()
    usable, refused = compiler.checked_env_keep(keep, credential_names=compiler.credential_env_names(names))
    unset = compiler.v2_env_unset(scalar=None, secret_env_names=names, launch_environ=runtime.environ,
                                  env_keep=usable)
    report = compiler.env_keep_report(unset, usable, refused, runtime.environ)
    attention = [gateway_facts.Finding(
        f"session_env_keep lists {name}, but managed sessions remove it: {reason}",
        code="session-env-keep-refused", public="a session_env_keep name is removed from managed sessions",
        remedy="remove the name from session_env_keep in Settings",
    ) for name, reason in report.refused]
    info: list[str] = []
    if report.kept or report.stripped:
        kept = ", ".join(report.kept) or "none"
        stripped = ", ".join(report.stripped) or "none"
        info.append(gateway_facts.Finding(
            f"Session environment: managed sessions keep {kept} (session_env_keep) and do not get {stripped}",
            code="session-env-keep",
            public="managed sessions keep only the API-key variables session_env_keep names",
        ))
    return attention, info


def _doctor_new_lines_report(runtime: runtime_mod.Runtime) -> list[str]:
    """Attention for each New model lacking the optional admission badge."""

    try:
        lines = runtime.lineup_catalog().lines
        admitted = set(runtime.current_effective().admitted_lines)
    except (cli_errors.CharmerError, KeyError, ValueError):
        return []
    found: list[str] = []
    for key in sorted(lines):
        line = lines[key]
        if line.get("status") != "new" or key in admitted:
            continue
        # The optional admission badge: an advisory, like every optional attestation.
        found.append(gateway_facts.tagged(gateway_facts.Finding(
            f"New model: {line.get('display', key)} ({key}) — not admitted (optional badge): "
            f"charmer models admit {key} (or Models, then Enter; use does not require it)",
            code="model-newly-available", subject_id=key if observations.identifier(key) else None,
            public="a new model is available and not admitted", remedy=f"charmer models admit {key}",
        ), area="Models", level=ADVISORY, subject=key))
    return found


@dataclass(frozen=True)
class DoctorDiagnostic(observations.Diagnostic):
    """A doctor finding in the JSON report: the shared diagnostic and the
    area doctor lists it under. Its severity may also be ``advisory``
    (optional evidence), which the report's status ignores."""

    area: str | None = None

    def __post_init__(self) -> None:
        if self.severity == ADVISORY:
            # Every other field is checked as any diagnostic's is.
            observations.Diagnostic("info", self.code, self.text, self.subject_id, self.remedy)
        else:
            super().__post_init__()
        if self.area is not None and self.area not in (*AREAS, OTHER_AREA):
            raise ValueError("invalid diagnostic area")


def _diagnostic(severity: str, line: str) -> observations.Diagnostic:
    """A typed finding's own fields; every other line the redacted fallback,
    coded by its action or model finding code when it has one. An advisory
    line has the ``advisory`` severity; every finding carries its area."""

    level = ADVISORY if severity == "attention" and getattr(line, "level", None) == ADVISORY else severity
    area = getattr(line, "area", None)
    if isinstance(line, gateway_facts.Finding):
        try:
            return DoctorDiagnostic(level, line.code, line.public, line.subject_id, line.remedy, area)
        except ValueError:
            pass
    fallback = observations.legacy_diagnostic("info" if level == ADVISORY else severity, line)
    text = fallback.text.replace(": info;", f": {ADVISORY};", 1) if level == ADVISORY else fallback.text
    group = getattr(line, "group", None) or ()
    code = getattr(line, "action", None) or (group[1] if len(group) > 1 and group[0] == "model" else fallback.code)
    try:
        return DoctorDiagnostic(level, code, text, area=area)
    except ValueError:
        return DoctorDiagnostic(level, fallback.code, text, area=area)


def _health_backend(runtime: runtime_mod.Runtime) -> str | None:
    """The recorded gateway backend, which names where credential health is
    read (``quota.health_source``); None when it cannot be read."""

    try:
        return service.backend_of(runtime.home).name
    except (cli_errors.CharmerError, OSError, ValueError):
        return None


# The one closing line of a doctor report, per health (the command and the
# card's H print the same).
DOCTOR_SUMMARY = {
    "blocked": "Launches are blocked until each blocking item above is fixed; each line names its fix.\n",
    "attention": "Nothing blocks a launch; each attention item says what to look at.\n",
    "ready": "Catalog, profiles, and local gateway are valid.\n",
}

# Routine detail plain doctor shows only with -v: one line per session, the
# gateway's counters and the evidence notes. Never a blocking reason or an
# attention item (those are other lists).
DETAIL_PREFIXES = (
    "Session ", "session ", "Scope: ", "Hook targets:", "Gateway instance lock:", "Gateway starts in the last",
    "Evidence:", "Collisions: none", "Claude Code copies kept", "Shared daemon:", "Operator state:",
    "HTTPS trust (",
)


def detail_line(line: str) -> bool:
    """A routine detail line (shown with ``doctor -v``)."""

    return line.startswith(DETAIL_PREFIXES)
