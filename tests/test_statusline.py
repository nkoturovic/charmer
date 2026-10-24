"""The shipped status lines (``charmer.statusline``) and their plumbing command.

The subagent rows follow Claude Code's ``subagentStatusLine`` contract
(2.1.293+): one JSON object with ``columns`` and ``tasks`` on stdin, one
``{"id", "content"}`` line per overridden row on stdout, exit 0 always. The
fourteen renderer cases are the earlier hand-installed status line's own
cases, ported one for one (shapes from the pinned client's task document);
plain-text checks strip the renderer's colour codes, one check keeps the
bytes. Synthetic documents only.
"""

from __future__ import annotations

import io
import json
import os
import platform
import random
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from charmer import scope, state, statusline, strict_json, transition
from charmer.cli import doctor
from charmer.cli.commands import statusline as statusline_command
from _golden import assertGolden
from _v4 import V4Case
from _catalog import GOLDENS_ROOT
from _layout import REPO_ROOT
from _release import load_tool

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RENDERER_CODES = re.compile(r"\x1b\[(0|2|31|32|33|34|35|36)m")

REVIEWER = {
    "id": "t1", "type": "local_agent", "agentType": "cm-reviewer", "status": "running",
    "description": "Review U1", "label": "Reading gateway_hold.py", "startTime": 1,
    "model": "gpt-multi-astra-xhigh[1m]", "effort": "xhigh", "contextWindowSize": 1048576,
    "tokenCount": 250000,
}
EXPLORE = {
    "id": "t2", "type": "local_agent", "agentType": "Explore", "status": "running",
    "description": "Find the hook", "label": "Find the hook", "startTime": 1, "tokenCount": 900,
}
FAILED = {
    "id": "t3", "type": "local_agent", "agentType": "cm-implementer", "name": "u1-writer",
    "status": "failed", "description": "Write U1", "label": "Write U1", "startTime": 1,
    "model": "claude-opus-5-5[1m]", "effort": "high", "contextWindowSize": 1000000, "tokenCount": 870000,
}
ZERO = {
    "id": "t0", "type": "local_agent", "agentType": "cm-explorer", "status": "running",
    "description": "Inspect fixture", "label": "Inspect fixture", "startTime": 1, "tokenCount": 0,
    "tokenSamples": [], "cwd": "/synthetic/workspace",
}
REVIEWER_FIXED = "cm-reviewer requested: gpt-multi-astra-xhigh[1m] · xhigh · 23% 250K/1.0M"


def rows(document: object) -> list[dict[str, str]]:
    return statusline.render_subagent_rows(document)


def output_lines(document: object) -> list[dict[str, str]]:
    """The stdout lines for ``document`` (JSON-encoded stdin), decoded."""

    data = statusline.subagent_output(json.dumps(document).encode())
    text = data.decode("ascii")
    assert text == "" or text.endswith("\n"), text
    return [json.loads(line) for line in text.splitlines()]


def plain(document: object) -> list[str]:
    return [_ANSI.sub("", row["content"]) for row in output_lines(document)]


