"""Tests for the deterministic compiler: agents, lead content, env, argv, goldens."""

from __future__ import annotations

import unittest
from pathlib import Path

from charmer import catalog, compiler, strict_json
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT
from _golden import assertGolden


CATALOG_ROOT = FIXTURE_ROOT
GOLDENS = GOLDENS_ROOT / "default"
FIXED_SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"
SETTINGS_PATH = Path("/trusted/settings.json")
SCOPE_DIR = Path("/state") / "scopes" / FIXED_SESSION


class LeadPromptPathTests(unittest.TestCase):
    def test_session_scoped_and_deterministic(self) -> None:
        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64
        path = compiler.lead_prompt_path(Path("/state"), digest_a, FIXED_SESSION)
        # resume of the same UUID is stable
        self.assertEqual(
            path, compiler.lead_prompt_path(Path("/state"), digest_a, FIXED_SESSION)
        )
        self.assertIn(FIXED_SESSION, path.name)
        self.assertIn(digest_a.removeprefix("sha256:")[:16], path.name)
        # fresh launches with different UUIDs never collide
        self.assertNotEqual(
            path, compiler.lead_prompt_path(Path("/state"), digest_a, OTHER_SESSION)
        )
        # composition transition for the same UUID lands on the new digest
        self.assertNotEqual(
            path, compiler.lead_prompt_path(Path("/state"), digest_b, FIXED_SESSION)
        )


