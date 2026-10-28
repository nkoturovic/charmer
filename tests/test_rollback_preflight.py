"""The rollback preflight (``charmer.rollback_preflight``) and its question.

The target data is the real packaged catalog data of 2.2.1 and 2.3.0
(``tests/fixtures/rollback-preflight``, copied from the release tags). The
state is shaped like a 2.3.0 installation's: an OpenRouter line of the
user's own with the low effort contract, a profile binding ``haiku`` and
``or-kimi`` at max, and a running session on the selector 2.3.0 moved
``or-kimi`` to. This checkout's validators read both, as the release
being left does. Every check leaves the state tree byte for byte as it was
and removes its temporary resource root.

No provider call and no gateway: renders are candidate documents, and the
preflight starts no other program (the target's doctor is not run before
the operator answers; see the module's docstring).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_release_update  # module import: no test classes re-exported
from _fake_release import FakeRelease
from _layout import FIXTURES_ROOT, RESOURCES_ROOT
from test_trust import SEED_A, signer_line
from charmer import (catalog, continuity, custom, observations, operator as operator_mod,
                     profile as profile_mod, proxy, release_update as ru, render, rollback_preflight as rp,
                     self_update as su, state, strict_json)

DATA = FIXTURES_ROOT / "rollback-preflight"
OLD, NEW = DATA / "2.2.1", DATA / "2.3.0"
ANCHOR = "docs/update.md#rolling-back"
MOVED = "33333333-3333-4333-8333-333333333333"
CONTROLS = "44444444-4444-4444-8444-444444444444"
ENDED = "55555555-5555-4555-8555-555555555555"
SECRETS = (b"OPENROUTER_CLAUDE_API_KEY=or-dummy\nKIMI_CLAUDE_API_KEY=kimi-dummy\n"
           b"LEGACY_GW_API_KEY=legacy-dummy\n")
LOW_LINE = {"wire_model": "z-ai/glm-5.2", "display": "GLM 5.2", "family": "zhipu",
            "efforts": {"low": "output-config-low", "high": "output-config-high"}, "default_effort": "high",
            "context": {"declared_tokens": 200000, "source": "operator"}}
PLAIN_LINE = {"wire_model": "acme/plain-1", "display": "Plain", "family": "acme",
              "efforts": {"high": "output-config-high"}, "default_effort": "high",
              "context": {"declared_tokens": 200000, "source": "operator"}}
OLD_LINE = {"wire_model": "acme/old-1", "display": "Old", "family": "acme",
            "efforts": {"high": "output-config-high"}, "default_effort": "high",
            "context": {"declared_tokens": 200000, "source": "operator"}}
MINE = {"version": 2, "name": "mine", "description": "a 2.3.0 profile",
        "lead": {"model": "opus", "effort": "high"},
        "agents": {"cm-explorer": {"model": "haiku", "effort": "medium"},
                   "cm-implementer": {"model": "or-kimi", "effort": "max"}},
        "native_agents": {"explore": "replace", "general_purpose": "off", "plan": "native"},
        "workflows": "native"}
LOW_TEXT = ("providers.d/openrouter.json: lines.custom-z-ai-glm-5-2: models.custom-z-ai-glm-5-2.efforts.low."
            "proxy_contract: proxy effort contract 'output-config-low' is not declared by provider 'openrouter'")
LOW_FINDING = f"  - {LOW_TEXT} — charmer models rm custom-z-ai-glm-5-2, or charmer models edit custom-z-ai-glm-5-2"
HAIKU_FINDING = "  - profile mine: agents.cm-explorer.model: unknown model 'haiku' — charmer profile edit mine"
MAX_FINDING = ("  - profile mine: agents.cm-implementer.effort: 'max' is not supported by 'or-kimi' "
               "(available: high, xhigh) — charmer profile edit mine")
MOVED_FINDING = ("  - session 33333333 uses charmer-or-kimi-3-1-high, which 2.2.1 does not serve — exit it first "
                 f"(end the session; after the rollback, charmer -r {MOVED} resumes it on 2.2.1's models)")
LIMITS_221 = "Checked with 2.4.0's rules against 2.2.1's data; 2.2.1 may still refuse what these rules accept."
NONE_UNCHANGED = "Also reported by 2.4.0: 0, unchanged by the rollback."
STATE_230_AGAINST_221 = [
    f"Declarations 2.2.1 cannot read ({ANCHOR}):", LOW_FINDING,
    f"Profiles and named bindings 2.2.1 cannot evaluate ({ANCHOR}):", HAIKU_FINDING, MAX_FINDING,
    f"Running sessions on selectors 2.2.1 does not serve ({ANCHOR}):", MOVED_FINDING,
    NONE_UNCHANGED,
    LIMITS_221,
]
def write_json(path: Path, document: dict) -> None:
    state.ensure_private_dir(path.parent)
    state.atomic_write(path, strict_json.pretty_file_bytes(document))


def declare(env: dict, lines: dict) -> None:
    """``providers.d/openrouter.json``: lines of the user's own on OpenRouter."""

    write_json(operator_mod.providers_dir(env) / "openrouter.json", {"version": 1, "lines": lines})


