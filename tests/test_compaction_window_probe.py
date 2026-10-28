"""Exact-client probe: the session window's representation, and why a ``cm-*``
agent's own window is not compiled.

Network-hermetic like every real-binary probe (``bwrap --unshare-net``,
loopback fake provider, fixture homes, fixture lines chosen by shape).
Responders keep metadata only: request models, counts, the scripted usage
they reported and the hint-header ids; never prompt, answer or transcript
text.

The pinned client resolves a role's compaction window in this order: a valid
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` in the process environment (a settings
``env`` value lands there too; a value below 100K counts as 100K), then a
``--autocompact`` session value, then the ``autoCompactWindow`` settings
(100K to 1M; another value is ignored), then the model's own default. A
subagent's frontmatter ``autoCompactWindow`` only lowers the window it would
otherwise inherit; from 2.1.312 that includes the variable's window (it never
raises it, and a value below 100K is ignored).

The production compile (``env``) states the window as the variable in the
compiled flag settings and the launch environment. Its cases gate the probe:
the lead compacts at the session window's trigger fresh, on ``--resume``,
against a user's top-level, per-model and env values in the user, project
and local settings and the process environment, after ``/autocompact``
(which saves nothing under the variable), and with a native ``--autocompact``
flag passed straight to the client (a launch refuses one; the variable
outranks it anyway); a
``[1m]`` agent on a line bound below the window shares its 200K class's
window; a lead set bound below 100K compacts at the client's 100K floor,
never at the model's own default.

Per-agent windows (an agent file's ``autoCompactWindow``) are not compiled: a
lineup states one window for the session. The ``env-agent-window`` cases
record, never gating, what an agent's own window does under the variable:
it lowers the agent's window (a 200K-class agent to 128K, a 1M-class agent to
300K), keeps doing so after ``--resume``, never raises it (500K) and is
ignored below 100K (64K).
The other representations are measured for the record, never gating:

* ``settings``: a top-level ``autoCompactWindow`` in the flag settings, the
  variable stated "0" (refused, skipped) there and absent from the process
  environment: an agent's own window lowers its window and the lead holds
  against the user's layers, a saved ``/autocompact``, resume and ``--agent``
  offline, but a native ``--autocompact`` replaces it and a window below 100K
  is ignored; survival of a supervisor takeover cannot be measured offline;
* ``flag``: a ``--autocompact`` launch flag (``/autocompact`` in a running
  session puts the settings back in its place; read from the build).

Each case names the trigger the role must compact at; every run must reach
its intended model (an agent that fell back to the lead is never evidence).
``PASS`` (class :data:`PASS_CLASS`) needs every gating case; the output line
is a stable interface that release evidence reads: ``compaction window probe:
<STATUS> class=<class> <detail>``.
"""

from __future__ import annotations

import copy
import json
import unittest
import uuid
from dataclasses import dataclass, field

import test_scope_probe_client as client_probe
from _tier import real_binary_gate
from charmer import probe, profile, scope, settings
from test_agent_window_probe import _AGENT, _HINTS, _WindowResponder, production_compile, wire

LINE_PREFIX = "compaction window probe:"
WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
# The settings representation's key, and the value that makes the client
# skip the variable ("0" is not a positive count).
WINDOW_KEY = "autoCompactWindow"
NEUTRAL_WINDOW_ENV = "0"
# The client's smallest session window: the variable below it counts as it.
CLIENT_FLOOR = 100_000
# A provider bound below the 200K class (the overflow an agent window is
# for) and an agent window above the client's floor.
SMALL_BOUND = 128_000
AGENT_WINDOW = 128_000
# A lead set bound below the client's floor (an accepted operator line).
SMALL_LEAD = 80_000
# Below the client's floor (100K): invalid, ignored.
BELOW_FLOOR = 64_000
# Above the 200K class: never raises the agent's window.
ABOVE_CLASS = 500_000
# A 1M-class agent's own lower window.
WIDE_AGENT_WINDOW = 300_000
# What a user's own layers try to set.
USER_WINDOW = 300_000
# A native --autocompact value larger than the session window.
FLAG_WINDOW = 1_000_000
REPRESENTATIONS = ("env", "settings", "flag")
PASS_CLASS = "env-window-holds"



# The completion record the re-pin gate requires (charmer.upgrade); release
# evidence finds this module as its producer by this literal.
ESSENTIAL_RECORD = "compaction window probe: PASS class=env-window-holds "