class PassthroughFlagTests(unittest.TestCase):
    def test_disable_slash_commands_is_blocked(self) -> None:
        for token in ("--disable-slash-commands", "--disable-slash-commands=x"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(
                    compiler.CompilerError, "agent-directory watching"
                ):
                    compiler.validate_passthrough([token])

    def test_autocompact_is_blocked(self) -> None:
        for args in (["--autocompact"], ["--autocompact", "1000000"], ["--autocompact=1000000"]):
            with self.subTest(args=args):
                with self.assertRaisesRegex(compiler.CompilerError, "session compaction window"):
                    compiler.validate_passthrough(args)

    def test_the_system_prompt_record_is_launcher_owned(self) -> None:
        for args in (["--system-prompt-snapshot", "on"], ["--system-prompt-snapshot=on"],
                     ["--system-prompt-snapshot", "off"], ["--system-prompt-snapshot"]):
            with self.subTest(args=args):
                with self.assertRaisesRegex(compiler.CompilerError, "system-prompt record policy"):
                    compiler.validate_passthrough(args)

    def test_the_copied_session_mark_is_blocked(self) -> None:
        for args in (["--session-began-as-copy", "1"], ["--session-began-as-copy=1"], ["--session-began-as-copy"]):
            with self.subTest(args=args):
                with self.assertRaisesRegex(compiler.CompilerError, "launcher session identity"):
                    compiler.validate_passthrough(args)
        self.assertEqual(compiler.validate_passthrough(["--verbose"]), ["--verbose"])


class EnvironmentTests(unittest.TestCase):

    def test_the_newer_host_switches_are_cleared_and_reserved(self) -> None:
        """Claude Code 2.1.313+ host switches never reach a managed session
        from the launching shell, and a model's lead env never sets them."""

        switches = ("CLAUDE_CODE_SESSION_BEGAN_AS_COPY", "CLAUDE_CODE_HOST_OWNED_CONFIG_DIR",
                    "CLAUDE_CODE_PERSONAL_HOOKS_OFF", "CLAUDE_CODE_DISABLE_NESTED_SESSION_ID")
        unset = compiler.v2_env_unset(scalar=None, secret_env_names=(), launch_environ={
            name: "1" for name in switches})
        for name in switches:
            with self.subTest(name=name):
                self.assertIn(name, compiler.V2_ENV_UNSET)
                self.assertEqual(unset.count(name), 1)
                self.assertIn(name, catalog.RESERVED_LEAD_ENV_KEYS)
        # Measured and left to the user: the auto-mode server switch fails
        # closed, the feature-flag switch keeps the defaults, and the subagent
        # effort applies only to agents that name none (every cm-* agent does).
        for name in ("CLAUDE_CODE_AUTO_MODE_SERVER", "CLAUDE_CODE_DISABLE_FEATURE_FLAGS",
                     "CLAUDE_CODE_SUBAGENT_EFFORT"):
            with self.subTest(left=name):
                self.assertNotIn(name, compiler.V2_ENV_UNSET)


    def test_family_default_env_rules(self) -> None:
        import copy

        bundle = catalog.load_catalog(CATALOG_ROOT)
        models = copy.deepcopy(bundle.docs["models"]["models"])
        providers = bundle.docs["providers"]["providers"]
        # A missing line omits its variable; a sub-1M line has no [1m].
        del models["fable"]
        models["opus"]["context"]["client_tokens"] = 200_000
        self.assertEqual(
            compiler.family_default_env(models, providers),
            {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8"},
        )
        # A custom synthetic entry is never a family default.
        models["sonnet"] = {**copy.deepcopy(models["opus"]), "family": "custom",
                            "provider": "kimi", "wire_model": "k3"}
        self.assertNotIn(
            "ANTHROPIC_DEFAULT_SONNET_MODEL", compiler.family_default_env(models, providers)
        )
        self.assertEqual(
            [variable for _key, variable in compiler.FAMILY_DEFAULT_LINES],
            ["ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
             "ANTHROPIC_DEFAULT_SONNET_MODEL"],
        )


class ForkLaunchArgvTests(unittest.TestCase):
    """A fork: the parent's conversation copied into a new session id, and
    every launch of a fork with Claude Code's system-prompt record off."""

    def launch(self, action: compiler.SessionAction, **kwargs) -> list[str]:
        from test_compiler_v2 import _seed_launch

        return _seed_launch("balanced", action=action, **kwargs)[1].argv

    def test_the_fork_argv(self) -> None:
        argv = self.launch(compiler.build_fork(FIXED_SESSION, OTHER_SESSION))
        self.assertEqual(argv[:5], ["--resume", OTHER_SESSION, "--fork-session", "--session-id", FIXED_SESSION])
        fresh = self.launch(compiler.build_fresh(FIXED_SESSION))
        # The rest is the fresh launch's, then the record switch, then passthrough.
        self.assertEqual(argv[5:-3], fresh[2:-1])
        self.assertEqual(argv[-3:], ["--system-prompt-snapshot", "off", "--verbose"])

    def test_every_resume_of_a_fork_switches_the_record_off(self) -> None:
        resume = compiler.build_resume(FIXED_SESSION)
        forked = self.launch(resume, fork_record=True)
        self.assertEqual(forked[:2], ["--resume", FIXED_SESSION])
        self.assertEqual(forked[-3:], ["--system-prompt-snapshot", "off", "--verbose"])
        self.assertNotIn("--system-prompt-snapshot", self.launch(resume))
        self.assertNotIn("--system-prompt-snapshot", self.launch(compiler.build_fresh(FIXED_SESSION)))

    def test_a_fork_needs_another_session_to_copy(self) -> None:
        for parent in ("", FIXED_SESSION):
            with self.subTest(parent=parent):
                with self.assertRaises(compiler.CompilerError):
                    compiler.build_fork(FIXED_SESSION, parent)
        with self.assertRaisesRegex(compiler.CompilerError, "runtime id it copies"):
            self.launch(compiler.SessionAction(kind="fork", managed_id=FIXED_SESSION,
                                               runtime_session_id=FIXED_SESSION))


if __name__ == "__main__":
    unittest.main()


class SessionDisplayNameTests(unittest.TestCase):
    """--name gains the project basename; old form without cwd."""

    def test_no_cwd_keeps_plain_prefix(self) -> None:
        self.assertEqual(compiler.session_display_name("cm:default", None), "cm:default")

    def test_root_cwd_keeps_plain_prefix(self) -> None:
        self.assertEqual(compiler.session_display_name("cg:sol", "/"), "cg:sol")

    def test_basename_appended(self) -> None:
        self.assertEqual(
            compiler.session_display_name("cm:kimi-sol", "/home/user/projects/example-app"),
            "cm:kimi-sol@example-app",
        )

    def test_long_basename_capped(self) -> None:
        name = compiler.session_display_name(
            "cg:glm52", "/x/" + "a" * 40
        )
        self.assertEqual(name, "cg:glm52@" + "a" * 24)

    def test_control_characters_stripped(self) -> None:
        name = compiler.session_display_name("cm:default", "/x/evil\x1b[2k\x07dir\n")
        self.assertNotIn("\x1b", name)
        self.assertNotIn("\x07", name)
        self.assertNotIn("\n", name)
        self.assertTrue(name.startswith("cm:default@evil"))


class GrokProfileFenceTests(unittest.TestCase):
    """The grok 500K selector (a fence is the lead set)."""

    def test_grok_selector_carries_no_1m_suffix(self) -> None:
        # 500K is not 1M-class: the selector must not self-classify as
        # extended-context; the window comes from the profile env.
        # On the v2 line's selectors (the v1 view is gone).
        bundle = catalog.load_catalog(CATALOG_ROOT)
        selectors = catalog.line_selectors(bundle.lines["grok46"])
        self.assertTrue(selectors)
        for _effort, selector, _contract in selectors:
            self.assertNotIn("[1m]", selector)