def save_profile(env: dict, root: Path, document: dict) -> None:
    profile_mod.ProfileStore.for_catalog(catalog.load_catalog(root), env).save(document)


def session(state_root: Path, identifier: str, lead: str, agents: dict, *, live: bool = True,
            fence: list[str] | None = None) -> None:
    """A session record (its applied lead and agents) and, with ``fence``,
    its compiled scope settings: the picker fence ``availableModels`` and
    ``model``, as a real launch writes them."""

    write_json(state_root / "sessions" / f"{identifier}.json", {
        "applied": {"lead": {"selector": lead},
                    "agents": {role: {"selector": selector} for role, selector in agents.items()}},
        "last_event_source": "startup" if live else "end"})
    if fence is not None:
        write_json(state_root / "scopes" / identifier / "settings.json", {"availableModels": fence, "model": lead})


# A 2.3.0 picker fence: every selector the lineup could switch to, including
# ones 2.2.1 does not serve (Haiku, the moved Muse, DeepSeek and Kimi lines).
FENCE_230 = ["claude-opus-5-5[1m]", "claude-fable-5-1[1m]", "claude-haiku-5-5[1m]", "charmer-muse-1-0-high[1m]",
             "charmer-or-deepseek-4-1-flash-1017-high[1m]", "charmer-or-kimi-3-1-high[1m]",
             "charmer-or-kimi-3-1-xhigh[1m]", "gpt-multi-sol-high[1m]"]


def state_230(env: dict, state_root: Path, root: Path) -> None:
    """The 2.3.0-shaped state: the low-contract OpenRouter line, the haiku
    and or-kimi max profile, a live session on the moved or-kimi selector."""

    declare(env, {"custom-z-ai-glm-5-2": LOW_LINE})
    save_profile(env, root, MINE)
    session(state_root, MOVED, "claude-opus-5-5[1m]", {"cm-implementer": "charmer-or-kimi-3-1-high[1m]"},
            fence=FENCE_230)


def tree(*roots: Path) -> dict[str, tuple[str, int]]:
    found = {}
    for base in roots:
        for path in sorted(base.rglob("*")) if base.exists() else ():
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""
            found[str(path)] = (digest, info.st_mode)
    return found


def scratch_roots() -> set[str]:
    return {name for name in os.listdir(tempfile.gettempdir()) if name.startswith("charmer-rollback-check-")}


class PreflightCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-preflight-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        secrets = state.ensure_private_dir(self.root / "secrets") / "claude.env"
        state.atomic_write(secrets, SECRETS)
        self.env = {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_STATE_HOME": str(self.root / "state"), "CHARMER_SECRET_ENV": str(secrets),
                    "CHARMER_REGISTRY_DIR": str(RESOURCES_ROOT / "registry")}
        self.home = Path(self.env["HOME"])
        self.home.mkdir(mode=0o700)
        self.state_root = Path(self.env["XDG_STATE_HOME"]) / "charmer"
        # This release's rules over 2.3.0's catalog: the release being left.
        self.current_root = rp.compose_root(RESOURCES_ROOT, rp.read_target(NEW), self.root / "current")

    def check(self, target_root: Path | None = OLD, *, target: str = "2.2.1") -> rp.Report:
        """The preflight, asserting it changed nothing and left no scratch root."""

        roots = [self.root / "home", self.root / "config", self.root / "state", self.root / "secrets"]
        before, scratch = tree(*roots), scratch_roots()
        report = rp.check(environ=self.env, home=self.home, state_root=self.state_root,
                          current_root=self.current_root, target_root=target_root, current="2.4.0", target=target)
        self.assertEqual(tree(*roots), before)
        self.assertEqual(scratch_roots(), scratch)
        return report

    def served(self, root: Path, env: dict | None = None) -> frozenset[str]:
        """What a render with the resources at ``root`` serves (test-side view)."""

        legacy = custom.load_registry(self.env)
        return rp._view(env or self.env, self.home, self.state_root, root, legacy).served


class DataFindingsTests(PreflightCase):
    def test_a_230_state_against_221_lists_each_kind_with_its_text_and_remedy(self) -> None:
        state_230(self.env, self.state_root, self.current_root)
        report = self.check()
        self.assertEqual(report.lines(), STATE_230_AGAINST_221)
        self.assertEqual([finding.group for finding in report.findings],
                         [rp.DECLARATIONS, rp.PROFILES, rp.PROFILES, rp.SESSIONS])
        self.assertIsNone(report.unreadable)
        self.assertEqual(report.unchanged, 0)

    def test_the_same_state_against_230_finds_nothing(self) -> None:
        state_230(self.env, self.state_root, self.current_root)
        report = self.check(NEW, target="2.3.0")
        self.assertEqual(report.lines(), [
            "Nothing found that 2.3.0 cannot read.",
            NONE_UNCHANGED,
            "Checked with 2.4.0's rules against 2.3.0's data; 2.3.0 may still refuse what these rules accept.",
        ])
        self.assertEqual(report.findings, ())

    def test_an_unreferenced_declaration_the_target_rejects_is_listed(self) -> None:
        # No profile, binding or session names it: the target's doctor would
        # call it Attention, yet the target's apply, add and rm refuse.
        declare(self.env, {"custom-z-ai-glm-5-2": LOW_LINE, "custom-or-plain": PLAIN_LINE})
        layer = proxy.operator_snapshot(self.env, catalog.load_catalog(self.current_root).docs, {},
                                        asset_root=self.current_root).layer
        self.assertEqual(operator_mod.blocking_problems(layer), ())
        report = self.check()
        self.assertEqual(report.lines(), [f"Declarations 2.2.1 cannot read ({ANCHOR}):", LOW_FINDING, NONE_UNCHANGED,
                                          LIMITS_221])

    def test_two_profile_failures_the_doctor_json_collapses_are_two_findings(self) -> None:
        save_profile(self.env, self.current_root, MINE)
        report = self.check()
        texts = [finding.text for finding in report.findings]
        self.assertEqual(texts, ["profile mine: agents.cm-explorer.model: unknown model 'haiku'",
                                 "profile mine: agents.cm-implementer.effort: 'max' is not supported by 'or-kimi' "
                                 "(available: high, xhigh)"])
        # The doctor JSON carries the same generic diagnostic for both.
        first, second = (observations.legacy_diagnostic("block", text) for text in texts)
        self.assertEqual(first, second)

    def test_a_named_binding_the_target_cannot_resolve_names_a_profile_that_uses_it(self) -> None:
        write_json(profile_mod.bindings_path(self.env),
                   {"version": 1, "bindings": {"fast": {"model": "haiku", "effort": "medium"},
                                               "spare": {"model": "haiku", "effort": "low"}}})
        document = {**MINE, "name": "uses", "agents": {"cm-explorer": {"use": "fast"}}}
        save_profile(self.env, self.current_root, document)
        report = self.check()
        lines = report.lines()
        self.assertIn("  - bindings.fast.model: unknown model 'haiku' — charmer profile edit uses (it uses this "
                      "binding), or the profile editor (charmer, then P)", lines)
        self.assertIn("  - bindings.spare.model: unknown model 'haiku' — rebind it in the profile editor "
                      "(charmer, then P)", lines)
        self.assertTrue([line for line in lines if line.startswith("  - profile uses: agents.cm-explorer")], lines)

    def test_what_both_releases_report_is_counted_never_listed(self) -> None:
        broken = {**PLAIN_LINE, "efforts": {"high": "output-config-bogus"}}
        declare(self.env, {"custom-z-ai-glm-5-2": LOW_LINE, "custom-or-broken": broken})
        save_profile(self.env, self.current_root,
                     {**MINE, "name": "old", "agents": {"cm-explorer": {"model": "no-such-model", "effort": "high"}}})
        report = self.check()
        self.assertEqual(report.lines(), [
            f"Declarations 2.2.1 cannot read ({ANCHOR}):", LOW_FINDING,
            "Also reported by 2.4.0: 2, unchanged by the rollback.",
            LIMITS_221,
        ])
        self.assertEqual(report.unchanged, 2)


