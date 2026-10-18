"""Exact-client probe: which representation of the session window lets a
``cm-*`` agent's own ``autoCompactWindow`` lower its window.

Network-hermetic like every real-binary probe (``bwrap --unshare-net``,
loopback fake provider, fixture homes, fixture lines chosen by shape).
Responders keep metadata only: request models, counts, the scripted usage
they reported and the hint-header ids; never prompt, answer or transcript
text.

The pinned client resolves a role's compaction window in this order: a valid
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` in the process environment (a settings
``env`` value lands there too), then a ``--autocompact`` session value, then
the ``autoCompactWindow`` settings (the last settings source that sets the
top-level key replaces every earlier source's per-model values), then the
model's own default. A subagent's frontmatter ``autoCompactWindow`` only
lowers the window it would otherwise inherit, and never under the
environment variable. Three representations of the same session window are
run on the production compile:

* ``env``: the earlier compile (the variable in the process environment and
  in the compiled flag settings);
* ``settings``: the production compile, a top-level ``autoCompactWindow`` in
  the compiled flag settings, the variable neutralised
  (:data:`NEUTRAL_WINDOW_ENV`) in the flag-settings environment and absent
  from the process environment;
* ``flag``: a ``--autocompact`` launch flag, the variable neutralised the
  same way.

A ``cm-*`` agent's own window comes from the production compile too (a line
bound below the 200K class); the other cases add one by hand. The ``flag``
form is measured, not compiled: ``/autocompact`` in a running session puts
the settings back in place of a ``--autocompact`` value.

Each case names the trigger the role must compact at; every run must reach
its intended model (an agent that fell back to the lead is never evidence).
``PASS`` needs every ``settings`` case (class
:data:`CLASS_SETTINGS`, the production form); any other class fails. The
output line is a stable interface that release evidence reads:
``compaction window probe: <STATUS> class=<class> <detail>``.
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
WINDOW_ENV = scope.WINDOW_ENV
# The client takes the variable only when it is set to a valid count: "0"
# is invalid (it is not positive), so resolution moves on to the settings.
NEUTRAL_WINDOW_ENV = scope.WINDOW_ENV_NEUTRAL
# A provider bound below the 200K class (the overflow the agent window is
# for) and an agent window above the client's 100K floor.
SMALL_BOUND = 128_000
AGENT_WINDOW = 128_000
# Below the client's floor (100K): invalid, ignored.
BELOW_FLOOR = 64_000
# Above the 200K class: never raises the agent's window.
ABOVE_CLASS = 500_000
# A 1M-class agent's own lower window.
WIDE_AGENT_WINDOW = 300_000
# What a user's own layers try to set.
USER_WINDOW = 300_000
# A case's agent file without the own window the compile gave it.
NO_AGENT_WINDOW = 0
REPRESENTATIONS = ("env", "settings", "flag")
CLASS_SETTINGS = "settings-window-lowers-agents"
CLASS_FLAG = "flag-window-lowers-agents"


def line(status: str, observed: str, detail: str) -> str:
    return f"{LINE_PREFIX} {status} class={observed} {detail}".rstrip()


def trigger(window: int, percent: int = settings.COMPACTION_PERCENT_DEFAULT) -> int:
    return profile.agent_compaction_trigger(window, percent)


def with_agent_window(agent_file: bytes, value: int) -> bytes:
    """``agent_file`` with ``autoCompactWindow: value`` after its effort line
    (in place of the one it has)."""

    text = agent_file.decode("utf-8")
    head, sep, rest = text.partition("\neffort: ")
    effort, newline, tail = rest.partition("\n")
    if not sep or not newline:
        raise AssertionError("the agent file has no effort line")
    if tail.startswith(f"{scope.AGENT_WINDOW_KEY}: "):
        tail = tail.partition("\n")[2]
    return f"{head}{sep}{effort}\nautoCompactWindow: {value}\n{tail}".encode("utf-8")


def without_agent_window(agent_file: bytes) -> bytes:
    """``agent_file`` without its own window."""

    lines = agent_file.decode("utf-8").split("\n")
    return "\n".join(line for line in lines if not line.startswith(f"{scope.AGENT_WINDOW_KEY}: ")).encode("utf-8")


def represent(document: dict, representation: str, window: int) -> tuple[dict, tuple[str, ...], bool]:
    """``(flag settings, extra argv, whether the process env carries the
    window)`` of ``document`` (the production compile) under
    ``representation``."""

    document = copy.deepcopy(document)
    env = dict(document.get("env") or {})
    if document.get(scope.AGENT_WINDOW_KEY) != window or env.get(WINDOW_ENV) != NEUTRAL_WINDOW_ENV:
        raise AssertionError("the production compile no longer carries the window as its settings key")
    if representation == "settings":
        return document, (), False
    del document[scope.AGENT_WINDOW_KEY]
    if representation == "env":
        env[WINDOW_ENV] = str(window)
        document["env"] = env
        return document, (), True
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
    narrow: bool  # the agent's line bound below the 200K class
    expected: int
    agent_window: int | None = None
    user_overrides: bool = False
    resume: bool = False
    # A user's ``/autocompact`` in the first run (it saves the window into the
    # user settings), the scripted work after a resume.
    autocompact_command: bool = False
    main_agent: bool = False
    extra_argv: tuple[str, ...] = field(default=())


def cases(window: int) -> tuple[Case, ...]:
    lead = trigger(window)
    narrow = trigger(profile.CLASS_STANDARD)
    return (
        # Today's representation: the variable wins over every frontmatter.
        Case("env-agent-window", "env", "agent", True, trigger(AGENT_WINDOW)),
        Case("settings-lead", "settings", "lead", False, lead),
        Case("settings-agent-window", "settings", "agent", True, trigger(AGENT_WINDOW)),
        Case("settings-agent", "settings", "agent", True, narrow, NO_AGENT_WINDOW),
        Case("settings-1m-agent", "settings", "agent", False, lead),
        Case("settings-1m-agent-window", "settings", "agent", False, trigger(WIDE_AGENT_WINDOW),
             WIDE_AGENT_WINDOW),
        Case("settings-agent-window-above-class", "settings", "agent", True, narrow, ABOVE_CLASS),
        Case("settings-agent-window-below-floor", "settings", "agent", True, narrow, BELOW_FLOOR),
        Case("settings-lead-user-layers", "settings", "lead", False, lead, user_overrides=True),
        Case("settings-agent-window-user-layers", "settings", "agent", True, trigger(AGENT_WINDOW),
             user_overrides=True),
        Case("settings-lead-resume", "settings", "lead", False, lead, resume=True),
        Case("settings-lead-autocompact-command", "settings", "lead", False, lead, autocompact_command=True),
        Case("settings-main-thread-agent", "settings", "lead", True, lead, main_agent=True),
        Case("flag-lead", "flag", "lead", False, lead),
        Case("flag-lead-user-layers", "flag", "lead", False, lead, user_overrides=True),
        Case("flag-agent-window", "flag", "agent", True, trigger(AGENT_WINDOW)),
    )


def verdict(observed: dict, expected: dict) -> tuple[str, dict]:
    """``(class, per-representation outcome)`` from the cases' rows. Every
    run must reach its intended model and have done its scripted work; a case
    passes when the role first compacts exactly at its expected trigger."""

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
    by = {rep: all(value == "ok" for name, value in outcome.items() if expected[name]["representation"] == rep)
          for rep in REPRESENTATIONS}
    if outcome.get("env-agent-window") == "ok":
        observed_class = "env-window-lowers-agents"
    elif by["settings"]:
        observed_class = CLASS_SETTINGS
    elif by["flag"]:
        observed_class = CLASS_FLAG
    else:
        observed_class = "no-representation-lowers-agents"
    return observed_class, outcome


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

    def test_the_agent_window_line_follows_the_effort_line(self) -> None:
        source = b"---\nname: cm-x\ndescription: \"d\"\nmodel: m\neffort: high\n---\nbody\n"
        self.assertEqual(with_agent_window(source, 128_000),
                         b"---\nname: cm-x\ndescription: \"d\"\nmodel: m\neffort: high\n"
                         b"autoCompactWindow: 128000\n---\nbody\n")

    def test_representations(self) -> None:
        document = {"env": {WINDOW_ENV: "0", "OTHER": "1"}, "autoCompactWindow": 800_000}
        self.assertEqual(represent(document, "settings", 800_000), (document, (), False))
        flag_settings, argv, process = represent(document, "env", 800_000)
        self.assertEqual(flag_settings, {"env": {WINDOW_ENV: "800000", "OTHER": "1"}})
        self.assertEqual((argv, process), ((), True))
        flag_settings, argv, process = represent(document, "flag", 800_000)
        self.assertEqual(flag_settings, {"env": {WINDOW_ENV: "0", "OTHER": "1"}})
        self.assertEqual((argv, process), (("--autocompact", "800000"), False))
        with self.assertRaises(AssertionError):
            represent({"env": {WINDOW_ENV: "800000"}}, "settings", 800_000)

    def test_the_production_compile_gives_a_small_bound_agent_its_own_window(self) -> None:
        narrow = production_compile(provider_tokens=SMALL_BOUND)
        agent = narrow["agent"]
        self.assertEqual((agent.client_class, agent.window, agent.agent_window), (200_000, SMALL_BOUND, SMALL_BOUND))
        self.assertEqual(agent.trigger, trigger(SMALL_BOUND))
        agent_file = narrow["plan"].agent_files[f".claude/agents/{_AGENT}.md"]
        self.assertIn(f"\nautoCompactWindow: {SMALL_BOUND}\n".encode(), agent_file)
        self.assertEqual(with_agent_window(without_agent_window(agent_file), SMALL_BOUND), agent_file)
        base = production_compile()
        self.assertIsNone(base["agent"].agent_window)
        self.assertNotIn(b"autoCompactWindow", base["plan"].agent_files[f".claude/agents/{_AGENT}.md"])

    def test_the_class_needs_every_case_of_a_representation(self) -> None:
        names = cases(800_000)
        expected = {case.name: {"target": case.target, "wire": "w", "trigger": case.expected,
                                "representation": case.representation} for case in names}
        rows = {case.name: {"work": 3, "spawned": True, "target_models": ["w"], "summaries": 1,
                            "first_summary_after": case.expected} for case in names}
        # Today's representation ignores the agent's window.
        rows["env-agent-window"]["first_summary_after"] = 162_000
        self.assertEqual(verdict(rows, expected)[0], CLASS_SETTINGS)
        broken = copy.deepcopy(rows)
        broken["settings-lead-user-layers"]["first_summary_after"] = trigger(USER_WINDOW)
        self.assertEqual(verdict(broken, expected)[0], CLASS_FLAG)
        broken["flag-agent-window"]["target_models"] = ["lead"]
        observed_class, outcome = verdict(broken, expected)
        self.assertEqual(observed_class, "no-representation-lowers-agents")
        self.assertEqual(outcome["flag-agent-window"], "model-wrong")
        never = copy.deepcopy(rows)
        never["settings-lead"]["summaries"] = 0
        self.assertEqual(verdict(never, expected)[1]["settings-lead"], "never-compacted")
        with self.assertRaises(client_probe._Inconclusive):
            verdict(dict(rows, **{"settings-agent": {**rows["settings-agent"], "spawned": False}}), expected)


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

        per_model = {lead_selector: {"autoCompactWindow": USER_WINDOW},
                     wire(lead_selector): {"autoCompactWindow": USER_WINDOW}}
        layer = {"autoCompactWindow": USER_WINDOW, "modelSettings": per_model,
                 "env": {WINDOW_ENV: str(USER_WINDOW)}}
        for target in (fixture.claude_config_dir / "settings.json",
                       fixture.project_dir / ".claude/settings.json",
                       fixture.project_dir / ".claude/settings.local.json"):
            self._settings(fixture, layer, str(target.relative_to(fixture.root)))

    def _case(self, case: Case, compiled: dict) -> dict:
        plan, context = compiled["plan"], compiled["context"]
        flag_settings, argv, process_env = represent(plan.settings, case.representation, context.window)
        flag_settings["env"] = dict(flag_settings["env"], **_HINTS)
        agent_files = dict(plan.agent_files)
        relpath = f".claude/agents/{_AGENT}.md"
        if case.agent_window == NO_AGENT_WINDOW:
            agent_files[relpath] = without_agent_window(agent_files[relpath])
        elif case.agent_window is not None:
            agent_files[relpath] = with_agent_window(agent_files[relpath], case.agent_window)
        fixture = self._fixture()
        live = scope.write_scope(fixture.root / "state", str(uuid.uuid4()),
                                 scope.ScopePlan(agent_files, plan.settings, plan.other_files))
        if case.user_overrides:
            self._user_layers(fixture, compiled["lead"].selector)
        policy = probe.ProbeCompactionPolicy(context.window, context.percent) if process_env else None
        if case.user_overrides:
            # The user's own process environment, too (a launch clears it; the
            # neutral flag-settings value must win anyway).
            policy = probe.ProbeCompactionPolicy(USER_WINDOW, context.percent)
        alternatives = {case.expected, trigger(context.window), trigger(profile.CLASS_STANDARD),
                        trigger(AGENT_WINDOW), trigger(USER_WINDOW), trigger(WIDE_AGENT_WINDOW)}
        top = 180_000 if (case.narrow and case.target == "agent") else context.window
        responder = _WindowResponder(case.target, schedule(alternatives, top))
        extra = ("--add-dir", str(live), *argv, *case.extra_argv)
        if case.main_agent:
            extra += ("--agent", _AGENT)
        if case.autocompact_command:
            # A local command: no model request. It saves the user's window
            # into the user settings, which every later start reads.
            warm, _ = self._run(fixture, model=compiled["lead"].selector, extra=extra, document=flag_settings,
                                compaction=policy, prompt=f"/autocompact {USER_WINDOW}", timeout=120)
            if warm.timed_out or warm.returncode != 0:
                raise probe.ProbeError("the /autocompact run did not complete")
            saved = json.loads((fixture.claude_config_dir / "settings.json").read_text())
            if not any(isinstance(entry, dict) and entry.get("autoCompactWindow") == USER_WINDOW
                       for entry in (saved.get("modelSettings") or {}).values()):
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
        base = production_compile()
        narrow = production_compile(provider_tokens=SMALL_BOUND)
        window = base["context"].window
        observed, expected = {}, {}
        for case in cases(window):
            compiled = narrow if case.narrow else base
            role = compiled["agent"] if case.target == "agent" else compiled["lead"]
            expected[case.name] = {"target": case.target, "wire": wire(role.selector), "trigger": case.expected,
                                   "representation": case.representation}
            observed[case.name] = self._case(case, compiled)
        self.evidence["expected"] = expected
        observed_class, outcome = verdict(observed, expected)
        detail = (f"window={window} percent={base['context'].percent} agent_window={AGENT_WINDOW} "
                  f"bound={narrow['bound']} neutral={NEUTRAL_WINDOW_ENV!r} "
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
            # The production compile is the settings representation: any
            # other class is a failure (an agent's own window is ignored).
            if observed != CLASS_SETTINGS:
                self.__class__.verdict = ("FAIL", observed, detail)
                self.fail(f"{observed}: {detail}")
            self.__class__.verdict = ("PASS", observed, detail)
        finally:
            fixture = self._fixture()
            probe.write_evidence(fixture, self.probe_id, self.evidence)


if __name__ == "__main__":
    unittest.main()
