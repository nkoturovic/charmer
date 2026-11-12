"""An ordinary split pending change remains resumable by the previous release."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from charmer import state
from test_lineup import LineupCase
from test_statusline import isolated_child_environ, write_fixture_endpoint


_PREVIOUS = "CHARMER_TEST_PREVIOUS_RELEASE"
_CHILD = r"""
import json
from pathlib import Path
import sys

previous, manifest = map(Path, sys.argv[1:])
sys.path[:0] = [str(previous / "src"), str(previous / "tests")]
import charmer
assert Path(charmer.__file__).resolve().is_relative_to((previous / "src").resolve())
from _v4 import V4Case

data = json.loads(manifest.read_text())
case = V4Case()
case.root = Path(data["root"])
case.project = Path(data["project"])
case.env = data["environ"]
case.fake_claude = data["fake_claude"]
case.execs = []
case.runtime = case.make_runtime()
before = case.store.load(data["managed_id"])
assert before["pending"]["kind"] == "profile"
assert before["lineup_generation"] == 2
after = case.resume(data["managed_id"])
assert "pending" not in after
assert after["applied"]["lead"]["key"] == "grok46"
assert after["profile"] == "split" and after["follow"]
assert len(case.execs) == 1
print("previous runtime: ordinary pending consumed; profile lead applied; fake exec only")
"""


class PreviousReleaseSplitResumeTests(LineupCase):
    def setUp(self):
        tree = os.environ.get(_PREVIOUS)
        if not tree:
            self.skipTest(f"BOUNDARY: {_PREVIOUS} names no 2.4.0 source tree for split resume")
        self.previous = Path(tree)
        version = json.loads((self.previous / "src/charmer/data/version.json").read_text())["launcher_version"]
        self.assertEqual(version, "2.4.0", f"{_PREVIOUS} must name the 2.4.0 source tree for this lane")
        super().setUp()

    def test_previous_release_consumes_split_pending_and_current_reader_accepts_it(self):
        credential = self.root / "fixture-secret.env"
        state.atomic_write(credential, b"OPENROUTER_CLAUDE_API_KEY=fixture-dummy\n")
        self.env["CHARMER_SECRET_ENV"] = str(credential)
        self.runtime.environ["CHARMER_SECRET_ENV"] = str(credential)

        def edit(document):
            document["lead"] = {"model": "grok46", "effort": "high"}
            document["agents"]["cm-implementer"] = {"model": "opus55", "effort": "xhigh"}

        self.save_profile("split", edit)
        code, output, error = self.request("profile split")
        self.assertEqual((code, error), (0, ""), output)
        record = self.store.load(self.mid)
        self.assertEqual(record["lineup_generation"], 2)
        self.assertEqual(record["pending"]["kind"], "profile")
        manifest = self.root / "split-input.json"
        state.atomic_write(manifest, json.dumps({
            "root": str(self.root), "project": str(self.project), "environ": self.env,
            "fake_claude": self.fake_claude, "managed_id": self.mid,
        }).encode())
        write_fixture_endpoint(Path(self.env["HOME"]))
        environ = isolated_child_environ(self.env, self.root / "previous-runtime")
        child = subprocess.run([sys.executable, "-c", _CHILD, str(self.previous), str(manifest)],
                               cwd=self.project, env=environ, capture_output=True, text=True, timeout=120)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertIn("ordinary pending consumed", child.stdout)
        after = self.store.load(self.mid)
        self.assertNotIn("pending", after)
        self.assertEqual(after["applied"]["lead"]["key"], "grok46")
        print(child.stdout.strip())