def line(status: str, observed: str, detail: str) -> str:
    return f"{LINE_PREFIX} {status} class={observed} {detail}".rstrip()


def trigger(window: int, percent: int = settings.COMPACTION_PERCENT_DEFAULT) -> int:
    return profile.agent_compaction_trigger(window, percent)


def with_agent_window(agent_file: bytes, value: int) -> bytes:
    """``agent_file`` with ``autoCompactWindow: value`` after its effort line."""

    text = agent_file.decode("utf-8")
    head, sep, rest = text.partition("\neffort: ")
    effort, newline, tail = rest.partition("\n")
    if not sep or not newline:
        raise AssertionError("the agent file has no effort line")
    if WINDOW_KEY.encode() in agent_file:
        raise AssertionError("the compiled agent file already carries a window")
    return f"{head}{sep}{effort}\n{WINDOW_KEY}: {value}\n{tail}".encode("utf-8")


def represent(document: dict, representation: str, window: int) -> tuple[dict, tuple[str, ...], bool]:
    """``(flag settings, extra argv, whether the process env carries the
    window)`` of ``document`` (the production compile: the variable in the
    flag settings) under ``representation``."""

    document = copy.deepcopy(document)
    env = dict(document.get("env") or {})
    if env.get(WINDOW_ENV) != str(window) or WINDOW_KEY in document:
        raise AssertionError("the production compile no longer carries the window as the variable")
    if representation == "env":
        return document, (), True
    env[WINDOW_ENV] = NEUTRAL_WINDOW_ENV
    document["env"] = env
    if representation == "settings":
        document[WINDOW_KEY] = window
        return document, (), False
    if representation == "flag":
        return document, ("--autocompact", str(window)), False
    raise ValueError(representation)


def schedule(candidates, top: int) -> tuple[int, ...]:
    """Scripted usage per work step: one step short of, at and past every
    candidate trigger below ``top``, then ``top``."""

    steps = {40_000, top}
    for value in candidates:
        if value < top:
            steps.update({value - 1_000, value, value + 1_000})
    return tuple(sorted(step for step in steps if step > 0))


@dataclass(frozen=True)
class Case:
    name: str
    representation: str
    target: str  # "lead" or "agent"
    compile: str  # "base", "narrow" (the agent's line below the 200K class) or "small" (the lead set below 100K)
    expected: int
    agent_window: int | None = None
    user_overrides: bool = False
    resume: bool = False
    # A user's ``/autocompact`` in a first run (it saves the window into the
    # user settings), the scripted work in the next.
    autocompact_command: bool = False
    main_agent: bool = False
    extra_argv: tuple[str, ...] = field(default=())
    gating: bool = False
    # A user settings env (the user layer) and extra flag settings env.
    user_env: tuple[tuple[str, str], ...] = field(default=())
    flag_env: tuple[tuple[str, str], ...] = field(default=())


