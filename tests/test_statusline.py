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
import random
import re
import unittest

from charmer import statusline

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


if __name__ == "__main__":
    unittest.main()