class SubagentRowCaseTests(unittest.TestCase):
    """The fourteen cases of the earlier status line, one for one."""

    def test_01_charmer_reviewer_row(self) -> None:
        self.assertEqual(plain({"columns": 120, "tasks": [REVIEWER]}),
                         [REVIEWER_FIXED + " · Reading gateway_hold.py"])

    def test_02_charmer_selector_is_visibly_requested_not_served(self) -> None:
        task = {**REVIEWER, "model": "charmer-synthetic-model"}
        self.assertEqual(plain({"columns": 120, "tasks": [task]}),
                         ["cm-reviewer requested: charmer-synthetic-model · xhigh · 23% 250K/1.0M · "
                          "Reading gateway_hold.py"])
        # The label is the renderer's own: magenta "requested: ", the selector in blue.
        content = rows({"columns": 120, "tasks": [task]})[0]["content"]
        self.assertIn("\x1b[35mrequested: \x1b[0m\x1b[34mcharmer-synthetic-model\x1b[0m", content)

    def test_03_native_agent_without_a_model(self) -> None:
        self.assertEqual(plain({"columns": 120, "tasks": [EXPLORE]}), ["Explore · 900 · Find the hook"])

    def test_04_named_failed_agent_at_high_context(self) -> None:
        self.assertEqual(plain({"columns": 120, "tasks": [FAILED]}),
                         ["cm-implementer u1-writer claude-opus-5-5[1m] · high · 87% 870K/1.0M · failed · "
                          "Write U1"])
        content = rows({"columns": 120, "tasks": [FAILED]})[0]["content"]
        self.assertIn("\x1b[31m87%\x1b[0m", content)  # red from 85 %
        self.assertIn("\x1b[31mfailed\x1b[0m", content)
        self.assertIn("\x1b[2mu1-writer\x1b[0m", content)  # the name dimmed

    def test_05_client_shaped_zero_progress_without_name_model_or_effort(self) -> None:
        self.assertEqual(plain({"columns": 120, "tasks": [ZERO]}), ["cm-explorer · 0 · Inspect fixture"])

    def test_06_missing_id_omits_the_row(self) -> None:
        document = {"columns": 80, "tasks": [{"type": "local_agent", "agentType": "x", "tokenCount": 1}]}
        self.assertEqual(statusline.subagent_output(json.dumps(document).encode()), b"")
        for task_id in ("", 7, None, ["t"]):
            with self.subTest(task_id=task_id):
                self.assertEqual(rows({"columns": 80, "tasks": [{"id": task_id, "agentType": "x"}]}), [])

    def test_07_label_dropped_when_it_cannot_fit_eight_columns(self) -> None:
        self.assertEqual(plain({"columns": 70, "tasks": [REVIEWER]}), [REVIEWER_FIXED])

    def test_08_label_truncated_to_the_room(self) -> None:
        got = plain({"columns": 90, "tasks": [REVIEWER]})
        self.assertEqual(got, [REVIEWER_FIXED + " · Reading gatewa…"])
        self.assertLessEqual(len(got[0]), 90)

    def test_09_fixed_segments_stay_intact_for_native_clipping(self) -> None:
        got = plain({"columns": 20, "tasks": [REVIEWER]})
        self.assertEqual(got, [REVIEWER_FIXED])
        self.assertGreater(len(got[0]), 20)  # the fixture overflows; the client clips it

    def test_10_zero_columns_uses_the_fallback_label_budget(self) -> None:
        label = "abcdefghij" * 7
        expected = [REVIEWER_FIXED + " · " + label[:59] + "…"]
        for document in ({"columns": 0, "tasks": [{**REVIEWER, "label": label}]},
                         {"tasks": [{**REVIEWER, "label": label}]}):
            with self.subTest(document=sorted(document)):
                self.assertEqual(plain(document), expected)

    def test_11_one_line_per_task_in_order(self) -> None:
        lines = output_lines({"columns": 120, "tasks": [REVIEWER, EXPLORE]})
        self.assertEqual([line["id"] for line in lines], ["t1", "t2"])
        for line in lines:
            self.assertEqual(sorted(line), ["content", "id"])
            self.assertIsInstance(line["content"], str)

    def test_12_ansi_colours_are_kept(self) -> None:
        content = rows({"columns": 120, "tasks": [EXPLORE]})[0]["content"]
        self.assertTrue(content.startswith("\x1b[36mExplore\x1b[0m"), content)
        self.assertEqual(
            rows({"columns": 120, "tasks": [REVIEWER]})[0]["content"],
            "\x1b[36mcm-reviewer\x1b[0m \x1b[35mrequested: \x1b[0m\x1b[34mgpt-multi-astra-xhigh[1m]\x1b[0m"
            " \x1b[2m·\x1b[0m \x1b[33mxhigh\x1b[0m"
            " \x1b[2m·\x1b[0m \x1b[32m23%\x1b[0m \x1b[2m250K/1.0M\x1b[0m"
            " \x1b[2m·\x1b[0m \x1b[2mReading gateway_hold.py\x1b[0m")

    def test_13_malformed_input_prints_nothing_and_exits_zero(self) -> None:
        for bad in (b"not json", b"", b'{"tasks":"nope"}', b'{"columns":80}',
                    # and the other shapes the contract names malformed
                    b"[1]", b'{"tasks":[1]}', b'{"tasks":[{"id":"t1"},"t2"]}', b'{"columns":"80","tasks":[]}',
                    b'{"tasks":[{"id":"t1"}],"columns":"80"}', b"NaN", b'{"tasks":[{"id":"t","tokenCount":NaN}]}',
                    b'{"tasks":[]}{"tasks":[]}', b"\xff\xfe"):
            with self.subTest(stdin=bad):
                stdout = io.BytesIO()
                status = statusline.run("subagents", io.BytesIO(bad), _BinaryStream(stdout))
                self.assertEqual((status, stdout.getvalue()), (0, b""))
        # The decoded API itself: a task that is not an object means no rows, never an exception.
        for document in ({"tasks": [{"id": "t1"}, "t2"]}, {"tasks": [None, {"id": "t1"}]}):
            with self.subTest(document=document):
                self.assertEqual(rows(document), [])

    def test_14_control_and_terminal_sequences_are_sanitized_id_and_unicode_kept(self) -> None:
        task = {"id": "synthetic-id\n\x1b[2K", "type": "local_agent", "agentType": "cm-explorer",
                "name": "fixture-name", "model": "native-model", "effort": "high", "status": "running",
                "label": "fixture-label", "description": "fixture-description"}
        variants = {
            "control_ranges": ("".join(map(chr, (*range(32), *range(127, 160), 0x2028, 0x2029))) + "λ界café",
                               " " * 67 + "λ界café"),
            "terminal_sequences": (
                "A\nB\rC\tD\x1b[2KE\x1b[8mF\x1b]0;synthetic\x07G\x1b]0;synthetic\x1b\\H\x9b2KI\x9b8mJ"
                "\x9d0;synthetic\x9cKλ界café",
                "A B C D [2KE [8mF ]0;synthetic G ]0;synthetic \\H 2KI 8mJ 0;synthetic Kλ界café"),
        }
        checked = 0
        for variant, (dirty, clean) in variants.items():
            for field in ("agentType", "type", "name", "model", "effort", "status", "label", "description"):
                with self.subTest(variant=variant, field=field):
                    base = dict(task)
                    if field == "type":
                        del base["agentType"]
                    elif field == "description":
                        del base["label"]
                    expected = output_lines({"columns": 4096, "tasks": [{**base, field: clean}]})
                    data = statusline.subagent_output(
                        json.dumps({"columns": 4096, "tasks": [{**base, field: dirty}]}).encode())
                    self.assertTrue(data.endswith(b"\n") and data.count(b"\n") == 1, data)
                    row = json.loads(data)
                    self.assertEqual(row["id"], task["id"])  # the raw id, unchanged
                    self.assertEqual(row["content"], expected[0]["content"])
                    self.assertIn(clean, row["content"])
                    shown = _RENDERER_CODES.sub("", row["content"])
                    self.assertFalse([ch for ch in shown if ord(ch) < 32 or 127 <= ord(ch) <= 159
                                      or ch in "\u2028\u2029"])
                    checked += 1
        self.assertEqual(checked, 16)


class _BinaryStream:
    """A text-stream stand-in whose ``buffer`` is ``raw`` (like sys.stdout)."""

    def __init__(self, raw: io.BytesIO) -> None:
        self.buffer = raw