def cases(window: int) -> tuple[Case, ...]:
    lead = trigger(window)
    narrow = trigger(profile.CLASS_STANDARD)
    floor = trigger(CLIENT_FLOOR)
    return (
        # The production representation: these gate.
        Case("env-lead", "env", "lead", "base", lead, gating=True),
        Case("env-lead-user-layers", "env", "lead", "base", lead, user_overrides=True, gating=True),
        Case("env-lead-resume", "env", "lead", "base", lead, resume=True, gating=True),
        Case("env-lead-autocompact-command", "env", "lead", "base", lead, autocompact_command=True, gating=True),
        Case("env-lead-autocompact-flag", "env", "lead", "base", lead,
             extra_argv=("--autocompact", str(FLAG_WINDOW)), gating=True),
        Case("env-agent", "env", "agent", "narrow", narrow, gating=True),
        Case("env-small-lead", "env", "lead", "small", floor, gating=True),
        # Recorded, never gating: an own window under the variable (what a
        # per-agent window would need: lowering, an attempted raise, a value
        # below the client's floor, a 1M-class agent and resume), and the
        # other representations.
        Case("env-agent-window", "env", "agent", "narrow", trigger(AGENT_WINDOW), AGENT_WINDOW),
        Case("env-agent-window-above-class", "env", "agent", "narrow", narrow, ABOVE_CLASS),
        Case("env-agent-window-below-floor", "env", "agent", "narrow", narrow, BELOW_FLOOR),
        Case("env-1m-agent-window", "env", "agent", "base", trigger(WIDE_AGENT_WINDOW), WIDE_AGENT_WINDOW),
        Case("env-agent-window-resume", "env", "agent", "narrow", trigger(AGENT_WINDOW), AGENT_WINDOW,
             resume=True),
        Case("settings-lead", "settings", "lead", "base", lead),
        Case("settings-agent-window", "settings", "agent", "narrow", trigger(AGENT_WINDOW), AGENT_WINDOW),
        Case("settings-agent", "settings", "agent", "narrow", narrow),
        Case("settings-1m-agent", "settings", "agent", "base", lead),
        Case("settings-1m-agent-window", "settings", "agent", "base", trigger(WIDE_AGENT_WINDOW),
             WIDE_AGENT_WINDOW),
        Case("settings-agent-window-above-class", "settings", "agent", "narrow", narrow, ABOVE_CLASS),
        Case("settings-agent-window-below-floor", "settings", "agent", "narrow", narrow, BELOW_FLOOR),
        Case("settings-lead-user-layers", "settings", "lead", "base", lead, user_overrides=True),
        Case("settings-agent-window-user-layers", "settings", "agent", "narrow", trigger(AGENT_WINDOW),
             AGENT_WINDOW, user_overrides=True),
        Case("settings-lead-resume", "settings", "lead", "base", lead, resume=True),
        Case("settings-lead-autocompact-command", "settings", "lead", "base", lead, autocompact_command=True),
        Case("settings-main-thread-agent", "settings", "lead", "narrow", lead, AGENT_WINDOW, main_agent=True),
        Case("settings-lead-autocompact-flag", "settings", "lead", "base", lead,
             extra_argv=("--autocompact", str(FLAG_WINDOW))),
        Case("settings-small-lead", "settings", "lead", "small", trigger(SMALL_LEAD)),
        Case("flag-lead", "flag", "lead", "base", lead),
        Case("flag-lead-user-layers", "flag", "lead", "base", lead, user_overrides=True),
        Case("flag-agent-window", "flag", "agent", "narrow", trigger(AGENT_WINDOW), AGENT_WINDOW),
        # Recorded: the compaction switches in a user's settings env against
        # the compiled scope (a launch unsets them in the process
        # environment), and the flag settings stating them off (from 2.1.311
        # a settings env can use DISABLE_COMPACT only to turn compaction off).
        Case("env-lead-user-disable-compact", "env", "lead", "base", lead, user_env=(("DISABLE_COMPACT", "1"),)),
        Case("env-lead-user-disable-auto-compact", "env", "lead", "base", lead,
             user_env=(("DISABLE_AUTO_COMPACT", "1"),)),
        Case("env-lead-user-disable-compact-flag-off", "env", "lead", "base", lead,
             user_env=(("DISABLE_COMPACT", "1"),), flag_env=(("DISABLE_COMPACT", "0"),)),
        Case("env-lead-user-disable-auto-compact-flag-off", "env", "lead", "base", lead,
             user_env=(("DISABLE_AUTO_COMPACT", "1"),), flag_env=(("DISABLE_AUTO_COMPACT", "0"),)),
        Case("env-lead-flag-disable-compact-off", "env", "lead", "base", lead, flag_env=(("DISABLE_COMPACT", "0"),)),
    )


def outcomes(observed: dict, expected: dict) -> dict[str, str]:
    """Each case's outcome: ``ok`` when the role first compacts exactly at
    its expected trigger. Every run must reach its intended model and have
    done its scripted work."""

    outcome: dict[str, str] = {}
    for name, row in observed.items():
        want = expected[name]
        if not row["work"]:
            raise client_probe._Inconclusive(f"{name}: the target never ran a scripted step")
        if want["target"] == "agent" and not row["spawned"]:
            raise client_probe._Inconclusive(f"{name}: the lead never spawned the agent")
        if row["target_models"] != [want["wire"]]:
            outcome[name] = "model-wrong"
        elif row["summaries"] and row["first_summary_after"] == want["trigger"]:
            outcome[name] = "ok"
        elif row["summaries"]:
            outcome[name] = f"compacted-at-{row['first_summary_after']}"
        else:
            outcome[name] = "never-compacted"
    return outcome


