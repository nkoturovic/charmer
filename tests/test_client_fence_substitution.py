"""Client check fence-substitution: the client's own model changes against the fence.

Claude Code 2.1.295-2.1.302 changed several paths on which the client itself
re-sends a request differently or on another model. Each case runs the
pinned client against a fixture fence (``availableModels``) and keeps every
request's wire model; the class holds when no request leaves the models the
session chose, and every model change shows on the switch hooks charmer
records through (``PreModelSwitch``/``PostModelSwitch``):

* ``beta-refused`` (2.1.295): the gateway refuses the context-1m beta of a
  ``[1m]`` lead with a 400 naming it; the client resends once without the
  beta, on the same model. The lead is a gateway name the client does not
  know: from 2.1.311 a canonical Anthropic id that is 1M on its own drops an
  ineffective ``[1m]`` and sends no beta to refuse (``client check
  one-m-suffix``);
* ``above-200k`` (2.1.299): a gateway model name the client does not know
  (200K assumed) past 200K tokens: the next turn compacts and answers on the
  same model instead of failing with "Prompt is too long";
* ``response-model`` (2.1.301): every response names another model than the
  one requested: three turns, one request each, every one on the requested
  model, no compaction loop, never the response's model;
* ``switch`` (2.1.301): a terminal ``/model`` from a ``[1m]`` lead to a smaller
  lead-set model after a 300K-token turn: every request stays on the two
  fenced models, and both switch hooks fire; whether the previous model
  summarises first is recorded (offline the client compacts on the new model
  when it knows neither window);
* ``fable-fallback`` (2.1.301): the gateway answers 404 for
  ``claude-fable-5-1``, asked by id and by the ``best`` alias, with the
  previous Fable (``claude-fable-5``) inside the fence: behind a gateway the
  client retries the same model and reports the error, it never moves to the
  previous Fable.

Hermetic like every real-client check; only models, request classes, beta
presence and counts leave the responder. The output line: ``client check
fence-substitution: <STATUS> class=<class> <detail>``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from charmer import probe, scope, state
from charmer.probe import ProbeError

from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

PASS_CLASS = "FS-fenced"
_CONTRACT = RESOURCES_ROOT / "catalog" / "native-contract.json"
_BETA = "context-1m-2025-08-07"
_FABLE, _PREVIOUS_FABLE = "claude-fable-5-1", "claude-fable-5"
_RESPONSE_MODEL = "fence-upstream-substituted"
# A gateway name the client does not know (200K unless [1m], which adds the beta).
_BETA_LEAD = "fence-gw-wide"


def wire(selector: str) -> str:
    return selector.removesuffix("[1m]")


class _Inconclusive(AssertionError):
    """A case did not exercise its path: no class can be chosen."""


def verdict(observed: dict[str, dict[str, Any]]) -> str:
    """The class from each case's request rows."""

    def models(case: str) -> set[str]:
        return {row["model"] for row in observed[case]["rows"]}

    beta = observed["beta-refused"]["rows"]
    if not beta or not beta[0]["beta"]:
        raise _Inconclusive("beta-refused: the first request carried no context-1m beta")
    if models("beta-refused") != {_BETA_LEAD} or len(beta) < 2 or beta[-1]["beta"]:
        return "FS-beta-resend-changed"
    big = observed["above-200k"]
    if big["returncodes"] != [0, 0] or models("above-200k") != {"fence-gw-lead"}:
        return "FS-above-200k-failed"
    if not any(row["class"] == "compaction" for row in big["rows"]):
        raise _Inconclusive("above-200k: the second turn did not compact")
    response = observed["response-model"]
    if _RESPONSE_MODEL in models("response-model") or models("response-model") != {"fence-gw-lead"}:
        return "FS-response-model-followed"
    if any(row["class"] == "compaction" for row in response["rows"]) or len(response["rows"]) != 3:
        return "FS-response-model-compacts"
    switch = observed["switch"]
    if not models("switch") <= {"fence-big", "fence-small"} or "fence-small" not in models("switch"):
        return "FS-switch-outside"
    if switch["hooks"] != ["PreModelSwitch", "PostModelSwitch"]:
        return "FS-switch-unrecorded"
    for case in ("fable-fallback", "best-fallback"):
        if _PREVIOUS_FABLE in models(case):
            return "FS-fable-substituted"
        if models(case) != {_FABLE}:
            raise _Inconclusive(f"{case}: no request on {_FABLE} ({models(case)})")
    return PASS_CLASS