class SubagentRowRuleTests(unittest.TestCase):
    """Rules of the contract the ported cases do not pin one by one."""

    def test_context_colours_and_the_bare_token_count(self) -> None:
        for tokens, code in ((599_999, "32"), (600_000, "33"), (849_999, "33"), (850_000, "31")):
            with self.subTest(tokens=tokens):
                content = rows({"tasks": [{"id": "t", "tokenCount": tokens, "contextWindowSize": 1_000_000}]})
                self.assertIn(f"\x1b[{code}m{tokens * 100 // 1_000_000}%\x1b[0m", content[0]["content"])
        for window in (None, 0, -5, "1000000"):
            with self.subTest(window=window):
                self.assertEqual(plain({"tasks": [{"id": "t", "tokenCount": 1500, "contextWindowSize": window}]}),
                                 ["agent · 1K"])

    def test_token_formatting(self) -> None:
        for count, text in ((0, "0"), (999, "999"), (1000, "1K"), (999_999, "999K"), (1_000_000, "1.0M"),
                            (1_048_576, "1.0M"), (1_050_000, "1.1M"), (2_449_999, "2.4M"), (12.7, "12")):
            with self.subTest(count=count):
                self.assertEqual(statusline.format_tokens(count), text)

    def test_status_names_and_type_fallbacks(self) -> None:
        cases = (({"status": "completed"}, "agent · done"), ({"status": "running"}, "agent"),
                 ({"status": "killed"}, "agent · killed"), ({"type": "local_agent"}, "local_agent"),
                 ({"agentType": False, "type": "local_agent"}, "local_agent"),
                 ({"name": "agent"}, "agent"), ({"name": ""}, "agent"), ({"effort": ""}, "agent"))
        for fields, text in cases:
            with self.subTest(fields=fields):
                self.assertEqual(plain({"tasks": [{"id": "t", **fields}]}), [text])

    def test_only_gateway_selectors_are_labelled_requested(self) -> None:
        for model, text in (("charmer-sol-high", "requested: charmer-sol-high"),
                            ("gpt-multi-sol-high[1m]", "requested: gpt-multi-sol-high[1m]"),
                            ("custom-local", "custom-local"), ("claude-opus-5-5", "claude-opus-5-5")):
            with self.subTest(model=model):
                self.assertEqual(plain({"tasks": [{"id": "t", "model": model}]}), [f"agent {text}"])

    def test_the_selector_prefixes_are_the_catalogs(self) -> None:
        from charmer import catalog, selector_prefixes

        self.assertIs(statusline.GATEWAY_SELECTOR_PREFIXES, selector_prefixes.GATEWAY_SELECTOR_PREFIXES)
        self.assertEqual(catalog.RENAMED_SELECTOR_PREFIX, selector_prefixes.CHARMER_SELECTOR_PREFIX)
        selectors = {spec["selector"] for line in _catalog_lines() for spec in _efforts(line)}
        gateway = {s for s in selectors if s.startswith(selector_prefixes.GATEWAY_SELECTOR_PREFIXES)}
        self.assertTrue(gateway)
        # Every non-Anthropic catalog selector is in one of the two name spaces.
        others = {s for s in selectors if not s.startswith(selector_prefixes.GATEWAY_SELECTOR_PREFIXES)}
        self.assertTrue(all(s.startswith("claude-") for s in others), sorted(others))

    def test_rows_with_values_that_cannot_be_rendered_keep_their_native_row(self) -> None:
        huge = 10 ** 400
        self.assertEqual([row["id"] for row in rows({"tasks": [
            {"id": "a", "tokenCount": huge, "contextWindowSize": 3}, {"id": "b"}]})], ["b"])
        # An out-of-range number is the largest finite one, as jq reads it.
        line = statusline.subagent_output(b'{"tasks":[{"id":"c","tokenCount":1e400}]}')
        self.assertEqual(json.loads(line)["id"], "c")

    def test_the_api_takes_a_decoded_object_and_never_raises(self) -> None:
        for document in (None, 1, "x", [], {"tasks": None}, {"tasks": False}, {"tasks": True},
                         {"tasks": {"a": {"id": "t"}}}, {"columns": True, "tasks": [{"id": "t"}]}):
            with self.subTest(document=document):
                result = rows(document)
                self.assertIsInstance(result, list)
        self.assertEqual(rows({"columns": True, "tasks": [{"id": "t"}]}), [{"id": "t", "content": "\x1b[36magent\x1b[0m"}])

    def test_oversized_input_prints_nothing(self) -> None:
        document = json.dumps({"tasks": [{"id": "t"}], "pad": "x" * statusline.MAX_INPUT_BYTES}).encode()
        self.assertEqual(statusline.subagent_output(document), b"")


class PropertyTests(unittest.TestCase):
    """Whatever arrives on stdin: exit 0, and either nothing or valid rows."""

    def test_random_bytes_exit_zero_with_empty_output(self) -> None:
        rng = random.Random(20261025)
        for case in range(400):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
            with self.subTest(case=case):
                stdout = io.BytesIO()
                self.assertEqual(statusline.run("subagents", io.BytesIO(data), _BinaryStream(stdout)), 0)
                self.assertEqual(stdout.getvalue(), b"", data)

    def test_random_documents_give_well_formed_rows(self) -> None:
        rng = random.Random(4229)
        scalars = (None, True, False, 0, -1, 7, 2.5, 1e300, "", "x", "running", "completed", "cm-reviewer",
                   "charmer-sol-high", "\x1b[2K", "λ\u2028界", 10 ** 30)

        def value(depth: int) -> object:
            roll = rng.random()
            if depth > 2 or roll < 0.6:
                return rng.choice(scalars)
            if roll < 0.8:
                return [value(depth + 1) for _ in range(rng.randrange(3))]
            return {rng.choice(_FIELDS): value(depth + 1) for _ in range(rng.randrange(4))}

        for case in range(600):
            tasks = [{field: value(1) for field in rng.sample(_FIELDS, rng.randrange(len(_FIELDS)))}
                     for _ in range(rng.randrange(4))]
            for task in tasks:
                if rng.random() < 0.7:
                    task["id"] = f"t{case}-{rng.randrange(9)}"
            document = {"columns": rng.choice((0, 1, 20, 80, 120, 4096, -3, 33.5, None)), "tasks": tasks}
            with self.subTest(case=case):
                data = statusline.subagent_output(json.dumps(document).encode())
                for line in data.decode("ascii").splitlines():
                    row = json.loads(line)
                    self.assertEqual(sorted(row), ["content", "id"])
                    self.assertTrue(row["id"] and isinstance(row["content"], str))
                    shown = _RENDERER_CODES.sub("", row["content"])
                    self.assertFalse([ch for ch in shown if ord(ch) < 32 or 127 <= ord(ch) <= 159
                                      or ch in "\u2028\u2029"], row)


_FIELDS = ["id", "type", "agentType", "name", "model", "effort", "status", "label", "description",
           "tokenCount", "contextWindowSize", "startTime", "tokenSamples", "cwd"]


def _catalog_lines() -> list[dict]:
    from _layout import RESOURCES_ROOT

    models = json.loads((RESOURCES_ROOT / "catalog" / "models.json").read_text(encoding="utf-8"))["models"]
    return list(models.values())


def _efforts(line: dict) -> list[dict]:
    efforts = line.get("efforts")
    specs = list(efforts.values()) if isinstance(efforts, dict) else []
    return specs + ([{"selector": line["selector"]}] if isinstance(line.get("selector"), str) else [])