class SessionTests(PreflightCase):
    def test_sessions_on_selectors_the_target_serves_are_not_flagged(self) -> None:
        # A retained capture: an operator line rendered once, then removed.
        declare(self.env, {"custom-or-old": OLD_LINE, "custom-or-plain": PLAIN_LINE})
        proxy.render_runtime_config(self.home, environ={**self.env, "CHARMER_ASSETS": str(self.current_root)},
                                    state_root=self.state_root)
        declare(self.env, {"custom-or-plain": PLAIN_LINE})
        # A continuity alias pruned since: served again only because a live
        # record names it (the record extension of the render).
        persisted = continuity.read(self.home)
        continuity.write(self.home, continuity.apply_prune(
            persisted, continuity.PrunePlan(("gpt-multi-sol-low",), ()), persisted["seeded_through_catalog"]))
        # A legacy custom.json model.
        write_json(custom.registry_path(self.env), {
            "version": 1,
            "providers": {"legacy-gw": {"base_url": "https://llm.example.invalid/v1", "auth_kind": "bearer",
                                        "secret_env": "LEGACY_GW_API_KEY"}},
            "models": {"legacy-model": {"provider": "legacy-gw", "wire_model": "legacy-wire",
                                        "context_tokens": 200000, "created_via": "manual"}}})
        controls = {
            "operator line": "custom-or-plain-high",
            "legacy model": "custom-legacy-model",
            "retained capture": "custom-or-old-high",
            "continuity seed": "gpt-multi-sol-high",
            "continuity revived by a live record": "gpt-multi-sol-low",
            "the target's own or-kimi": "charmer-or-kimi-high",
        }
        # The picker fence names selectors the target does not serve: never a
        # finding (only what the session runs on counts).
        session(self.state_root, CONTROLS, "claude-opus-5-5[1m]",
                {f"cm-role-{index}": f"{selector}[1m]" for index, selector in enumerate(controls.values())},
                fence=FENCE_230)
        session(self.state_root, ENDED, "charmer-or-kimi-3-1-high[1m]", {}, live=False)
        mine = self.served(self.current_root)
        with tempfile.TemporaryDirectory() as scratch:
            old_root = rp.compose_root(RESOURCES_ROOT, rp.read_target(OLD), Path(scratch) / "old")
            theirs = self.served(old_root)
            # Neither packaged catalog serves them: only the effective render does.
            packaged = set()
            for root in (self.current_root, old_root):
                docs = catalog.load_catalog(root).docs
                packaged |= render.catalog_alias_set(docs["providers"]["providers"], docs["models-v2"]["models"])
        for label, selector in controls.items():
            with self.subTest(label):
                self.assertIn(selector, mine)
                self.assertIn(selector, theirs)  # served by the target, so never a finding
                if label != "the target's own or-kimi":
                    self.assertNotIn(selector, packaged)
        # The ended session's selector is one the target does not serve.
        self.assertIn("charmer-or-kimi-3-1-high", mine)
        self.assertNotIn("charmer-or-kimi-3-1-high", theirs)
        report = self.check()
        self.assertEqual(report.lines(), ["Nothing found that 2.2.1 cannot read.", NONE_UNCHANGED, LIMITS_221])

    def test_a_running_session_on_a_selector_the_target_does_not_serve_is_a_finding(self) -> None:
        # One finding for the agent that runs on the moved selector; the fence
        # (which also names it, and other unserved selectors) adds none.
        session(self.state_root, MOVED, "claude-opus-5-5[1m]", {"cm-implementer": "charmer-or-kimi-3-1-high[1m]"},
                fence=FENCE_230)
        sessions = self.state_root / "sessions"
        (sessions / "66666666-6666-4666-8666-666666666666.json").write_bytes(b"{not json")
        report = self.check()
        self.assertEqual(report.lines(), [
            f"Running sessions on selectors 2.2.1 does not serve ({ANCHOR}):", MOVED_FINDING,
            NONE_UNCHANGED,
            "Session records not checked (unreadable): 66666666.",
            LIMITS_221,
        ])