class _Provider(probe.FakeAnthropicProvider):
    """The fake gateway, also keeping whether each request carried the
    context-1m beta (a boolean; no header value is kept)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.betas: dict[int, bool] = {}
        self._beta_lock = threading.Lock()

    def _record(self, method, path, headers, body, document):  # noqa: ANN001
        context = super()._record(method, path, headers, body, document)
        with self._beta_lock:
            self.betas[context.ordinal] = _BETA in (headers.get("anthropic-beta") or "")
        return context


class _Script:
    """One case's answers: ``beta`` refuses a request carrying the beta;
    ``fable`` refuses ``claude-fable-5-1`` with a 404; ``tokens`` is the
    usage every answer reports; ``response_model`` names another model."""

    def __init__(self, *, refuse_beta: bool = False, refuse_fable: bool = False, tokens: int = 1,
                 response_model: str | None = None) -> None:
        self.refuse_beta = refuse_beta
        self.refuse_fable = refuse_fable
        self.tokens = tokens
        self.response_model = response_model
        self.provider: _Provider | None = None
        self.rows: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> tuple[int, Any]:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        model = document.get("model")
        assert self.provider is not None
        beta = self.provider.betas.get(context.ordinal, False)
        hints = dict(context.record.hint_header_values)
        with self.lock:
            self.rows.append({"model": model, "beta": beta, "class": hints.get("x-claude-code-request-class")})
        if self.refuse_beta and beta:
            return 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                    "message": f"{_BETA} is not supported for this model"}}
        if self.refuse_fable and model == _FABLE:
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": f"model: {_FABLE}"}}
        payload = probe._message_payload(
            [{"type": "text", "text": "PROBE-OK"}], model=self.response_model or model, stop_reason="end_turn",
            message_id=f"msg_fence_{context.ordinal:04d}_{uuid.uuid4().hex[:8]}", input_tokens=self.tokens)
        return 200, probe._sse_message(payload) if document.get("stream") else payload


class VerdictTests(unittest.TestCase):
    def rows(self) -> dict[str, dict[str, Any]]:
        def row(model: str, beta: bool = False, kind: str = "main") -> dict[str, Any]:
            return {"model": model, "beta": beta, "class": kind}

        return {
            "beta-refused": {"rows": [row(_BETA_LEAD, True), row(_BETA_LEAD)]},
            "above-200k": {"rows": [row("fence-gw-lead"), row("fence-gw-lead", kind="compaction"),
                                    row("fence-gw-lead")], "returncodes": [0, 0]},
            "response-model": {"rows": [row("fence-gw-lead")] * 3},
            "switch": {"rows": [row("fence-big"), row("fence-small", kind="auxiliary"), row("fence-small")],
                       "hooks": ["PreModelSwitch", "PostModelSwitch"]},
            "fable-fallback": {"rows": [row(_FABLE), row(_FABLE)]},
            "best-fallback": {"rows": [row(_FABLE), row(_FABLE)]},
        }

    def test_the_class_needs_every_case_on_its_chosen_models(self) -> None:
        rows = self.rows()
        self.assertEqual(verdict(rows), PASS_CLASS)
        changed = {**rows, "beta-refused": {"rows": [rows["beta-refused"]["rows"][0], {
            "model": "claude-opus-5", "beta": False, "class": "main"}]}}
        self.assertEqual(verdict(changed), "FS-beta-resend-changed")
        self.assertEqual(verdict({**rows, "above-200k": {**rows["above-200k"], "returncodes": [0, 1]}}),
                         "FS-above-200k-failed")
        followed = {"rows": [*rows["response-model"]["rows"][:2], {"model": _RESPONSE_MODEL, "beta": False,
                                                                    "class": "main"}]}
        self.assertEqual(verdict({**rows, "response-model": followed}), "FS-response-model-followed")
        self.assertEqual(verdict({**rows, "switch": {**rows["switch"], "hooks": ["PreModelSwitch"]}}),
                         "FS-switch-unrecorded")
        moved = {"rows": [{"model": _FABLE, "beta": False, "class": "main"},
                          {"model": _PREVIOUS_FABLE, "beta": False, "class": "main"}]}
        self.assertEqual(verdict({**rows, "best-fallback": moved}), "FS-fable-substituted")
        with self.assertRaises(_Inconclusive):
            verdict({**rows, "beta-refused": {"rows": [{"model": _BETA_LEAD, "beta": False, "class": "main"}]}})


class FenceSubstitutionProbe(unittest.TestCase):
    probe_id = "fence-substitution"
    verdict: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")

    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = real_binary_gate(_CONTRACT, what="the fence-substitution check")
        if cls.gate.boundary:
            cls.verdict = ("BOUNDARY", "unavailable", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls) -> None:
        status, observed, detail = cls.verdict
        print(f"client check {cls.probe_id}: {status} class={observed} {detail}".rstrip(), flush=True)

    def setUp(self) -> None:
        if self.gate.boundary:
            self.skipTest(self.gate.boundary)
        self.trusted = self.gate.trusted
        self.root = Path(tempfile.mkdtemp(prefix="charmer-fence-substitution-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.count = 0

    def _fixture(self) -> probe.ProbeFixture:
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    def _settings(self, fixture: probe.ProbeFixture, lead: str, fence: list[str],
                  extra: dict[str, Any] | None = None) -> Path:
        document: dict[str, Any] = {"availableModels": fence, "model": lead,
                                    "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE},
                                    # The compiled window variable, so a 1M lead does not compact at 200K.
                                    "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}}
        document.update(extra or {})
        return probe.write_fixture_settings(fixture, "scope/settings.json", document)

    def _turns(self, lead: str, fence: list[str], script: _Script, prompts: tuple[str, ...]) -> dict[str, Any]:
        """Headless turns of one session (the first fresh, the rest resumed)."""

        fixture = self._fixture()
        settings = self._settings(fixture, lead, fence)
        session = str(uuid.uuid4())
        returncodes = []
        for index, prompt in enumerate(prompts):
            provider = _Provider(responder=script)
            script.provider = provider
            with provider:
                result = probe.run_native(
                    ("-p", prompt, "--settings", str(settings), "--model", lead,
                     *(("--session-id", session) if index == 0 else ("--resume", session))),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                    environ=self.environ, child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"})
            if result.timed_out:
                raise _Inconclusive("a turn timed out")
            returncodes.append(result.returncode)
        return {"rows": list(script.rows), "returncodes": returncodes}

    def _switch(self) -> dict[str, Any]:
        """A terminal /model to a smaller lead-set model after a 300K-token turn."""

        fixture = self._fixture()
        log = fixture.root / "switch-hooks.jsonl"
        hook = fixture.root / "switch-hook.py"
        state.atomic_write(hook, (
            "#!/usr/bin/python3\nimport json, sys\n"
            f"event = json.load(sys.stdin).get('hook_event_name')\nopen({str(log)!r}, 'a').write(str(event) + '\\n')\n"
            "print('{}')\n").encode("utf-8"))
        hook.chmod(0o700)
        hooks = {event: [{"hooks": [{"type": "command", "command": str(hook), "timeout": 5}]}]
                 for event in ("PreModelSwitch", "PostModelSwitch")}
        settings = self._settings(fixture, "fence-big[1m]", ["fence-big[1m]", "fence-small"], {"hooks": hooks})
        script = _Script(tokens=300000)

        def pause() -> None:
            time.sleep(2.5)

        step = probe.PTYInteraction
        steps = [step(b"Choose", b"2\r"), step(b"Press", b"\r"),
                 step("❯".encode(), b"client check one\r"), step(b"PROBE-OK", b"", preserve_after_wait=True),
                 step("❯".encode(), b"/model fence-small\r"),
                 step("❯".encode(), b"\r", before_send=pause),
                 step("❯".encode(), b"client check two\r", before_send=pause),
                 step(b"PROBE-OK", b"", preserve_after_wait=True),
                 step("❯".encode(), b"/exit\r", before_send=pause)]
        provider = _Provider(responder=script)
        script.provider = provider
        with provider:
            result = probe.run_native_pty(("--settings", str(settings), "--model", "fence-big[1m]"), steps,
                                          trusted=self.trusted, fixture=fixture, provider=provider, timeout=150,
                                          allow_real=True, environ=self.environ,
                                          child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"})
        if result.timed_out or result.returncode != 0:
            raise _Inconclusive("the terminal switch run did not complete")
        hooks_seen = log.read_text(encoding="utf-8").split() if log.exists() else []
        return {"rows": list(script.rows), "hooks": hooks_seen}

    def test_client_model_changes_stay_on_the_chosen_models(self) -> None:
        observed: dict[str, dict[str, Any]] = {}
        try:
            observed["beta-refused"] = self._turns(
                f"{_BETA_LEAD}[1m]", [f"{_BETA_LEAD}[1m]"], _Script(refuse_beta=True), ("client check",))
            observed["above-200k"] = self._turns(
                "fence-gw-lead", ["fence-gw-lead"], _Script(tokens=210000), ("client check one", "client check two"))
            observed["response-model"] = self._turns(
                "fence-gw-lead[1m]", ["fence-gw-lead[1m]"], _Script(response_model=_RESPONSE_MODEL),
                ("client check one", "client check two", "client check three"))
            observed["switch"] = self._switch()
            fence = [_FABLE, _PREVIOUS_FABLE]
            observed["fable-fallback"] = self._turns(_FABLE, fence, _Script(refuse_fable=True), ("client check",))
            observed["best-fallback"] = self._turns("best", ["best", *fence], _Script(refuse_fable=True),
                                                    ("client check",))
            observed_class = verdict(observed)
        except _Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            raise
        except ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                type(self).verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            type(self).verdict = ("INCONCLUSIVE", "unavailable", "probe execution unavailable")
            raise
        previous_summary = any(row["model"] == "fence-big" and row["class"] == "compaction"
                               for row in observed["switch"]["rows"])

        def summary(case: str) -> str:
            rows = observed[case]["rows"]
            return "/".join(f"{row['model']}{'+beta' if row['beta'] else ''}:{row['class'] or '-'}" for row in rows)

        detail = (" ".join(f"{case}=[{summary(case)}]" for case in observed)
                  + f" switch-hooks={observed['switch']['hooks']} previous-model-summary="
                  f"{'seen' if previous_summary else 'not seen (compacts on the new model)'}"
                  f" fable/best returncodes={observed['fable-fallback']['returncodes']}"
                  f"/{observed['best-fallback']['returncodes']}"
                  f" client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("PASS" if observed_class == PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, PASS_CLASS, detail)


if __name__ == "__main__":
    unittest.main()