class PlumbingCommandTests(unittest.TestCase):
    """``charmer statusline subagents`` through the parser and the entry points."""

    def test_the_command_module_owns_the_kinds(self) -> None:
        from charmer.cli import parser as cli_parser

        self.assertEqual(statusline_command.KINDS, statusline.KINDS)
        parser = cli_parser.build_parser()
        for kind in statusline.KINDS:
            self.assertEqual(parser.parse_args(["statusline", kind]).statusline_kind, kind)

    def test_entry_main_routes_the_command_before_any_runtime(self) -> None:
        from unittest import mock

        import charmer.cli.entry as entry
        import charmer.cli.runtime as runtime_mod

        document = json.dumps({"columns": 120, "tasks": [EXPLORE]})
        stdout = io.StringIO()
        with mock.patch.object(runtime_mod, "Runtime", side_effect=AssertionError("Runtime built")):
            status = entry.main(["statusline", "subagents"], input_stream=io.StringIO(document),
                                output_stream=stdout)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["id"], "t2")

    def test_a_python_entry_point_answers_with_exit_zero(self) -> None:
        # The release launcher's interpreter program: -I, the site directory first.
        code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from charmer.entrypoints import main; raise SystemExit(main())")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for stdin, expect_row in ((json.dumps({"columns": 80, "tasks": [EXPLORE]}), True), ("{oops", False)):
            with self.subTest(stdin=stdin[:10]):
                result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(REPO_ROOT / "src"), "charmer",
                                         "statusline", "subagents"],
                                        input=stdin.encode(), capture_output=True, env=env, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                if expect_row:
                    self.assertEqual(json.loads(result.stdout)["id"], "t2")
                else:
                    self.assertEqual(result.stdout, b"")


class MainLineTests(unittest.TestCase):
    """``charmer statusline main``: the opt-in line for a user's own ``statusLine``."""

    MID = "11111111-1111-4111-8111-111111111111"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charmer-statusline-main-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.scope = self.home / ".local/state/charmer/scopes" / self.MID
        self.scope.mkdir(parents=True)
        (self.scope / "lineup.gen").write_text("7 0123456789ab\n")
        (self.scope / "lineup.md").write_text("# Lineup\n\nProfile: quality · generation 7\n")
        self.base = {"model": {"display_name": "Model", "id": "native-model"}, "effort": {"level": "high"},
                     "context_window": {"used_percentage": 73.8, "context_window_size": 1048576,
                                        "total_input_tokens": 765432},
                     "workspace": {"current_dir": str(self.home / "project")}, "cost": {"total_cost_usd": 0.1234}}

    def line(self, document: object, managed_id: str | None = None) -> str:
        environ = {"HOME": str(self.home)}
        if managed_id is not None:
            environ["CHARMER_MANAGED_ID"] = managed_id
        stdout = io.BytesIO()
        data = document if isinstance(document, bytes) else json.dumps(document).encode()
        self.assertEqual(statusline.run("main", io.BytesIO(data), _BinaryStream(stdout), environ), 0)
        return _ANSI.sub("", stdout.getvalue().decode("utf-8"))

    def scoped(self, directory: Path | None = None) -> dict:
        return {**self.base, "workspace": {**self.base["workspace"],
                                           "added_dirs": [None, 42, str(directory or self.scope)]}}

    def test_a_plain_session_has_no_marker_and_no_profile(self) -> None:
        self.assertEqual(self.line(self.base), "~/project Model · high · ctx 73% 765K/1.0M · $0.12\n")

    def test_a_managed_session_shows_the_marker_and_its_profile(self) -> None:
        self.assertEqual(self.line(self.scoped(), self.MID),
                         "~/project ◆Model · high · quality · ctx 73% 765K/1.0M · $0.12\n")
        (self.scope / "lineup.md").write_text("# Lineup\n\nProfile: ad-hoc direct · generation 8\n")
        self.assertIn(" · direct · ", self.line(self.scoped(), self.MID))

    def test_the_marker_needs_the_sessions_own_scope_directory(self) -> None:
        other = "22222222-2222-4222-8222-222222222222"
        for document, managed_id in ((self.base, self.MID), (self.scoped(), other), (self.scoped(), None),
                                     (self.scoped(), "not-a-uuid")):
            with self.subTest(managed_id=managed_id, scoped="added_dirs" in document["workspace"]):
                self.assertNotIn("◆", self.line(document, managed_id))

    def test_a_scope_without_a_lineup_generation_shows_no_profile(self) -> None:
        (self.scope / "lineup.gen").write_text("")
        self.assertEqual(self.line(self.scoped(), self.MID), "~/project ◆Model · high · ctx 73% 765K/1.0M · $0.12\n")
        (self.scope / "lineup.gen").write_text("7 0123456789ab\n")
        (self.scope / "lineup.md").write_text("unexpected\n")
        self.assertIn(" · lineup · ", self.line(self.scoped(), self.MID))

    def test_context_colours_cost_and_fallbacks(self) -> None:
        stdout = io.BytesIO()
        document = {**self.base, "context_window": {"used_percentage": 90, "total_input_tokens": 999999}}
        statusline.run("main", io.BytesIO(json.dumps(document).encode()), _BinaryStream(stdout), {})
        text = stdout.getvalue().decode("utf-8")
        self.assertIn("ctx \x1b[31m90%\x1b[0m \x1b[2m999K\x1b[0m", text)
        self.assertEqual(self.line({"model": {"id": "native-model"}, "cost": {"total_cost_usd": 0}}), "native-model\n")

    def test_display_text_is_sanitized(self) -> None:
        document = {"model": {"display_name": "M\x1b[2K\nX"}, "effort": {"level": "hi\x9bgh"},
                    "workspace": {"current_dir": "/p\x07q"}}
        self.assertEqual(self.line(document), "/p q M [2K X · hi gh\n")

    def test_unusable_input_prints_nothing_and_exits_zero(self) -> None:
        for data in (b"", b"not json", b"[1]", b"{}", b"null", b'{"model": 5}', b"\xff"):
            with self.subTest(data=data):
                self.assertEqual(self.line(data), "")