class UnreadableDataTests(PreflightCase):
    def test_unreadable_target_data_could_not_check(self) -> None:
        state_230(self.env, self.state_root, self.current_root)
        models = json.loads((OLD / "catalog/models.json").read_text())
        cases = (
            ("catalog/models.json is missing", "catalog/models.json", None),
            ("catalog/models.json is not valid JSON", "catalog/models.json", b"{"),
            ("catalog/profiles holds no seed profile", "catalog/profiles", None),
            # A document format this release does not read.
            ("2.2.1's data cannot be read with 2.4.0's rules (catalog/models.json: unsupported data version 1",
             "catalog/models.json", json.dumps({**models, "version": 1}).encode()),
        )
        for needle, relative, data in cases:
            with self.subTest(needle):
                target = self.root / "damaged"
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(OLD, target)
                path = target / relative
                if data is not None:
                    path.write_bytes(data)
                elif path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                report = self.check(target)
                (line,) = report.lines()
                self.assertTrue(line.startswith(f"Could not check what 2.2.1 can read ({needle}"), line)
                self.assertEqual(report.findings, ())
        report = self.check(None)
        self.assertEqual(report.lines(),
                         ["Could not check what 2.2.1 can read (the retained release carries no packaged data)."])


class JourneyTests(test_release_update.JourneyTestCase):
    """The question: a finding refuses --yes and makes No the default; only
    unreadable data falls through to the question as it was."""

    FINDINGS = rp.Report("1.1.0", "1.0.0", (rp.Finding(rp.SESSIONS, "session 33333333 uses x", "exit it first"),))

    def setUp(self) -> None:
        super().setUp()
        self.seed(self.release("1.0.0"))
        code, out = self.run_journey(from_dir=str(self.release("1.1.0").dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_OK, out)
        self.calls: list[str] = []

    def preflight(self, report: rp.Report):
        def run(installation: su.Installation) -> rp.Report:
            self.calls.append(f"{installation.current.version} -> {installation.previous.version}")
            return report

        return self.journey(preflight=run)

    def test_yes_with_a_finding_refuses_and_changes_nothing(self) -> None:
        code, out = self.run_journey(mode="rollback", journey=self.preflight(self.FINDINGS), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertEqual(out.splitlines(), [
            "Rollback: 1.1.0 -> 1.0.0 (1.1.0 stays installed as the previous version, so a second rollback "
            "returns to it).",
            "Checking what 1.0.0 can read: its packaged data, with 1.1.0's rules; nothing is changed.",
            f"Running sessions on selectors 1.0.0 does not serve ({ANCHOR}):",
            "  - session 33333333 uses x — exit it first",
            "Also reported by 1.1.0: 0, unchanged by the rollback.",
            "Checked with 1.1.0's rules against 1.0.0's data; 1.0.0 may still refuse what these rules accept.",
            "charmer update: the rollback to 1.0.0 was not started: 1.0.0 cannot read what is listed above; "
            "nothing was changed.",
            f"  fix: prepare first ({ANCHOR}), or run without --yes to roll back anyway",
        ])
        self.assertEqual(self.calls, ["1.1.0 -> 1.0.0"])
        self.assertEqual(self.current(), "versions/1.1.0")

    def test_with_a_finding_no_is_the_default_and_an_explicit_yes_rolls_back(self) -> None:
        for answer, code_expected, last in ((False, ru.EXIT_DECLINED, "Nothing was changed."),
                                            (None, ru.EXIT_REFUSED, "Nothing was changed: with what is listed "
                                                                    "above, confirm on a terminal (--yes does not "
                                                                    "roll back).")):
            with self.subTest(answer=answer):
                ask = test_release_update.Answers(answer)
                code, out = self.run_journey(mode="rollback", journey=self.preflight(self.FINDINGS), ask=ask)
                self.assertEqual(code, code_expected, out)
                self.assertEqual(ask.questions, ["Roll back charmer to 1.0.0 now?"])
                self.assertIn(f"Prepare first ({ANCHOR}), or roll back anyway.", out)
                self.assertEqual(out.splitlines()[-1], last)
                self.assertEqual(self.current(), "versions/1.1.0")
        # The terminal's own answer: an empty line is No.
        prompt = io.StringIO()
        ask = ru.terminal_ask(io.StringIO("\n"), prompt, interactive=True)
        code, out = self.run_journey(mode="rollback", journey=self.preflight(self.FINDINGS), ask=ask)
        self.assertEqual(code, ru.EXIT_DECLINED, out)
        self.assertEqual(prompt.getvalue(), "Roll back charmer to 1.0.0 now? [y/N] ")
        code, out = self.run_journey(mode="rollback", journey=self.preflight(self.FINDINGS),
                                     ask=test_release_update.Answers(True))
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("Rolled back to charmer 1.0.0; 1.1.0 is now the previous version.", out)
        self.assertEqual(self.current(), "versions/1.0.0")

    def test_without_findings_and_when_data_is_unreadable_the_question_is_as_it_was(self) -> None:
        reports = {"nothing found": rp.Report("1.1.0", "1.0.0"),
                   "could not check": rp.Report("1.1.0", "1.0.0", unreadable="catalog/models.json is missing")}
        for label, report in reports.items():
            with self.subTest(label):
                code, out = self.run_journey(mode="rollback", journey=self.preflight(report), assume_yes=True)
                self.assertEqual(code, ru.EXIT_OK, out)
                self.assertIn(report.lines()[0], out)
                self.assertNotIn("Prepare first", out)
                self.assertIn("Rolled back to charmer 1.0.0", out)
                self.assertEqual(self.run_journey(mode="rollback", assume_yes=True)[0], ru.EXIT_OK)  # 1.1.0 again
        self.assertIn("Could not check what 1.0.0 can read (catalog/models.json is missing).",
                      reports["could not check"].lines())

    def test_the_hold_and_the_state_format_still_refuse_after_the_preflight(self) -> None:
        journey = self.preflight(rp.Report("1.1.0", "1.0.0"))
        fd = os.open(self.state / "state-version", os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"9\n")
        os.close(fd)
        code, out = self.run_journey(mode="rollback", journey=journey, assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertIn("cannot roll back to 1.0.0: the state was upgraded to format 9", out)
        self.assertEqual(self.calls, ["1.1.0 -> 1.0.0"])

    def test_an_interrupted_preflight_cancels(self) -> None:
        def interrupted(_installation):
            raise KeyboardInterrupt

        code, out = self.run_journey(mode="rollback", journey=self.journey(preflight=interrupted), assume_yes=True)
        self.assertEqual(code, ru.EXIT_CANCELLED, out)
        self.assertIn("Cancelled; nothing was changed.", out)
        self.assertEqual(self.current(), "versions/1.1.0")


class RetainedBundleTests(test_release_update.JourneyTestCase):
    """The updater's own seam over an installation whose previous version
    carries 2.2.1's packaged data."""

    def test_the_230_state_through_the_updater(self) -> None:
        secrets = self.tmp / "claude.env"
        state.atomic_write(secrets, SECRETS)
        environ = {**self.environ, "CHARMER_SECRET_ENV": str(secrets),
                   "CHARMER_REGISTRY_DIR": str(RESOURCES_ROOT / "registry")}
        current_root = rp.compose_root(RESOURCES_ROOT, rp.read_target(NEW), self.tmp / "current")
        overrides = {f"charmer/data/{relative}": raw for relative, raw in rp.read_target(OLD).items()}
        self.seed(self.release("1.0.0", overrides=overrides))
        code, out = self.run_journey(from_dir=str(self.release("1.1.0").dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_OK, out)
        state_230(environ, self.state, current_root)
        journey = self.journey(preflight=rp.for_rollback(environ, state_root=self.state, current_root=current_root))
        before = tree(self.home)
        code, out = self.run_journey(mode="rollback", journey=journey, assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertEqual(tree(self.home), before)
        lines = out.splitlines()
        self.assertEqual(lines[2:11], [line.replace("2.2.1", "1.0.0").replace("2.4.0", "1.1.0")
                                       for line in STATE_230_AGAINST_221])
        self.assertIn("run without --yes to roll back anyway", out)
        self.assertEqual(self.current(), "versions/1.1.0")


class InstalledCommandTests(test_release_update.JourneyTestCase):
    """The installed charmer's rollback runs the preflight: with a finding,
    --yes refuses and the whole home is byte for byte as it was; with none,
    it rolls back. No gateway is contacted (the preflight starts no program)."""

    def release(self, version: str, **kwargs) -> FakeRelease:
        return test_release_update.sign(FakeRelease(self.tmp / f"release-{version}", version, real_launchers=True,
                                                    trust=signer_line(SEED_A), **kwargs))

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LC_ALL": "C", "TMPDIR": str(self.tmp)}
        return subprocess.run([str(self.root / "current/bin/charmer"), *args], env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)

    def test_the_installed_rollback_checks_first(self) -> None:
        self.seed(self.release("1.0.0"))
        result = self.cli("update", "--from-dir", str(self.release("1.1.0").dir), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cli("update", "--rollback", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Checking what 1.0.0 can read: its packaged data, with 1.1.0's rules; nothing is changed.",
                      result.stdout)
        self.assertIn("Nothing found that 1.0.0 cannot read.", result.stdout)
        self.assertIn("Also reported by 1.1.0: 0, unchanged by the rollback.", result.stdout)
        self.assertIn("Checked with 1.1.0's rules against 1.0.0's data; 1.0.0 may still refuse what these rules "
                      "accept.", result.stdout)
        self.assertEqual(self.current(), "versions/1.0.0")

    def test_the_installed_rollback_refuses_yes_and_changes_nothing_with_a_finding(self) -> None:
        # The previous release carries 2.2.1's packaged data; the home holds
        # the 2.3.0-shaped state (declared through this checkout's data, which
        # the installed 1.1.0 runs).
        overrides = {f"charmer/data/{relative}": raw for relative, raw in rp.read_target(OLD).items()}
        self.seed(self.release("1.0.0", overrides=overrides))
        result = self.cli("update", "--from-dir", str(self.release("1.1.0").dir), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state_230({"HOME": str(self.home)}, self.state, RESOURCES_ROOT)
        # Every writing command of the installed launcher refreshes the shared
        # shims to itself; after the switch the first one does so. The refused
        # rollback itself must leave the home as it finds it — and the shims
        # must never name the previous release while it is only being checked.
        first = self.cli("update", "--rollback", "--yes")
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        hook = (self.state / "bin" / "charmer-hook").read_text()
        self.assertIn("versions/1.1.0", hook)
        self.assertNotIn("versions/1.0.0", hook)
        before = tree(self.home)
        result = self.cli("update", "--rollback", "--yes")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, first.stdout)
        for line in (f"Declarations 1.0.0 cannot read ({ANCHOR}):", LOW_FINDING,
                     f"Profiles and named bindings 1.0.0 cannot evaluate ({ANCHOR}):", HAIKU_FINDING, MAX_FINDING,
                     "Also reported by 1.1.0: 0, unchanged by the rollback.",
                     "charmer update: the rollback to 1.0.0 was not started: 1.0.0 cannot read what is listed above; "
                     "nothing was changed."):
            self.assertIn(line, result.stdout.splitlines(), result.stdout)
        after = tree(self.home)
        self.assertEqual({path: value for path, value in after.items() if before.get(path) != value}, {})
        self.assertEqual(sorted(set(before) - set(after)), [])
        self.assertEqual(self.current(), "versions/1.1.0")


if __name__ == "__main__":
    unittest.main()