def verdict(outcome: dict[str, str], gating: set[str]) -> str:
    """:data:`PASS_CLASS` when every gating case is ``ok``; otherwise the
    first failing gating case's class."""

    for name in sorted(gating):
        if outcome.get(name) != "ok":
            return f"{name}-{outcome.get(name, 'missing')}"
    return PASS_CLASS


class ScheduleTests(unittest.TestCase):
    def test_every_candidate_is_straddled(self) -> None:
        steps = schedule((95_000, 162_000), 180_000)
        for value in (95_000, 162_000):
            self.assertEqual(steps[steps.index(value - 1_000) + 1], value)
        self.assertEqual(steps[-1], 180_000)

    def test_triggers_follow_the_client_rule(self) -> None:
        self.assertEqual(trigger(800_000), 702_000)
        self.assertEqual(trigger(200_000), 162_000)
        self.assertEqual(trigger(128_000), 95_000)
        self.assertEqual(trigger(CLIENT_FLOOR), 67_000)
        self.assertEqual(trigger(SMALL_LEAD), 47_000)

    def test_the_agent_window_line_follows_the_effort_line(self) -> None:
        source = b"---\nname: cm-x\ndescription: \"d\"\nmodel: m\neffort: high\n---\nbody\n"
        self.assertEqual(with_agent_window(source, 128_000),
                         b"---\nname: cm-x\ndescription: \"d\"\nmodel: m\neffort: high\n"
                         b"autoCompactWindow: 128000\n---\nbody\n")

    def test_representations(self) -> None:
        document = {"env": {WINDOW_ENV: "800000", "OTHER": "1"}}
        self.assertEqual(represent(document, "env", 800_000), (document, (), True))
        flag_settings, argv, process = represent(document, "settings", 800_000)
        self.assertEqual(flag_settings, {"env": {WINDOW_ENV: "0", "OTHER": "1"}, WINDOW_KEY: 800_000})
        self.assertEqual((argv, process), ((), False))
        flag_settings, argv, process = represent(document, "flag", 800_000)
        self.assertEqual(flag_settings, {"env": {WINDOW_ENV: "0", "OTHER": "1"}})
        self.assertEqual((argv, process), (("--autocompact", "800000"), False))
        with self.assertRaises(AssertionError):
            represent({"env": {WINDOW_ENV: "0"}, WINDOW_KEY: 800_000}, "env", 800_000)

    def test_the_production_compile_is_the_env_representation(self) -> None:
        """The window is the variable; no agent file carries its own window,
        a small-bound agent's included; a lead set below the client's floor
        states its own bound (the client counts it as 100K)."""

        for compiled in (production_compile(), production_compile(provider_tokens=SMALL_BOUND),
                         production_compile(lead_tokens=SMALL_LEAD)):
            plan, context = compiled["plan"], compiled["context"]
            self.assertEqual(represent(plan.settings, "env", context.window)[0], plan.settings)
            self.assertNotIn(WINDOW_KEY, plan.settings)
            for agent_file in plan.agent_files.values():
                self.assertNotIn(WINDOW_KEY.encode(), agent_file)
        narrow = production_compile(provider_tokens=SMALL_BOUND)["agent"]
        self.assertEqual((narrow.client_class, narrow.window), (profile.CLASS_STANDARD, profile.CLASS_STANDARD))
        small = production_compile(lead_tokens=SMALL_LEAD)["context"]
        self.assertEqual((small.window, small.trigger), (SMALL_LEAD, trigger(SMALL_LEAD)))

    def test_only_the_gating_cases_decide_the_class(self) -> None:
        names = cases(800_000)
        gating = {case.name for case in names if case.gating}
        self.assertEqual({case.representation for case in names if case.gating}, {"env"})
        self.assertNotIn("env-agent-window", gating)
        expected = {case.name: {"target": case.target, "wire": "w", "trigger": case.expected,
                                "representation": case.representation} for case in names}
        rows = {case.name: {"work": 3, "spawned": True, "target_models": ["w"], "summaries": 1,
                            "first_summary_after": case.expected} for case in names}
        # An agent's own window under the variable is recorded, not gating
        # (a client that ignores it, as 2.1.311 did, still passes).
        rows["env-agent-window"]["first_summary_after"] = 162_000
        rows["settings-lead-autocompact-flag"]["summaries"] = 0
        outcome = outcomes(rows, expected)
        self.assertEqual(outcome["env-agent-window"], "compacted-at-162000")
        self.assertEqual(outcome["settings-lead-autocompact-flag"], "never-compacted")
        self.assertEqual(verdict(outcome, gating), PASS_CLASS)
        broken = copy.deepcopy(rows)
        broken["env-lead-autocompact-flag"]["summaries"] = 0
        self.assertEqual(verdict(outcomes(broken, expected), gating), "env-lead-autocompact-flag-never-compacted")
        broken = copy.deepcopy(rows)
        broken["env-small-lead"]["first_summary_after"] = 162_000
        self.assertEqual(verdict(outcomes(broken, expected), gating), "env-small-lead-compacted-at-162000")
        broken = copy.deepcopy(rows)
        broken["env-agent"]["target_models"] = ["lead"]
        self.assertEqual(verdict(outcomes(broken, expected), gating), "env-agent-model-wrong")
        with self.assertRaises(client_probe._Inconclusive):
            outcomes(dict(rows, **{"env-agent": {**rows["env-agent"], "spawned": False}}), expected)