class CompiledStatusLineTests(V4Case):
    """The compiled ``subagentStatusLine`` through real launches, resumes, doctor and repair."""

    def settings(self, mid: str) -> dict:
        return strict_json.loads((self.live(mid) / scope.SETTINGS_RELPATH).read_bytes())

    def write_settings(self, mid: str, document: dict) -> None:
        state.atomic_write(self.live(mid) / scope.SETTINGS_RELPATH, strict_json.canonical_file_bytes(document))

    def user_layer(self, document: dict | None) -> None:
        path = Path(self.env["HOME"]) / ".claude" / "settings.json"
        if document is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)  # the user's own file, not charmer state
        path.write_text(json.dumps(document), encoding="utf-8")

    def shipped(self) -> dict[str, str]:
        return scope.status_line_setting(scope.status_line_shim_path(self.store.root))

    def integrity(self, record: dict) -> tuple[list[str], list[str]]:
        problems, attention, _info = doctor._check_scope_integrity(self.runtime, record)
        return problems, attention

    def converge(self, mid: str) -> list[str]:
        return transition.converge(self.store.root, self.store, mid, runtime_parts=self.runtime.converge_parts())

    def test_a_launch_compiles_the_stable_shim_and_no_bundle_path(self) -> None:
        for target in (self.profile_target(), self.direct_target()):
            with self.subTest(target=target.kind):
                record = self.launch_fresh(target)
                settings = self.settings(record["managed_id"])
                self.assertEqual(settings["subagentStatusLine"], self.shipped())
                self.assertNotIn("statusLine", settings)  # the main line stays the user's
                shim = scope.status_line_shim_path(self.store.root)
                self.assertTrue(os.access(shim, os.X_OK))
                self.assertIn(shlex.quote(self.runtime.resolved_hook_command).encode(), shim.read_bytes())
                for data in self.tree(self.live(record["managed_id"])).values():
                    self.assertNotIn(self.runtime.resolved_hook_command.encode(), data)

    def test_a_user_owned_key_wins_and_the_next_resume_follows_the_layers(self) -> None:
        self.user_layer({"subagentStatusLine": {"type": "command", "command": "true"}})
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.assertNotIn("subagentStatusLine", self.settings(mid))
        self.assertEqual(self.integrity(record), ([], []))  # nothing drifts under the user's key
        self.user_layer(None)
        self.assertEqual(self.resume(mid)["managed_id"], mid)
        self.assertEqual(self.settings(mid)["subagentStatusLine"], self.shipped())

    def test_doctor_names_the_line_in_effect(self) -> None:
        def line() -> list[str]:
            return [text for text in doctor._doctor_user_settings_report(self.runtime, [])[1]
                    if text.startswith("subagent status line:")]

        self.assertEqual(line(), ["subagent status line: charmer (compiled into managed sessions)"])
        self.user_layer({"subagentStatusLine": {"type": "command", "command": "/home/user/secret-command"}})
        self.assertEqual(line(), ["subagent status line: user-owned (subagentStatusLine in settings.json; "
                                  "charmer compiles none)"])
        self.assertNotIn("secret-command", " ".join(doctor._doctor_user_settings_report(self.runtime, [])[1]))

    def test_a_scope_another_release_compiled_is_lazy_and_repair_converges_it(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        compiled = self.settings(mid)
        earlier = {key: value for key, value in compiled.items() if key != "subagentStatusLine"}
        self.write_settings(mid, earlier)  # as 2.1.0 compiled it
        problems, attention = self.integrity(record)
        self.assertEqual(problems, [])
        self.assertTrue(any(text.startswith(f"session {mid[:8]}: its subagent status line differs") for text in attention),
                        attention)
        before = self.tree(self.live(mid))
        report = self.converge(mid)
        self.assertTrue(any("recompiled from the record and the installed catalog" in line for line in report), report)
        after = self.tree(self.live(mid))
        self.assertEqual({name for name in after if after[name] != before.get(name)}, {scope.SETTINGS_RELPATH})
        self.assertEqual(self.settings(mid), compiled)
        self.assertEqual(self.integrity(record), ([], []))

    def test_repair_drops_it_under_a_user_owned_key(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.user_layer({"subagentStatusLine": {"type": "command", "command": "true"}})
        problems, attention = self.integrity(record)
        self.assertEqual(problems, [])
        self.assertEqual(len([text for text in attention if "subagent status line differs" in text]), 1)
        self.converge(mid)
        self.assertNotIn("subagentStatusLine", self.settings(mid))

    def test_another_status_line_in_the_scope_stays_a_problem(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        edited = {**self.settings(mid), "subagentStatusLine": {"type": "command", "command": "/tmp/other"}}
        self.write_settings(mid, edited)
        problems, attention = self.integrity(record)
        self.assertTrue(any("scope differs from the record-authoritative compile" in text for text in problems),
                        (problems, attention))
        self.assertFalse(any("subagent status line differs" in text for text in attention))


PREVIOUS_RELEASE_ENV = "CHARMER_TEST_PREVIOUS_RELEASE"
PREVIOUS_RELEASE = "2.1.0"


class RollbackToPreviousReleaseTests(V4Case):
    """The retained previous release's own launcher over state this checkout
    wrote, with and without a user-owned ``subagentStatusLine``: it starts,
    lists the sessions, its repair finds the compiled key a scope difference
    and recompiles it (a user-owned key leaves nothing to repair), its pin
    knows the user's key (no settings skew), and its argparse refuses
    ``statusline``, which the shim then answers with nothing, exit 0.

    Only commands that observe no gateway run (never ``doctor`` itself). The
    child gets :func:`isolated_child_environ`: the runner's live-resource
    tripwire armed, the fixture's own endpoint on a free port of the
    new-install range (never the packaged one), and no route to the real
    user session's service manager or bus. Opt-in:
    ``CHARMER_TEST_PREVIOUS_RELEASE`` names a source tree of that release
    (``git archive v2.1.0``); without it the class skips."""

    def setUp(self) -> None:
        tree = os.environ.get(PREVIOUS_RELEASE_ENV)
        if not tree:
            self.skipTest(f"{PREVIOUS_RELEASE_ENV} names no source tree of the previous release")
        self.previous = Path(tree)
        version = json.loads((self.previous / "src/charmer/data/version.json").read_text())["launcher_version"]
        if version != PREVIOUS_RELEASE:
            self.fail(f"{PREVIOUS_RELEASE_ENV} names release {version}, not {PREVIOUS_RELEASE}")
        super().setUp()
        write_fixture_endpoint(Path(self.env["HOME"]))
        self.runtime = self.make_runtime(asset_root=charmer_resources())
        self.environ = isolated_child_environ(self.env, self.root / "runtime")
        assert_guard_armed(self, self.environ)

    def previous_charmer(self, *argv: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.previous / "bin" / "charmer"), *argv], input=stdin,
                              capture_output=True, env=self.environ, cwd=self.project, timeout=120)

    def user_layer(self, document: dict | None) -> Path:
        path = Path(self.env["HOME"]) / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if document is not None:
            path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_the_previous_launcher_starts_and_repairs_over_this_releases_state(self) -> None:
        contract = json.loads((self.previous / "src/charmer/data/catalog/native-contract.json").read_text())
        self.assertIn("subagentStatusLine", contract["verified"][0]["settings_keys"])  # no skew finding
        user_line = {"type": "command", "command": "printf ''"}
        for owned in (False, True):
            with self.subTest(user_owned=owned):
                user = self.user_layer({"subagentStatusLine": user_line} if owned else None)
                record = self.launch_fresh(self.direct_target("opus"))
                mid = record["managed_id"]
                settings_path = self.live(mid) / scope.SETTINGS_RELPATH
                self.assertEqual("subagentStatusLine" in strict_json.loads(settings_path.read_bytes()), not owned)
                user_before = user.read_bytes() if user.exists() else None
                version = self.previous_charmer("--version")
                self.assertEqual(version.returncode, 0, version.stderr)
                self.assertTrue(version.stdout.decode().startswith(f"charmer {PREVIOUS_RELEASE}"), version.stdout)
                listing = self.previous_charmer("sessions", "list", "--all")
                self.assertEqual(listing.returncode, 0, listing.stderr)
                self.assertIn(mid[:8].encode(), listing.stdout)
                repaired = self.previous_charmer("doctor", "--repair", mid)
                self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
                self.assertNotIn(b"Traceback", repaired.stderr)
                report = repaired.stdout.decode()
                if not owned:
                    # The key it does not compile is a scope difference its repair resolves.
                    self.assertIn("live scope drifted from record authority (settings.json content differs)", report)
                    self.assertNotIn("subagentStatusLine", strict_json.loads(settings_path.read_bytes()))
                else:
                    self.assertIn("live scope matches the record-authoritative compile", report)
                again = self.previous_charmer("doctor", "--repair", mid)
                self.assertIn("live scope matches the record-authoritative compile", again.stdout.decode())
                # Its parser refuses the plumbing command; the shim prints nothing and exits 0.
                refused = self.previous_charmer("statusline", "subagents", stdin=json.dumps(TEN_TASKS).encode())
                self.assertEqual((refused.returncode, refused.stdout), (2, b""))
                shim = scope.ensure_status_line_shim(self.store.root, str(self.previous / "bin" / "charmer"))
                answered = subprocess.run(str(shim), input=json.dumps(TEN_TASKS).encode(), capture_output=True,
                                          env=self.environ, timeout=120)
                self.assertEqual((answered.returncode, answered.stdout, answered.stderr), (0, b"", b""))
                self.assertEqual(user.read_bytes() if user.exists() else None, user_before)  # never written
                user.unlink(missing_ok=True)
                print(f"rollback {PREVIOUS_RELEASE}: user-owned={owned} --version ok, sessions list ok, repair: "
                      + next(line for line in report.splitlines() if line.startswith("live scope"))
                      + "; statusline refused (exit 2), shim answered nothing (exit 0)")


class RollbackLaneGuardTests(V4Case):
    """The rollback lane's child environment arms the tripwire and reaches no
    real user session, shown with fabricated events only (no launcher of
    another release runs, no real connection is made)."""

    def test_the_lane_child_environment_arms_the_guard(self) -> None:
        port = write_fixture_endpoint(Path(self.env["HOME"]))
        environ = isolated_child_environ(self.env, self.root / "runtime")
        self.assertNotIn(port, (8316, 8317))
        self.assertTrue({"PYTHONPATH", "CM_ISOLATION_GUARD_LOG"} <= set(environ))
        self.assertEqual(environ["HOME"], self.env["HOME"])
        assert_guard_armed(self, environ)


# The variables that locate the real user session's service manager and bus.
USER_SESSION_ENV = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")


def write_fixture_endpoint(home: Path) -> int:
    """The fixture's own ``endpoint.json``: a free ephemeral loopback port
    the system assigns (bound, then released; nothing listens there, and it
    is never a live gateway port), the on-demand backend."""

    import socket

    from charmer import endpoint

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    endpoint.write_config(home, endpoint.EndpointConfig(port=port))
    config = endpoint.read_config(home)
    assert config is not None and config.port == port and config.backend == endpoint.ON_DEMAND
    assert port not in (8316, 8317)
    return port


def isolated_child_environ(fixture: dict[str, str], runtime_dir: Path) -> dict[str, str]:
    """A child's environment: the runner's (its tripwire: ``PYTHONPATH`` with
    the guard ``sitecustomize`` and ``CM_ISOLATION_GUARD_LOG``) with the
    fixture's HOME and roots, ``XDG_RUNTIME_DIR`` an empty private fixture
    directory and no session bus address, so ``systemctl --user`` and
    ``journalctl --user`` cannot reach the real user session."""

    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    environ = {key: value for key, value in os.environ.items() if key not in USER_SESSION_ENV}
    environ.update(fixture)
    environ.update({"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(runtime_dir)})
    return environ


# Fabricated events only: the connect goes to a fake object (an unguarded
# socket method rejects it with TypeError before any system call), the
# audit events are raised by hand, and no real connection is ever made.
_GUARD_PROBE = """\
import os, socket, sys
results = []
try:
    socket.socket.connect(object(), ("127.0.0.1", 8317))
    results.append("connect:allowed")
except ConnectionRefusedError as exc:
    results.append("connect:refused" if "tripwire" in str(exc) else "connect:other")
except TypeError:
    results.append("connect:unguarded")
results.append("create_connection:" + ("guarded" if getattr(socket.create_connection, "_cm_test_tripwire", False)
                                       else "unguarded"))
for name, event, args in (("proc-net-tcp", "open", ("/proc/net/tcp", "r", 0)),
                          ("systemctl-restart", "subprocess.Popen",
                           ("systemctl", ["systemctl", "--user", "restart", "charmer-gateway"], None, None))):
    try:
        sys.audit(event, *args)
        results.append(name + ":allowed")
    except AssertionError:
        results.append(name + ":refused")
print(",".join(results), os.environ.get("XDG_RUNTIME_DIR", "-"), os.environ.get("DBUS_SESSION_BUS_ADDRESS", "-"))
"""
GUARD_VERDICTS = "connect:refused,create_connection:guarded,proc-net-tcp:refused,systemctl-restart:refused"


def assert_guard_armed(case: unittest.TestCase, environ: dict[str, str]) -> None:
    """A child with the lane's environment (its guard log redirected to a
    probe log, so the deliberate hits never fail the run) refuses a
    fabricated live-gateway connect, a fabricated ``/proc/net/tcp`` read and a
    fabricated ``systemctl --user restart``, logs them, and sees the fixture's
    empty runtime directory and no session bus."""

    log = Path(environ["XDG_RUNTIME_DIR"]).parent / "guard-probe.log"
    probe_environ = {**environ, "CM_ISOLATION_GUARD_LOG": str(log)}
    case.assertEqual(set(probe_environ) - {"CM_ISOLATION_GUARD_LOG"}, set(environ) - {"CM_ISOLATION_GUARD_LOG"})
    result = subprocess.run([sys.executable, "-c", _GUARD_PROBE], capture_output=True, env=probe_environ,
                            timeout=60)
    case.assertEqual(result.returncode, 0, result.stderr)
    verdicts, runtime_dir, bus = result.stdout.decode().split()
    case.assertEqual(verdicts, GUARD_VERDICTS, result.stderr)
    case.assertEqual((runtime_dir, bus), (environ["XDG_RUNTIME_DIR"], "-"))
    case.assertEqual(list(Path(runtime_dir).iterdir()), [])  # no bus socket, no manager
    logged = log.read_text(encoding="utf-8")
    for hit in ("('127.0.0.1', 8317)", "/proc/net/tcp", "systemctl unit control"):
        case.assertIn(hit, logged)
    print(f"guard armed in the lane's child: {verdicts}; XDG_RUNTIME_DIR={runtime_dir} (empty fixture), "
          "DBUS_SESSION_BUS_ADDRESS unset")


def charmer_resources() -> Path:
    import charmer

    return charmer.resources_root()


GOLDEN_COMMAND = "/nix/store/fixture/bin/charmer"
TEN_TASKS = {"columns": 120, "tasks": [
    {**REVIEWER, "id": f"t{n}", "tokenCount": 25_000 * n} for n in range(10)]}
# The charmer modules the shim's real invocation may load: the entry points,
# the plumbing command and the renderer (no parser, runtime, store or catalog).
STATUS_LINE_MODULES = {"charmer", "charmer.entrypoints", "charmer.cli", "charmer.cli.commands",
                       "charmer.cli.commands.statusline", "charmer.statusline", "charmer.selector_prefixes"}
_IMPORTED = re.compile(r"^import time:\s+\d+ \|\s+\d+ \|( *)(\S+)$")


def _tree(root: Path) -> dict[str, tuple[int, int, int, bytes]]:
    """Every entry under ``root``: relpath -> (mode, size, mtime_ns, bytes of a file)."""

    entries = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        entries[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns, data)
    return entries


class StatusLineShimTests(unittest.TestCase):
    """``<state>/bin/charmer-statusline``: its bytes, its writer and its exit-0 contract."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charmer-statusline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"

    def fake_launcher(self, body: str, name: str = "charmer") -> Path:
        path = self.root / "launchers" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_shim(self, launcher: str, stdin: bytes = b"{}", path: str = "/usr/bin:/bin") -> subprocess.CompletedProcess:
        shim = scope.ensure_status_line_shim(self.state, launcher)
        command = scope.status_line_setting(shim)["command"]
        # As the client runs it: through a shell, the document on stdin.
        return subprocess.run(command, shell=True, input=stdin, capture_output=True, timeout=30,
                              env={"PATH": path, "HOME": str(self.root)})

    def test_the_shim_bytes(self) -> None:
        assertGolden(self, GOLDENS_ROOT / "shim" / "charmer-statusline", scope.status_line_shim_text(GOLDEN_COMMAND))
        with self.assertRaises(scope.ScopeError):
            scope.status_line_shim_text("")

    def test_the_writer_is_the_hook_shims_one(self) -> None:
        path = scope.ensure_status_line_shim(self.state, GOLDEN_COMMAND)
        self.assertEqual(path, self.state / "bin" / "charmer-statusline")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        inode = path.stat().st_ino
        path.chmod(0o600)
        scope.ensure_status_line_shim(self.state, GOLDEN_COMMAND)
        self.assertEqual((path.stat().st_ino, stat.S_IMODE(path.stat().st_mode)), (inode, 0o700))  # same bytes
        scope.ensure_status_line_shim(self.state, "/other/bin/charmer")
        self.assertNotEqual(path.stat().st_ino, inode)  # rewritten atomically on change
        self.assertIn(b"launcher=/other/bin/charmer\n", path.read_bytes())

    def test_the_compiled_value_names_only_the_shim(self) -> None:
        setting = scope.status_line_setting(self.root / "a state" / "bin" / "charmer-statusline")
        self.assertEqual(setting["command"], f"'{self.root}/a state/bin/charmer-statusline'")
        self.assertTrue(scope.is_status_line_setting(setting))
        for other in ({"type": "command", "command": "/x/bin/other"}, {"type": "command", "command": "printf ''"},
                      {"type": "command", "command": "/s/bin/charmer-statusline --x"},
                      {"type": "static", "command": "/s/bin/charmer-statusline"},
                      {"type": "command", "command": "/s/bin/charmer-statusline", "padding": 1}, "x", None):
            with self.subTest(other=other):
                self.assertFalse(scope.is_status_line_setting(other))

    def test_rows_are_forwarded_only_on_exit_zero(self) -> None:
        ok = self.fake_launcher('[ "$1 $2" = "statusline subagents" ] || exit 9\n'
                                'printf \'{"id":"a","content":"%s"}\\n{"id":"b","content":"x"}\\n\' "$(wc -c)"\n')
        result = self.run_shim(str(ok), stdin=b"12345")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, b'{"id":"a","content":"5"}\n{"id":"b","content":"x"}\n', b""))

    def test_every_failure_prints_nothing_and_exits_zero(self) -> None:
        older = self.fake_launcher("echo 'usage: charmer ...' >&2\necho partial\nexit 2\n", name="older")
        crash = self.fake_launcher("kill -9 $$\n", name="crash")
        for label, launcher in (("older release", str(older)), ("killed", str(crash)),
                                ("missing, none on PATH", str(self.root / "gone" / "charmer"))):
            with self.subTest(case=label):
                result = self.run_shim(launcher, stdin=json.dumps(TEN_TASKS).encode())
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_a_missing_launcher_falls_back_to_charmer_on_path(self) -> None:
        on_path = self.fake_launcher("echo '{\"id\":\"p\",\"content\":\"path\"}'\n")
        result = self.run_shim(str(self.root / "gone" / "charmer"), path=f"{on_path.parent}:/usr/bin:/bin")
        self.assertEqual((result.returncode, result.stdout), (0, b'{"id":"p","content":"path"}\n'))


class RuntimeStatusLineTests(unittest.TestCase):
    """The Runtime refreshes the shim beside the hook shims and decides the key."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charmer-statusline-rt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.environ = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local/state"),
                        "XDG_CONFIG_HOME": str(self.home / ".config"), "TERM": "dumb",
                        "CHARMER_HOOK_COMMAND": str(self.root / "install" / "bin" / "charmer")}
        self.state = self.home / ".local/state/charmer"

    def runtime(self, **kwargs: object) -> object:
        from charmer import cli
        from _catalog import FIXTURE_ROOT

        with mock.patch.dict(os.environ, self.environ):
            for name in ("CHARMER_CHANNEL", "CHARMER_INHIBITION_TOKEN"):
                os.environ.pop(name, None)
            return cli.Runtime(asset_root=FIXTURE_ROOT, environ=dict(self.environ), cwd=str(self.root / "project"),
                               **kwargs)

    def test_runtime_writes_the_shim_from_the_resolved_launcher(self) -> None:
        runtime = self.runtime()
        shim = self.state / "bin" / "charmer-statusline"
        self.assertEqual(shim.read_bytes(), scope.status_line_shim_text(runtime.resolved_hook_command))
        self.assertEqual(runtime.status_line_setting, scope.status_line_setting(shim))
        self.assertEqual(runtime.subagent_status_line(str(self.root / "project")), scope.status_line_setting(shim))

    def test_a_read_only_runtime_names_the_shim_and_writes_nothing(self) -> None:
        runtime = self.runtime(refresh_shims=False, allow_state_writes=False, initialize_session_store=False)
        self.assertFalse((self.state / "bin" / "charmer-statusline").exists())
        self.assertEqual(runtime.status_line_setting,
                         scope.status_line_setting(self.state / "bin" / "charmer-statusline"))

    def test_a_user_owned_key_in_any_layer_compiles_none(self) -> None:
        runtime = self.runtime(refresh_shims=False, allow_state_writes=False, initialize_session_store=False)
        project = self.root / "project"
        layers = (self.home / ".claude" / "settings.json", project / ".claude" / "settings.json",
                  project / ".claude" / "settings.local.json")
        for layer in layers:
            with self.subTest(layer=str(layer.relative_to(self.root))):
                layer.parent.mkdir(parents=True, exist_ok=True)
                # Any value counts, an empty command (the opt-out) included.
                layer.write_text(json.dumps({"subagentStatusLine": {"type": "command", "command": "printf ''"}}))
                self.assertIsNone(runtime.subagent_status_line(str(project)))
                layer.unlink()
                self.assertIsNotNone(runtime.subagent_status_line(str(project)))
        # Never CLAUDE_CONFIG_DIR, which every managed launch unsets.
        other = self.root / "config-dir"
        other.mkdir()
        (other / "settings.json").write_text(json.dumps({"subagentStatusLine": None}))
        runtime.environ["CLAUDE_CONFIG_DIR"] = str(other)
        self.assertIsNotNone(runtime.subagent_status_line(str(project)))

    def test_an_inhibition_the_run_does_not_own_leaves_the_shim(self) -> None:
        from charmer import gateway_inhibition as gi

        self.runtime()
        shim = self.state / "bin" / "charmer-statusline"
        before = shim.read_bytes()
        token = gi.begin(self.state, owner="cutover", purpose="move to the bundle", phase="freeze",
                         remedy="sh cutover.sh --recover", expiry=gi.deadline(3600))
        self.addCleanup(gi.end, self.state, token)
        self.environ["CHARMER_HOOK_COMMAND"] = str(self.root / "newer" / "bin" / "charmer")
        self.assertTrue(self.runtime().inhibited_shims)
        self.assertEqual(shim.read_bytes(), before)


class RealInvocationTests(unittest.TestCase):
    """The shim through a release-shaped launcher (sh launcher, ``python3 -I
    -B`` bootstrap): the rows, exit 0, the modules it loads, the time it
    takes and that nothing under the state or config root changes."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charmer-statusline-real-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.state = self.home / ".local/state/charmer"
        self.launcher = release_shaped_launcher(self.root / "bundle", sys.executable,
                                                imports_log=self.root / "imports.log")

    def test_the_shim_runs_the_renderer_without_the_runtime(self) -> None:
        shim = scope.ensure_status_line_shim(self.state, str(self.launcher))
        for name in (".config/charmer", ".claude"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        (self.home / ".config/charmer/settings.json").write_text('{"version": 1}\n')
        before = _tree(self.home)
        document = json.dumps(TEN_TASKS).encode()
        times = []
        for _ in range(5):
            (self.root / "imports.log").write_bytes(b"")
            started = time.monotonic()
            result = subprocess.run(scope.status_line_setting(shim)["command"], shell=True, input=document,
                                    capture_output=True, timeout=30,
                                    env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
            times.append(time.monotonic() - started)
            self.assertEqual((result.returncode, result.stderr), (0, b""))
            self.assertEqual(result.stdout, statusline.subagent_output(document))
            self.assertEqual(len(result.stdout.splitlines()), 10)
        loaded = {match.group(2) for line in (self.root / "imports.log").read_text().splitlines()
                  if (match := _IMPORTED.match(line))}
        self.assertEqual({name for name in loaded if name.startswith("charmer")}, STATUS_LINE_MODULES)
        self.assertNotIn("argparse", loaded)  # the command-line parser never loads
        self.assertEqual(_tree(self.home), before)  # nothing under the state or config root changed
        times.sort()
        self.assertLess(times[len(times) // 2], 0.5, times)  # generous; the budget is 60 ms


def release_shaped_launcher(root: Path, interpreter: str, *, imports_log: Path | None = None) -> Path:
    """A release bundle's ``bin/charmer`` over this checkout's package: the
    build's own launcher text, ``interpreter`` as the bundled Python (with
    ``-X importtime`` to ``imports_log`` when given)."""

    bundle = load_tool(REPO_ROOT / "tools/_build/bundle.py", "statusline_bundle")
    version = platform.python_version()
    site = root / bundle.site_dir(version)
    site.mkdir(parents=True)
    (site / "charmer").symlink_to(REPO_ROOT / "src" / "charmer", target_is_directory=True)
    python = root / bundle.RUNTIME_DIR / "bin" / "python3"
    python.parent.mkdir(parents=True)
    trace = " -X importtime" if imports_log is not None else ""
    redirect = f" 2>>{shlex.quote(str(imports_log))}" if imports_log is not None else ""
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(interpreter)}{trace} "$@"{redirect}\n', encoding="utf-8")
    python.chmod(0o755)
    launcher = root / "bin" / "charmer"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(bundle.launcher_text("charmer", version, {"patches": []}))
    launcher.chmod(0o755)
    return launcher


if __name__ == "__main__":
    unittest.main()
