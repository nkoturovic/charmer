"""Offline upstream-delta measurements over tiny Git histories; no Go or network."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _layout import REPO_ROOT, UPSTREAM_JSON

TOOL = REPO_ROOT / "tools" / "upstream_delta.py"
spec = importlib.util.spec_from_file_location("upstream_delta_under_test", TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

TOP_KEYS = {"format", "tool", "clone", "pinned", "reviewed_through", "target", "cherry_picks", "module_rewrite",
            "modules_patch", "series", "isolated", "candidates", "totals", "review_gap", "watch"}
PATCH_KEYS = {"position", "basename", "sha256", "rewrites", "rejected", "rejected_hunks", "offsets", "offset_hunks",
              "context_reduced", "clean", "already_upstream"}


def patch(path, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                      fromfile="a/" + path if before else "/dev/null",
                                      tofile="b/" + path if after else "/dev/null"))


@unittest.skipUnless(shutil.which("git"), "boundary: git is unavailable")
class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delta-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clone, self.repo = self.root / "upstream", self.root / "recipe"
        self.clone.mkdir()
        (self.repo / "gateway" / "patches").mkdir(parents=True)
        (self.repo / "gateway" / "candidates").mkdir()
        self.env = tool.git_environment(self.clone)
        self.env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test",
                        GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z",
                        GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0")
        self.git("init", "-b", "main")
        self.old_mod = "module example.test/up/v1\n\ngo 1.23\n\nrequire (\n\texample.test/dep v1.0.0\n)\n"
        self.write("go.mod", self.old_mod)
        self.write("go.sum", "fixture only\n")
        self.old_offset = 'package sample\n\nconst Value = "before"\n\n// end\n'
        self.new_offset = self.old_offset.replace('"before"', '"after"')
        self.write("offset.go", self.old_offset)
        self.old_reject = 'package sample\n\nconst Conflict = "original"\n'
        self.write("reject.go", self.old_reject)
        self.write("internal/api/routes.go", 'package api\nfunc routes() {\n v1 := engine.Group("/v1")\n v1.GET("/old", handler)\n mgmt := engine.Group("/v0/management")\n mgmt.GET("/config", handler)\n}\n')
        self.write("internal/registry/models/models.json", json.dumps({
            "claude": [{"id": "keep", "limit": 1}, {"id": "gone"}], "codex-pro": [{"id": "codex", "limit": 1}],
            "other": [{"id": "ignored"}]}))
        self.write("internal/registry/models/codex_client_models.json", json.dumps({"models": [{"slug": "client", "limit": 1}]}))
        self.pin = self.commit("baseline", "v1.0.0")
        self.write("go.mod", self.old_mod.replace("/v1\n", "/v2\n"))
        self.write("offset.go", "// inserted\n// two lines\n" + self.old_offset)
        self.write("reject.go", self.old_reject.replace("original", "upstream"))
        self.listed = self.commit("new module", "v2.0.0")
        self.write("internal/api/routes.go", '''package api
func routes() {
 v1 := engine.Group("/v1")
 v1.GET("/new", handler)
 mgmt := engine.Group("/v0/management")
 mgmt.GET("/config", handler)
 const prefix = "/v8/management"
 v8 := engine.Group(prefix)
 engine.GET(prefix+"/callback", handler)
 v8.PATCH("/config", handler)
 v8.Handle(method, "/config/*path", handler)
 // v8.DELETE("/not-a-route", handler)
}
const endpoint = "https://new.example.test/api"
// https://comment.example.test/not-a-literal
''')
        self.write("internal/api/routes_test.go", 'engine.GET("/test-only", handler)\nconst endpoint = "https://test.example.test/"\n')
        self.write("internal/registry/models/models.json", json.dumps({
            "claude": [{"id": "keep", "limit": 2}, {"id": "new"}], "codex-pro": [{"id": "codex", "limit": 2}]}))
        self.write("internal/registry/models/codex_client_models.json", json.dumps({"models": [{"slug": "client", "limit": 2}]}))
        self.unlisted = self.commit("routes and registry", "v2.0.2")
        self.write("tests/fixture.go", "package fixture\n")
        self.write("extra.go", "package sample\n")
        self.target = self.commit("later change", "v2.0.10")
        self.git("checkout", "-b", "side", self.pin)
        self.write("side.go", "package side\n")
        self.outside = self.commit("side branch")
        self.git("checkout", "main")
        self.clean_source = ('package sample\nimport "example.test/up/v1/thing"\n'
                             'const Module = "example.test/up/v1"\nconst Other = "example.test/up/v10/thing"\n')
        self.recipe = json.loads(UPSTREAM_JSON.read_text())
        self.recipe["upstream"].update(commit=self.pin, version="1.0.0", tag="v1.0.0", module="example.test/up/v1")
        self.recipe["markers"] = []
        self.recipe["series"] = [
            self.entry("clean.patch", patch("clean.go", "", self.clean_source)),
            self.entry("offset.patch", patch("offset.go", self.old_offset, self.new_offset)),
            self.entry("reject.patch", patch("reject.go", self.old_reject, self.old_reject.replace("original", "patched"))),
        ]
        self.recipe["candidates"] = [self.entry("candidate.patch", patch("candidate.go", "", "package sample\n"), candidate=True)]
        self.recipe["vendor"]["modules_patch"] = self.entry("modules.patch", patch(
            "go.mod", self.old_mod, self.old_mod.replace("dep v1.0.0", "dep v1.1.0")))
        del self.recipe["vendor"]["modules_patch"]["admitted"]
        self.review = {"format": 1, "upstream": {"reviewed_through": {"version": "1.0.0", "commit": self.pin}},
                       "fixes": [{"commits": [self.listed[:8], self.outside[:8], "ffeeddcc"]}]}
        self.save_recipe()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.clone), *args], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return result.stdout.decode().strip()

    def write(self, name, content):
        path = self.clone / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, subject, tag=None):
        self.git("add", ".")
        self.git("commit", "-m", subject)
        commit = self.git("rev-parse", "HEAD")
        if tag:
            self.git("tag", tag)
        return commit

    def entry(self, name, content, *, candidate=False):
        (self.repo / "gateway" / ("candidates" if candidate else "patches") / name).write_text(content)
        return {"basename": name, "sha256": hashlib.sha256(content.encode()).hexdigest(), "admitted": not candidate}

    def save_recipe(self):
        (self.repo / "gateway" / "UPSTREAM.json").write_text(json.dumps(self.recipe))
        (self.repo / "gateway" / "upstream-review.json").write_text(json.dumps(self.review))

    def argv(self, *extra):
        return ["--clone", str(self.clone), "--repo", str(self.repo), "--target", "v2.0.10", *extra]

    def measure(self, *extra):
        with tempfile.TemporaryDirectory(dir=self.root) as work:
            result = tool.measure(tool.parser().parse_args(self.argv(*extra)), Path(work))
            self.assertEqual(list(Path(work).rglob("*.rej")), [])
            return result

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-I", str(TOOL), *self.argv(*extra)], cwd=self.root,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)

    def test_counts_rewrites_and_read_only_inputs(self):
        before_clone, before_repo = tool.build.tree_sha256(self.clone), tool.build.tree_sha256(self.repo)
        result = self.measure()
        clean, offset, reject = result["series"]
        self.assertTrue(clean["clean"])
        self.assertEqual(clean["rewrites"], 2)
        self.assertEqual(result["module_rewrite"]["replacements"]["clean.patch"], 2)
        self.assertEqual(offset["offset_hunks"], [{"file": "offset.go", "hunk": 1, "offset": 2}])
        self.assertEqual(reject["rejected_hunks"], [{"file": "reject.go", "hunk": 1}])
        self.assertFalse(reject["already_upstream"])
        self.assertEqual(result["totals"], {"rejected": 1, "offsets": 1, "patches_with_rejects": 1,
                                            "patches_with_offsets": 1, "clean_positions": [1]})
        self.assertEqual(result["modules_patch"], {"basename": "modules.patch", "applies": "clean",
                                                  "go_sum_identical": True,
                                                  "go_mod_requires_changed": {"added": [], "removed": []}})
        self.assertEqual(result["isolated"], [])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(tool.build.tree_sha256(self.clone), before_clone)
        self.assertEqual(tool.build.tree_sha256(self.repo), before_repo)

    def test_cumulative_dependencies_and_isolated_candidates(self):
        self.recipe["series"][2] = self.entry("reject.patch", patch("offset.go", self.new_offset,
                                                                   self.new_offset.replace('"after"', '"final"')))
        self.save_recipe()
        result = self.measure("--mode", "both", "--series", "all")
        self.assertEqual(result["series"][2]["rejected"], 0)
        self.assertEqual(result["isolated"][2]["rejected"], 1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(result["candidates"][0]["clean"])
        alone = self.measure("--mode", "isolated")
        self.assertEqual(alone["series"], [])
        self.assertEqual(alone["isolated"], result["isolated"])
        self.assertEqual(alone["totals"]["rejected"], 1)

    def test_already_upstream_and_no_module_rewrite_on_pin(self):
        self.write("reject.go", self.old_reject.replace("original", "patched"))
        self.commit("already contains third patch", "v2.1.0")
        result = self.measure("--target", "v2.1.0")
        self.assertTrue(result["series"][2]["already_upstream"])
        self.assertEqual(result["series"][2]["rejected"], 1)
        pinned = self.measure("--target", "v1.0.0")
        self.assertEqual(set(pinned["module_rewrite"]["replacements"].values()), {0})
        self.assertEqual(pinned["totals"]["clean_positions"], [1, 2, 3])

    def test_review_gap_release_order_and_prefix_matching(self):
        result = self.measure()["review_gap"]
        self.assertEqual(result, {"from": self.pin, "to": self.target, "commits_total": 3, "reviewed_count": 1,
            "unreviewed": [
                {"commit": self.unlisted[:8], "release": "v2.0.2", "subject": "routes and registry", "files": [
                    "internal/api/routes.go", "internal/registry/models/codex_client_models.json", "internal/registry/models/models.json"]},
                {"commit": self.target[:8], "release": "v2.0.10", "subject": "later change", "files": ["extra.go"]}],
            "fixes_outside_range": [self.outside[:8]], "fixes_unknown": ["ffeeddcc"]})
        self.write("unreleased.go", "package sample\n")
        commit = self.commit("unreleased")
        result = self.measure("--target", commit)
        self.assertIsNone(result["target"]["tag"])
        self.assertEqual(result["review_gap"]["unreviewed"][-1]["release"], "unreleased")

    def test_static_watch_routes_urls_and_both_catalogs(self):
        result = self.measure()["watch"]
        self.assertIn({"method": "GET", "path": "/v1/new"}, result["routes_added"])
        self.assertEqual(result["routes_removed"], [{"method": "GET", "path": "/v1/old"}])
        self.assertEqual(result["management_routes"], {"v0": 1, "v8": 3})
        self.assertEqual(result["url_literals_added"], ["https://new.example.test/api"])
        self.assertEqual(result["registry"], {
            "claude": {"added": ["new"], "removed": ["gone"], "changed": {"keep": ["limit"]}},
            "codex-pro": {"added": [], "removed": [], "changed": {"codex": ["limit"]}},
            "codex_client_models": {"added": [], "removed": [], "changed": {"client": ["limit"]}}})
        self.assertFalse(any("test" in row["path"] or "not-a-route" in row["path"] for row in result["routes_added"]))

    def test_partial_clone_refused_even_with_fetch(self):
        for key, value in (("extensions.partialclone", "origin"), ("remote.origin.promisor", "true"),
                           ("remote.origin.promisor", "false")):
            with self.subTest(key=key, value=value):
                self.git("config", key, value)
                for fetch in ([], ["--fetch"]):
                    result = self.cli(*fetch)
                    self.assertEqual(result.returncode, 3, result.stderr)
                    self.assertIn(b"partial clone refused", result.stderr)
                self.git("config", "--unset", key)

    def test_no_network_commands_and_scrubbed_git_environment(self):
        run = subprocess.run
        with mock.patch.dict(os.environ, {"GIT_DIR": "/not/the/clone", "GIT_WORK_TREE": "/not/the/tree",
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                          "GIT_CONFIG_VALUE_0": "/unexpected"}), mock.patch.object(tool.subprocess, "run", wraps=run) as calls:
            self.measure()
        for call in calls.call_args_list:
            argv, env = call.args[0], call.kwargs["env"]
            self.assertEqual(argv[0], "git")
            self.assertNotIn(argv[argv.index("-C") + 2], {"clone", "fetch", "ls-remote"})
            self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["LC_ALL"], "C")
            self.assertNotIn("GIT_DIR", env)
            self.assertNotIn("GIT_WORK_TREE", env)
            self.assertNotIn("GIT_CONFIG_COUNT", env)

    def test_fetch_file_origin_is_the_only_network_enabled_command(self):
        origin = self.root / "origin.git"
        self.git("clone", "--bare", str(self.clone), str(origin))
        self.git("remote", "add", "origin", origin.as_uri())
        run = subprocess.run
        with mock.patch.object(tool.subprocess, "run", wraps=run) as calls:
            result = self.measure("--fetch")
        self.assertTrue(result["clone"]["fetched"])
        enabled = [call for call in calls.call_args_list if "GIT_ALLOW_PROTOCOL" not in call.kwargs["env"]]
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0].args[0][-4:], ["fetch", "--tags", "--prune", "origin"])

    def test_sha_mismatch_and_recipe_errors_exit_four(self):
        (self.repo / "gateway" / "patches" / "clean.patch").write_text("changed\n")
        result = self.cli()
        self.assertEqual(result.returncode, 4)
        self.assertIn(b"sha256 mismatch", result.stderr)
        self.recipe["format"] = 2
        self.save_recipe()
        self.assertEqual(self.cli().returncode, 4)
        self.recipe["format"] = 1
        self.recipe["series"][0]["admitted"] = False
        self.save_recipe()
        self.assertEqual(self.cli().returncode, 4)

    def test_malformed_recipe_shapes_are_recipe_errors(self):
        for section, value in (("upstream", []), ("series", [None]), ("vendor", None),
                               ("upstream", {"commit": self.pin, "module": "example.test/up/v1"}),
                               ("upstream", {"commit": self.pin, "version": "1.0.0", "module": []})):
            with self.subTest(section=section):
                original = self.recipe[section]
                self.recipe[section] = value
                self.save_recipe()
                result = self.cli()
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
                self.recipe[section] = original
        self.review["upstream"]["reviewed_through"]["extra"] = True
        self.save_recipe()
        result = self.cli()
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(b"reviewed_through needs exactly", result.stderr)

    def test_module_changes_and_modules_patch_classification(self):
        self.write("go.mod", self.old_mod.replace("/v1\n", "/v2\n").replace("dep v1.0.0", "dep v2.0.0"))
        self.write("go.sum", "changed upstream sums\n")
        changed = self.commit("dependencies change")
        result = self.measure("--target", changed)["modules_patch"]
        self.assertEqual(result["applies"], "rejects")
        self.assertFalse(result["go_sum_identical"])
        self.assertEqual(result["go_mod_requires_changed"], {"added": ["example.test/dep v2.0.0"],
                                                          "removed": ["example.test/dep v1.0.0"]})
        self.write("go.mod", "// leading comment\n" + self.old_mod.replace("/v1\n", "/v2\n"))
        shifted = self.commit("dependencies moved")
        self.assertEqual(self.measure("--target", shifted)["modules_patch"]["applies"], "offsets")
        del self.recipe["vendor"]["modules_patch"]
        self.save_recipe()
        missing = self.measure()["modules_patch"]
        self.assertIsNone(missing["basename"])
        self.assertIsNone(missing["applies"])

    def test_shallow_clone_refused_without_fetch(self):
        shallow = self.root / "shallow"
        self.git("clone", "--depth", "1", self.clone.as_uri(), str(shallow))
        result = self.cli("--clone", str(shallow))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b"shallow clone refused", result.stderr)

    def test_untrusted_checkout_and_cwd_never_supply_python_imports(self):
        poison = 'raise RuntimeError("untrusted code executed")\n'
        (self.root / "json.py").write_text(poison)
        (self.clone / "json.py").write_text(poison)
        (self.clone / "build.py").write_text(poison)
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["totals"]["rejected"], 1)
        self.assertFalse((self.clone / "__pycache__").exists())
        self.assertFalse((self.root / "__pycache__").exists())

    def test_bad_clone_ref_and_usage_exit_codes(self):
        self.assertEqual(self.cli("--clone", str(self.root / "absent")).returncode, 3)
        self.assertEqual(self.cli("--target", "missing").returncode, 3)
        self.assertEqual(self.cli("--target", "--help").returncode, 2)
        self.assertEqual(self.cli("--mode", "unknown").returncode, 2)
        self.assertEqual(self.cli("--out", str(self.repo / "output.json")).returncode, 2)
        self.assertFalse((self.repo / "output.json").exists())

    def test_cherry_picks_in_order_conflict_and_cleanup(self):
        self.git("checkout", "-b", "picks")
        self.write("pick.go", 'package sample\nconst Pick = "first"\n')
        first = self.commit("first pick")
        self.write("pick.go", 'package sample\nconst Pick = "second"\n')
        second = self.commit("second pick")
        self.git("checkout", "main")
        result = self.measure("--cherry-pick", first, "--cherry-pick", second, "--mode", "both")
        self.assertEqual(result["cherry_picks"], [{"commit": first, "subject": "first pick"},
                                                 {"commit": second, "subject": "second pick"}])
        directories = []
        tempdir = tempfile.TemporaryDirectory

        def capture_temp(*args, **kwargs):
            temporary = tempdir(*args, dir=self.root, **kwargs)
            directories.append(Path(temporary.name))
            return temporary

        with mock.patch.object(tool.tempfile, "TemporaryDirectory", side_effect=capture_temp), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main(self.argv("--cherry-pick", second)), 5)
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))
        self.assertEqual(list(self.root.rglob("*.rej")), [])

    def test_closed_json_shape_and_byte_determinism_from_another_cwd(self):
        first, second = self.cli(), self.cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        result = json.loads(first.stdout)
        self.assertEqual(first.stdout, (json.dumps(result, sort_keys=True, indent=1) + "\n").encode())
        self.assertEqual(set(result), TOP_KEYS)
        self.assertEqual(set(result["clone"]), {"path", "partial", "fetched"})
        self.assertEqual(set(result["target"]), {"ref", "commit", "tag", "committer_date", "module"})
        self.assertEqual(set(result["module_rewrite"]), {"from", "to", "replacements"})
        self.assertEqual(set(result["review_gap"]), {"from", "to", "commits_total", "reviewed_count", "unreviewed",
                                                  "fixes_outside_range", "fixes_unknown"})
        self.assertEqual(set(result["watch"]), {"routes_added", "routes_removed", "management_routes", "url_literals_added", "registry"})
        for row in result["series"]:
            self.assertEqual(set(row), PATCH_KEYS)
        output = self.root / "result.json"
        written = self.cli("--out", str(output))
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(written.stdout, b"")
        self.assertEqual(output.read_bytes(), first.stdout)


class ApplyParserTests(unittest.TestCase):
    def test_files_for_rejects_are_from_the_apply_pass(self):
        output = """Checking patch first.go...
Hunk #2 succeeded at 40 (offset -3 lines).
Checking patch second.go...
Hunk #3 succeeded at 9 (offset 1 line).
Context reduced to (1/1) to apply fragment at 9
Applying patch first.go with 1 reject...
Rejected hunk #1.
Applying patch second.go with 2 rejects...
Rejected hunk #1.
Rejected hunk #2.
"""
        rejected, offsets, reduced = tool.parse_apply(output)
        self.assertEqual(rejected, [{"file": "first.go", "hunk": 1}, {"file": "second.go", "hunk": 1},
                                    {"file": "second.go", "hunk": 2}])
        self.assertEqual(offsets, [{"file": "first.go", "hunk": 2, "offset": -3},
                                   {"file": "second.go", "hunk": 3, "offset": 1}])
        self.assertEqual(reduced, 1)
        self.assertEqual(len(offsets) + reduced, len(tool.OFFSET_REPORT.findall(output)))

    def test_requires_reports_only_dependency_lines(self):
        self.assertEqual(tool.requires("module example.test/up\nrequire a.test/b v1\nrequire (\n c.test/d v2 // indirect\n)\n"),
                         {"a.test/b v1", "c.test/d v2 // indirect"})


if __name__ == "__main__":
    unittest.main()