class CompactionWindowProbe(client_probe._ProbeCase, unittest.TestCase):
    """The representation matrix on the pinned client (offline; zero provider calls)."""

    probe_id = "compaction-window"
    test_observation = None  # run under the name below

    @classmethod
    def setUpClass(cls):
        cls.verdict = ("INCONCLUSIVE", "unavailable", "probe did not complete")
        cls.gate = real_binary_gate(client_probe._CONTRACT, what="the compaction window probe")
        if cls.gate.boundary:
            cls.verdict = ("BOUNDARY", "unavailable", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls):
        print(line(*cls.verdict), flush=True)

    def _user_layers(self, fixture, lead_selector: str) -> None:
        """A user's own window everywhere it can be written: the user,
        project and local settings (top level, per model and env)."""

        per_model = {lead_selector: {WINDOW_KEY: USER_WINDOW}, wire(lead_selector): {WINDOW_KEY: USER_WINDOW}}
        layer = {WINDOW_KEY: USER_WINDOW, "modelSettings": per_model, "env": {WINDOW_ENV: str(USER_WINDOW)}}
        for target in (fixture.claude_config_dir / "settings.json",
                       fixture.project_dir / ".claude/settings.json",
                       fixture.project_dir / ".claude/settings.local.json"):
            self._settings(fixture, layer, str(target.relative_to(fixture.root)))

    def _case(self, case: Case, compiled: dict) -> dict:
        plan, context = compiled["plan"], compiled["context"]
        flag_settings, argv, process_env = represent(plan.settings, case.representation, context.window)
        flag_settings["env"] = dict(flag_settings["env"], **_HINTS, **dict(case.flag_env))
        agent_files = dict(plan.agent_files)
        relpath = f".claude/agents/{_AGENT}.md"
        if case.agent_window is not None:
            agent_files[relpath] = with_agent_window(agent_files[relpath], case.agent_window)
        fixture = self._fixture()
        live = scope.write_scope(fixture.root / "state", str(uuid.uuid4()),
                                 scope.ScopePlan(agent_files, plan.settings, plan.other_files))
        if case.user_overrides:
            self._user_layers(fixture, compiled["lead"].selector)
        if case.user_env:
            target = fixture.claude_config_dir / "settings.json"
            self._settings(fixture, {"env": dict(case.user_env)}, str(target.relative_to(fixture.root)))
        # The launch's own process environment (the probe harness admits a
        # window from 100K; below it the flag settings alone carry it).
        policy = (probe.ProbeCompactionPolicy(context.window, context.percent)
                  if process_env and context.window >= CLIENT_FLOOR else None)
        if case.user_overrides:
            # The user's own process environment, too (a launch replaces it;
            # the flag settings' value must win anyway).
            policy = probe.ProbeCompactionPolicy(USER_WINDOW, context.percent)
        alternatives = {case.expected, trigger(context.window), trigger(profile.CLASS_STANDARD),
                        trigger(AGENT_WINDOW), trigger(USER_WINDOW), trigger(WIDE_AGENT_WINDOW),
                        trigger(CLIENT_FLOOR), trigger(SMALL_LEAD)}
        top = 180_000 if case.compile == "small" or (case.compile == "narrow" and case.target == "agent") \
            else context.window
        responder = _WindowResponder(case.target, schedule(alternatives, top))
        extra = ("--add-dir", str(live), *argv, *case.extra_argv)
        if case.main_agent:
            extra += ("--agent", _AGENT)
        if case.autocompact_command:
            # A local command: no model request. Without the variable it
            # saves the user's window into the user settings, which every
            # later start reads; under the variable it saves none.
            warm, _ = self._run(fixture, model=compiled["lead"].selector, extra=extra, document=flag_settings,
                                compaction=policy, prompt=f"/autocompact {USER_WINDOW}", timeout=120)
            if warm.timed_out or warm.returncode != 0:
                raise probe.ProbeError("the /autocompact run did not complete")
            if any(row.tool_count for row in warm.requests):
                raise client_probe._Inconclusive(f"{case.name}: /autocompact reached the model as a prompt")
            path = fixture.claude_config_dir / "settings.json"
            saved = json.loads(path.read_text()) if path.is_file() else {}
            saved = any(isinstance(entry, dict) and entry.get(WINDOW_KEY) == USER_WINDOW
                        for entry in (saved.get("modelSettings") or {}).values())
            self.evidence[f"{case.name}-saved"] = saved
            if case.representation != "env" and not saved:
                raise client_probe._Inconclusive(f"{case.name}: /autocompact saved no window")
        if case.resume:
            session = str(uuid.uuid4())
            warm, _ = self._run(fixture, model=compiled["lead"].selector, extra=(*extra, "--session-id", session),
                                document=flag_settings, compaction=policy, timeout=120)
            self._complete(warm)
            extra = (*extra, "--resume", session)
        result, responder = self._run(fixture, responder=responder, model=compiled["lead"].selector, extra=extra,
                                      document=flag_settings, compaction=policy, timeout=300)
        self._complete(result)
        row = responder.observation()
        self.evidence[case.name] = row
        return row

    def _probe(self):
        compiles = {"base": production_compile(), "narrow": production_compile(provider_tokens=SMALL_BOUND),
                    "small": production_compile(lead_tokens=SMALL_LEAD)}
        window = compiles["base"]["context"].window
        observed, expected, gating = {}, {}, set()
        for case in cases(window):
            compiled = compiles[case.compile]
            role = compiled["agent"] if case.target == "agent" else compiled["lead"]
            expected[case.name] = {"target": case.target, "wire": wire(role.selector), "trigger": case.expected,
                                   "representation": case.representation}
            if case.gating:
                gating.add(case.name)
            observed[case.name] = self._case(case, compiled)
        self.evidence["expected"] = expected
        outcome = outcomes(observed, expected)
        observed_class = verdict(outcome, gating)
        small = compiles["small"]["context"]
        detail = (f"window={window} percent={compiles['base']['context'].percent} "
                  f"small_lead={SMALL_LEAD} small_lead_compiled_trigger={small.trigger} "
                  f"agent_window={AGENT_WINDOW} bound={compiles['narrow']['bound']} "
                  + " ".join(f"{name}={value}@{observed[name]['first_summary_after']}/{expected[name]['trigger']}"
                             for name, value in outcome.items()))
        return observed_class, detail

    def test_representations_of_the_session_window(self):
        try:
            observed, detail = self._probe()
        except unittest.SkipTest as exc:
            self.__class__.verdict = ("BOUNDARY", "unavailable", str(exc))
            raise
        except client_probe._Inconclusive as exc:
            self.__class__.verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            self.fail(str(exc))
        except probe.ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                self.__class__.verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            self.__class__.verdict = ("INCONCLUSIVE", "unavailable", "probe execution unavailable")
            self.fail("probe execution unavailable (stdio deliberately not retained)")
        else:
            if observed != PASS_CLASS:
                self.__class__.verdict = ("FAIL", observed, detail)
                self.fail(f"{observed}: {detail}")
            self.__class__.verdict = ("PASS", observed, detail)
        finally:
            fixture = self._fixture()
            probe.write_evidence(fixture, self.probe_id, self.evidence)


class EssentialRecordTests(unittest.TestCase):
    def test_the_pass_line_is_the_gate_record(self) -> None:
        from charmer import upgrade

        self.assertIn(ESSENTIAL_RECORD, upgrade.ESSENTIAL_EVIDENCE_PREFIXES)
        self.assertTrue((line("PASS", PASS_CLASS, "") + " ").startswith(ESSENTIAL_RECORD))


if __name__ == "__main__":
    unittest.main()
