"""Source-level packaging and cutover assertions for charmer v2.

The repository-layout assertions need ``flake.nix``/``flake.lock``, so they
skip with an explicit boundary inside the Nix sandbox test derivation, whose
staged tree (``tests/source-tree.nix``) carries neither. They run in the
source checkout and in the flake-verify path copies. Every path comes from
``tests/_layout.py``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path


from _layout import FLAKE_NIX as _FLAKE, NIX_DIR, PATCH_DIR, REPO_ROOT, RESOURCES_ROOT
from _layout import UPSTREAM_JSON, UPSTREAM_REVIEW, installed_paths, load_tool, logical_path, pyproject, tree_files
from charmer import legacy

# None inside the Nix sandbox tree, which stages no flake.nix/flake.lock.
FLAKE_REPO = REPO_ROOT if _FLAKE.is_file() else None
FLAKE_NIX = FLAKE_REPO / "flake.nix" if FLAKE_REPO else None


REMOVED_IDENTIFIERS = (
    "charmer-plugin",
    *legacy.LEGACY_NIX_ATTRIBUTES,
    "conserve-opus",
    "conserve-kimi",
    "fable-specialist",
    "sol-explorer",
    "sol-reasoner",
    "sol-engineer",
    "kimi-analyst",
    "kimi-specialist",
    "gpt55-reviewer",
    "opus-reviewer",
    "charmer:orchestrator",
)

REMOVED_ALIASES = (
    "charmer-fable-5",
    "charmer-sol-high",
    "charmer-sol-xhigh",
    "charmer-gpt55-high",
)


def recipe():
    """The gateway recipe: the one ordered patch list, pins and Go gates."""
    return json.loads(UPSTREAM_JSON.read_text())


def admitted_names(document=None):
    document = recipe() if document is None else document
    return [entry["basename"] for entry in document["series"] if entry["admitted"]]


def gate(name, document=None):
    document = recipe() if document is None else document
    return next(record for record in document["gates"] if record["name"] == name)


# The files whose bytes define the gateway build: the recipe and the tool.
RECIPE_FILES = ("gateway/UPSTREAM.json", "tools/build.py")

# Admission ids are stable and never inferred from a patch's position or the
# series length: 11 was never admitted, 14 retired with the 8.0.33 rebase, 30
# was withdrawn, 24 and 25 retired with the 8.0.42 rebase (upstream carries
# them), and 33 with the 8.0.44 rebase (upstream 21553509). The keyed proof
# numbers each shipped patch by this map.
RETIRED_PATCH_NUMBERS = (11, 14, 24, 25, 33)
PATCH_IDS = {
    "cli-proxy-api-loopback-oauth.patch": 1, "cli-proxy-api-kimi-claude-compat.patch": 2,
    "cli-proxy-api-non-claude-cache-retention.patch": 3, "cli-proxy-api-management-readonly-allowlist.patch": 4,
    "cli-proxy-api-watcher-parentdir.patch": 5, "cli-proxy-api-serve-after-initial-auth-load.patch": 6,
    "cli-proxy-api-management-env-only.patch": 7, "cli-proxy-api-credential-save-report.patch": 8,
    "cli-proxy-api-credentialed-redirects.patch": 9, "cli-proxy-api-openai-compat-keyed-safety.patch": 10,
    "cli-proxy-api-auth-snapshot-locking.patch": 12, "cli-proxy-api-server-config-snapshot.patch": 13,
    "cli-proxy-api-claude-metadata-locking.patch": 15, "cli-proxy-api-oauth-model-overlay.patch": 16,
    "cli-proxy-api-no-antigravity-egress.patch": 17, "cli-proxy-api-codex-client-identity.patch": 18,
    "cli-proxy-api-antigravity-loopback-callback.patch": 19, "cli-proxy-api-codex-api-key-safety.patch": 20,
    "cli-proxy-api-refresh-shutdown-join.patch": 21, "cli-proxy-api-openai-content-chunks.patch": 22,
    "cli-proxy-api-charmer-model-ids.patch": 23, "cli-proxy-api-output-cap.patch": 26,
    "cli-proxy-api-context-error-normalization.patch": 27, "cli-proxy-api-log-reduction.patch": 28,
    "cli-proxy-api-tool-call-integrity.patch": 29, "cli-proxy-api-initial-registration.patch": 31,
    "cli-proxy-api-terminal-rejection-reload.patch": 32,
}


class KeyedAuditInvariantTests(unittest.TestCase):
    """Reviewed evidence, not runtime policy; valid in a copied/sandbox tree.

    Source paths are repository-relative; immutable store identities remain
    exact. All current source checks use REPO_ROOT; neither operator state
    nor host evidence is read. The shipped flag is true only with a current
    proof, otherwise false: an open flag requires the complete current proof,
    not just a matching gateway version or patch basename list.
    """

    def setUp(self):
        self.fixture = json.loads((REPO_ROOT / "tests/fixtures/keyed-compat-audit.json").read_text())
        self.gateway = json.loads((RESOURCES_ROOT / "catalog/gateway.json").read_text())
        # The binding mechanics are tested on an explicitly opened copy, so a
        # sanctioned audit=false (stale proof) leaves them meaningful and green.
        self.opened = {**self.gateway, "audits": {**self.gateway.get("audits", {}), "openai_compat_keyed": True}}

    def bound_document(self, entry):
        self.assertEqual(set(entry), {"document", "sha256"})
        raw = (json.dumps(entry["document"], indent=2, sort_keys=True) + "\n").encode()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
        return entry["document"]

    def validate_audit(self, *, fixture=None, gateway=None, changed_bytes=None):
        import _gateway_harness as harness
        from charmer import catalog

        fixture = self.fixture if fixture is None else fixture
        gateway = self.gateway if gateway is None else gateway
        if not catalog.keyed_compat_audited(gateway):
            return False
        changed_bytes = changed_bytes or {}

        def content(name):
            return changed_bytes[name] if name in changed_bytes else logical_path(name).read_bytes()

        def digest(name):
            return hashlib.sha256(content(name)).hexdigest()

        self.assertEqual(set(fixture), {"version", "route", "gateway", "client", "required_patch_numbers",
            "excluded_patch_numbers", "route_dependencies", "package_root", "source_report", "evidence"})
        self.assertEqual(fixture["version"], 1)
        self.assertEqual(fixture["route"], {"kind": "openai-compatible", "adapter": "cliproxy-openai-compat-v1",
            "transport": "direct-openai", "auth": "bearer", "scheme": "https",
            "ingress": "/v1/messages", "upstream": "/chat/completions"})
        build = fixture["gateway"]
        self.assertEqual(digest("nix/gateway.nix"), build["build_definition_sha256"])
        # The recipe and the build tool define the gateway bytes (official Go
        # toolchain archive, source, vendored modules, flags), so both are bound.
        self.assertEqual(build["recipe_sha256"], {name: digest(name) for name in RECIPE_FILES})
        upstream = json.loads(content("gateway/UPSTREAM.json"))
        version = upstream["upstream"]["version"]
        self.assertEqual(build["upstream_version"], version)
        self.assertEqual(build["source_tag"], "v" + version)
        self.assertEqual(build["source_hash"], upstream["source"]["nix_hash"])
        self.assertEqual(build["vendor_hash"], upstream["vendor"]["nix_hash"])
        self.assertEqual(build["consumed_go_modules"], harness.PINNED_GO_MODULES)
        names = admitted_names(upstream)
        self.assertEqual(gateway["gateway"]["patches"], names)
        identities = [name + "@" + digest("gateway/patches/" + name) for name in names]
        manifest = {"version": 2, "upstream_version": version, "patches": identities,
                    "go_version": upstream["toolchain"]["version"], "godebug": upstream["build"]["godebug"]}
        self.assertEqual(build["ordered_manifest"], manifest)
        from charmer import qualify
        self.assertEqual(qualify.parse_gateway_contract(content("gateway-contract.json")), manifest)
        # Each shipped patch carries its reviewed admission id, never one
        # inferred from its position or the series length.
        self.assertLessEqual(set(names), set(PATCH_IDS))
        self.assertEqual(build["numbered_to_shipped"], [
            {"number": PATCH_IDS[name], "identity": identity} for name, identity in zip(names, identities)])
        self.assertEqual(fixture["required_patch_numbers"], [3, 9, 10])
        self.assertEqual(fixture["excluded_patch_numbers"], list(RETIRED_PATCH_NUMBERS))
        for index, name in ((2, "non-claude-cache-retention"), (8, "credentialed-redirects"),
                            (9, "openai-compat-keyed-safety")):
            self.assertEqual(names[index], "cli-proxy-api-" + name + ".patch")
        self.assertFalse(any("retry-after" in name for name in names))
        self.assertTrue(fixture["route_dependencies"])
        self.assertEqual(set(build["binary_sha256"]), {build[role] for role in
            ("gateway", "startup", "executor_omission", "handlers_omission")})
        for value in build["binary_sha256"].values():
            self.assertRegex(value, r"^[a-f0-9]{64}$")

        client = json.loads(content("catalog/native-contract.json"))["verified"][0]
        self.assertEqual(fixture["client"], {"version": client["version"],
            "sha256": client["platforms"]["linux-x64"]["sha256"],
            "native_contract_file_sha256": digest("catalog/native-contract.json")})
        sources = {name: digest(name) for name in harness.KEYED_RELEVANT_SOURCES}
        report = self.bound_document(fixture["source_report"])
        self.assertEqual(set(report), {"head", "evidence_dir", "relevant_sources"})
        self.assertRegex(report["head"], r"^[a-f0-9]{40}$")
        self.assertEqual(fixture["package_root"], ".")
        self.assertEqual(report["relevant_sources"], [{"path": logical_path(name).relative_to(REPO_ROOT).as_posix(),
            "sha256": sources[name]} for name in harness.KEYED_RELEVANT_SOURCES])
        evidence = fixture["evidence"]
        self.assertEqual(evidence["validated_exit"], 0)
        self.assertEqual(report["evidence_dir"], evidence["directory"])
        self.assertEqual(set(evidence["files"]), {"inventory.json", "tests.keyed_journeys.json",
            "tests.test_gateway_keyed_compat.json", "tests.test_keyed_compat_client.json"})
        documents = {name: self.bound_document(entry) for name, entry in evidence["files"].items()}
        identity = {"gateway": build["gateway"], "gateway_sha256": build["binary_sha256"][build["gateway"]],
            "sources": sources, "gateway_patch_names": names,
            "client_contract_sha256": fixture["client"]["native_contract_file_sha256"]}
        inventory = documents["inventory.json"]
        self.assertEqual(inventory["schema"], "keyed-complete-proof-v1")
        self.assertEqual(inventory["status"], "passed")
        self.assertEqual(inventory["head"], report["head"])
        self.assertEqual(inventory["keyed_identity"], identity)
        self.assertEqual(inventory["relevant_sources"], report["relevant_sources"])
        self.assertEqual(inventory["client_sha256"], fixture["client"]["sha256"])
        for count in ("failures", "errors", "skipped"):
            self.assertEqual(inventory[count], 0)
        self.assertIs(inventory["production_audit_enabled"], False)
        self.assertLessEqual(inventory["keyed_client_seconds"], inventory["keyed_client_budget_seconds"])
        self.assertEqual(inventory["keyed_client_budget_seconds"], 900)
        expected = dict(harness.KEYED_INVENTORIES)
        expected["J1"] = {
            "tests.test_portability.KeyedPortabilityTests." + name for name in (
                "test_keyed_json_export_has_logical_refs_and_no_store_reads",
                "test_keyed_import_closed_target_refuses_audit",
                "test_keyed_import_open_target_requires_local_route_and_key_not_admission",
                "test_keyed_import_rechecks_headers_origins_collisions",
                "test_keyed_import_uses_existing_single_commit_barrier")}
        expected["J1"].update({
            "tests.test_packaging.PackageSourceTests.test_package_payload_excludes_operator_credentials_and_grants",
            "tests.test_onboarding_isolation.KeyedJourneyTests.test_keyed_cli_declare_approve_apply_admit_qualify_bind",
            "tests.test_onboarding_isolation.KeyedJourneyTests.test_keyed_cli_changes_and_failed_qualification_remain_selectable_with_warnings",
            "tests.test_tui_pty.KeyedJourneyPTYTests.test_keyed_tui_optional_admission_qualification_and_binding"})
        self.assertEqual(set(inventory["inventory"]), set(expected))
        for module, tables in (("tests.test_gateway_keyed_compat", {"core", "audit"}),
                               ("tests.test_keyed_compat_client", {"client"}), ("tests.keyed_journeys", {"J1"})):
            document = documents[module + ".json"]
            self.assertEqual(document["binary"], {"realpath": build["gateway"], "version": version})
            if tables == {"J1"}:
                harness.validate_evidence_document(document, module)
                self.assertTrue(all(row["head"] == report["head"] for row in document["tables"]["J1"]))
            else:
                harness.validate_keyed_document(document, tables, identity=identity)
            self.assertEqual(set(document["tables"]), tables)
            for table in tables:
                rows = document["tables"][table]
                self.assertEqual(len(rows), len(expected[table]))
                self.assertEqual({row["row"] for row in rows}, expected[table])
                self.assertTrue(all(row["status"] == "passed" for row in rows))
                self.assertEqual(inventory["inventory"][table], {"present": sorted(expected[table]),
                    "missing": [], "skipped": [], "count": len(rows)})
        return True

    def test_keyed_audit_flag_bound_to_audited_patch_identities(self):
        from charmer import catalog

        # Shipped open flag must match the fixture; a closed flag grants nothing.
        if catalog.keyed_compat_audited(self.gateway):
            self.assertTrue(self.validate_audit())
        else:
            self.assertFalse(self.validate_audit())

    def test_keyed_audit_fixture_has_portable_provenance(self):
        self.assertEqual(self.fixture["package_root"], ".")
        report = self.bound_document(self.fixture["source_report"])
        for row in report["relevant_sources"]:
            path = Path(row["path"])
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertTrue((REPO_ROOT / path).is_file())
        self.assertEqual(report["evidence_dir"], "tests/fixtures")
        self.assertEqual(self.fixture["evidence"]["directory"], "tests/fixtures")

        def check(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    check(key)
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            elif isinstance(value, str):
                self.assertNotRegex(value, r"/home/|/Users/|/tmp/|\.claude/|~[/\\]")
                if value.startswith("/") and value not in ("/v1/messages", "/chat/completions"):
                    self.assertTrue(value.startswith("/nix/store/"), value)
        check(self.fixture)

    def test_keyed_audit_rejects_gateway_recipe_change(self):
        # An intact open proof must pass before a deliberate mutation, so stale
        # evidence cannot make a negative control pass at an earlier identity.
        self.validate_audit()
        for name in RECIPE_FILES:
            with self.subTest(source=name), self.assertRaises(AssertionError):
                self.validate_audit(gateway=self.opened, changed_bytes={name: (REPO_ROOT / name).read_bytes() + b"\n"})

    def test_keyed_audit_rejects_content_change_same_basename(self):
        import _gateway_harness as harness

        self.validate_audit()
        for patch in self.fixture["gateway"]["ordered_manifest"]["patches"]:
            name = "gateway/patches/" + patch.split("@", 1)[0]
            with self.subTest(patch=name), self.assertRaises(AssertionError):
                self.validate_audit(gateway=self.opened, changed_bytes={name: (REPO_ROOT / name).read_bytes() + b"\n"})
        for name in (*harness.KEYED_RELEVANT_SOURCES,
                     "nix/gateway.nix", "catalog/native-contract.json"):
            with self.subTest(source=name), self.assertRaises(AssertionError):
                self.validate_audit(gateway=self.opened, changed_bytes={name: logical_path(name).read_bytes() + b"\n"})

    def test_keyed_audit_requires_full_order_and_no_patch_eleven(self):
        self.validate_audit()
        for change in ("reverse", "remove", "eleven"):
            gateway = json.loads(json.dumps(self.opened))
            names = gateway["gateway"]["patches"]
            if change == "reverse":
                names.reverse()
            elif change == "remove":
                names.pop()
            else:
                names.insert(10, "cli-proxy-api-retry-after.patch")
            with self.subTest(change=change), self.assertRaises(AssertionError):
                self.validate_audit(gateway=gateway)

    def test_keyed_audit_requires_complete_matching_evidence(self):
        import copy

        self.validate_audit()
        for change in ("missing", "skipped", "duplicate", "identity", "head", "digest", "journey"):
            fixture = copy.deepcopy(self.fixture)
            entry = fixture["evidence"]["files"]["tests.test_gateway_keyed_compat.json"]
            rows = entry["document"]["tables"]["audit"]
            if change == "missing":
                rows.pop()
            elif change == "skipped":
                rows[0]["status"] = "skipped"
            elif change == "duplicate":
                rows.append(copy.deepcopy(rows[0]))
            elif change == "identity":
                entry["document"]["keyed_identity"]["gateway_sha256"] = "0" * 64
            elif change == "head":
                entry = fixture["evidence"]["files"]["inventory.json"]
                entry["document"]["head"] = "0" * 40
            elif change == "journey":
                entry = fixture["evidence"]["files"]["tests.keyed_journeys.json"]
                entry["document"]["tables"]["J1"].pop()
            else:
                entry["sha256"] = "0" * 64
            if change != "digest":  # also exercise structural validation, not only the digest
                raw = (json.dumps(entry["document"], indent=2, sort_keys=True) + "\n").encode()
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
            with self.subTest(change=change), self.assertRaises(AssertionError):
                self.validate_audit(fixture=fixture, gateway=self.opened)

    def test_keyed_audit_false_or_absent_grants_nothing(self):
        from charmer import catalog

        for audit in ({}, {"openai_compat_keyed": False}):
            gateway = {**self.gateway, "audits": audit}
            self.assertFalse(catalog.keyed_compat_audited(gateway))
            self.assertFalse(self.validate_audit(gateway=gateway, fixture={}))
        gateway = {key: value for key, value in self.gateway.items() if key != "audits"}
        self.assertFalse(self.validate_audit(gateway=gateway, fixture={}))


class EntryPointTests(unittest.TestCase):
    def test_source_launcher_version_matches_launcher_version(self) -> None:
        # The one-model session is `charmer direct`; no separate
        # launcher, in a checkout or installed.
        self.assertFalse((REPO_ROOT / "bin" / "claude-gateway").exists())
        gateway = REPO_ROOT / "bin" / "charmer"
        if not gateway.is_file():
            self.skipTest("boundary: source entrypoints are absent in sandbox package tree")
        expected = json.loads((RESOURCES_ROOT / "version.json").read_text())[
            "launcher_version"
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        result = subprocess.run(
            [sys.executable, str(gateway), "--version"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(f"charmer {expected} (catalog "), result.stdout)
        from charmer import identity

        self.assertEqual(result.stdout.strip(), identity.version_line())


    def test_module_entrypoint_runs_after_native_discovery_helpers(self) -> None:
        from charmer import cli

        self.assertTrue(callable(cli._original_cwd_for_adopt))
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        result = subprocess.run([sys.executable, "-m", "charmer.cli", "--version"],
                                env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        from charmer import identity

        # The same full release identity as the launchers print.
        self.assertEqual(result.stdout.strip(), identity.version_line())
        self.assertTrue(result.stdout.startswith(f"charmer {cli._pkg_version} (catalog "), result.stdout)


class PackageSourceTests(unittest.TestCase):
    """The Nix package installs what the Python package declares
    (``pyproject.toml``), so both channels carry the same content; package
    tree only, so these also run in the Nix sandbox."""

    def test_the_nix_package_reads_its_payload_from_pyproject(self) -> None:
        # No second list: the source filter, the copies and the wrappers all
        # come from pyproject.toml, so a tests/ or doc-only edit never
        # changes the store path.
        text = (NIX_DIR / "package.nix").read_text()
        self.assertIn("pyproject = builtins.fromTOML (builtins.readFile ../pyproject.toml);", text)
        self.assertIn('packageRoot = pyproject.tool.setuptools.package-dir."";', text)
        self.assertIn("dataFiles = pyproject.tool.setuptools.data-files;", text)
        self.assertIn("scripts = builtins.attrNames pyproject.project.scripts;", text)
        self.assertIn("installedPaths = [ packageRoot ] ++ documents;", text)
        for literal in ('"bin"', "docs/USAGE.md", "docs/CHEATSHEET.md", "docs/STANDALONE.md", "share/charmer/src"):
            self.assertNotIn(literal, text)
        phase = text.split("installPhase = ''", 1)[1].split("\n  '';", 1)[0]
        self.assertIn("cp -r ${packageRoot}/charmer $out/${packageDir}", phase)
        self.assertIn("${installDocuments}", phase)
        self.assertIn('packageDir = "${sitePackages}/charmer";', text)
        self.assertIn("sitePackages = python.sitePackages;", text)
        # The filter keys on the installed paths and keeps the pycache rule.
        self.assertIn("builtins.elem top installedPaths", text)
        self.assertIn('base != "__pycache__"', text)
        self.assertIn('!(pkgs.lib.hasSuffix ".pyc" base)', text)
        for name in installed_paths():
            self.assertTrue((REPO_ROOT / name).exists(), name)

    def test_package_payload_excludes_operator_credentials_and_grants(self) -> None:
        # The standalone package replaces the removed module-fragment export.
        # Its payload is source-only, never an export of the builder's state.
        self.test_the_nix_package_reads_its_payload_from_pyproject()
        text = (NIX_DIR / "package.nix").read_text()
        for forbidden in ("builtins.getEnv", "$HOME", "~/", "operator-ledger.json",
                          "operator-evidence.json", "config.yaml", "previous-key"):
            self.assertNotIn(forbidden, text)
        roots = installed_paths()
        self.assertEqual(roots[0], "src")
        self.assertTrue(all(name.startswith("docs/") and ".." not in Path(name).parts for name in roots[1:]))
        private = {".claude", ".config", ".local", ".env", "api-key", "previous-key",
                   "config.yaml", "operator-ledger.json", "operator-evidence.json",
                   "bindings.json", "custom.json"}
        for name in roots:
            root = REPO_ROOT / name
            entries = (root, *root.rglob("*")) if root.is_dir() else (root,)
            for path in entries:
                with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                    self.assertFalse(path.is_symlink(), "package payload must not import host state")
                    self.assertFalse(private.intersection(path.relative_to(REPO_ROOT).parts))
        if FLAKE_NIX is not None:
            flake = FLAKE_NIX.read_text()
            self.assertIn("pkgs.callPackage ./nix/package.nix", flake)
            self.assertIn("charmer = packageFor pkgs cliProxyApi;", flake)
            self.assertNotIn("builtins.getEnv", flake)

    def test_every_public_launcher_gets_one_wrapper(self) -> None:
        from charmer import entrypoints

        scripts = pyproject()["project"]["scripts"]
        # Every console script is an entry-point program (the source
        # launchers in bin/ may keep a name the package does not install).
        self.assertTrue(set(scripts) <= set(entrypoints.NAMES), sorted(scripts))
        self.assertNotIn("claude-gateway", scripts)
        text = (NIX_DIR / "package.nix").read_text()
        self.assertIn('proxyScript = "charmer-proxy";', text)
        # The wrappers are the manifest's launchers, each a console script.
        self.assertIn("launchers = (builtins.fromJSON (builtins.readFile ../packaging/product.json)).launchers;",
                      text)
        self.assertIn("launcherScripts = builtins.filter (name: name != proxyScript) launchers;", text)
        self.assertIn("assert builtins.elem proxyScript scripts;", text)
        self.assertIn("assert builtins.all (name: builtins.elem name scripts) launchers;", text)
        phase = text.split("installPhase = ''", 1)[1]
        self.assertIn('--add-flags "-P -m charmer.entrypoints $entry"', phase)
        self.assertIn('--add-flags "-P -m charmer.entrypoints ${proxyScript}"', phase)

    def test_presets_and_samples_are_installed_package_data(self) -> None:
        # Reviewed presets and externalized-provider samples ship as package
        # data (declaration templates; never auto-installed): inside src/.
        self.assertIn("src", installed_paths())
        self.assertIn("data/**/*", pyproject()["tool"]["setuptools"]["package-data"]["charmer"])
        for name in ("presets", "examples"):
            self.assertTrue((RESOURCES_ROOT / name).is_dir(), name)
            self.assertTrue(RESOURCES_ROOT.is_relative_to(REPO_ROOT / "src"))
        retired = json.loads((RESOURCES_ROOT / "catalog" / "retired.json").read_text())["retired"]
        samples = {entry["externalized"]["sample"] for entry in retired.values() if "externalized" in entry}
        self.assertTrue(samples)
        for sample in sorted(samples):
            self.assertTrue((RESOURCES_ROOT / "examples" / "providers.d" / sample).is_file(), sample)
        self.assertTrue(sorted((RESOURCES_ROOT / "presets").glob("*.json")))

    def test_proxy_entrypoint_passes_os_chdir(self) -> None:
        # The proxy entry point is the only production caller that chdirs;
        # main's seam defaults to no chdir.
        tree = ast.parse((REPO_ROOT / "src" / "charmer" / "entrypoints.py").read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_charmer_proxy")
        calls = [node for node in ast.walk(function)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "main"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0]), "main(argv, chdir=os.chdir)")


class ManagementPinWiringTests(unittest.TestCase):
    def test_attestation_uses_the_applied_ordered_patch_list(self):
        text = (NIX_DIR / "gateway.nix").read_text()
        names = admitted_names()
        manifest = json.loads((RESOURCES_ROOT / "catalog" / "gateway.json").read_text())["gateway"]["patches"]
        self.assertEqual(names, manifest)
        # The applied series and the passthru export are the recipe's admitted
        # series, in order; gateway.nix carries no patch list of its own.
        self.assertIn("gatewayPatches = map patchFile admitted;", text)
        self.assertIn("    patches = gatewayPatches;", text)
        self.assertIn("      gatewayPatches\n      gatewayContract\n", text)
        self.assertNotIn(".patch\n", text)
        # The flake passes the package that attestation (no module, no patch list).
        if FLAKE_NIX is not None:  # the sandbox tree stages no flake.nix
            flake = FLAKE_NIX.read_text()
            self.assertIn("cliProxyApiPatches = map baseNameOf cliProxyApi.gatewayPatches;", flake)
            self.assertIn('cliProxyApiBin = "${cliProxyApi}/bin/cli-proxy-api";', flake)
            self.assertNotIn(".patch", flake)

    def test_gateway_contract_manifest_is_ordered_content_identity(self):
        """An eval-time guard keeps inherited base patches
        empty; the passthru manifest is the upstream version plus ordered
        basename@sha256 of every applied patch; the package installs it
        as package data inside the Python package; the basename attestation
        is unchanged."""

        from charmer import qualify

        text = (NIX_DIR / "gateway.nix").read_text()
        # Each patch file must carry its recipe sha256 at evaluation time.
        self.assertIn('assert lib.assertMsg (builtins.hashFile "sha256" path == entry.sha256)', text)
        contract = text.split("  contract = {", 1)[1].split("};", 1)[0]
        self.assertIn("version = 2;", contract)
        self.assertIn("go_version = upstream.toolchain.version;", contract)
        self.assertIn("godebug = upstream.build.godebug;", contract)
        # The passthru is the checked-in package resource's exact bytes
        # (every channel ships that file inside the package).
        self.assertIn("builtins.toJSON contract == builtins.readFile ../src/charmer/data/gateway-contract.json",
                      text)
        self.assertIn("upstream_version = version;", contract)
        self.assertIn('patches = map (entry: "${entry.basename}@${entry.sha256}") admitted;', contract)
        if FLAKE_NIX is not None:
            self.assertIn("cliProxyApiContract = cliProxyApi.gatewayContract;", FLAKE_NIX.read_text())
        package = (NIX_DIR / "package.nix").read_text()
        self.assertIn("cliProxyApiContract ? null", package)
        # No second copy: the package asserts the checked-in resource equals
        # the passthru and installs it with the Python package.
        self.assertIn("builtins.toJSON cliProxyApiContract == builtins.readFile "
                      "../src/charmer/data/gateway-contract.json", package)
        self.assertIn("assert contractChecked;", package)
        self.assertNotIn("share/charmer/gateway-contract.json", package)
        # The manifest Nix would produce parses, in the applied order.
        names = admitted_names()
        root = PATCH_DIR
        manifest = {"version": 2, "upstream_version": recipe()["upstream"]["version"],
                    "go_version": recipe()["toolchain"]["version"], "godebug": recipe()["build"]["godebug"],
                    "patches": [f"{name}@{hashlib.sha256((root / name).read_bytes()).hexdigest()}" for name in names]}
        parsed = qualify.parse_gateway_contract(json.dumps(manifest, sort_keys=True).encode())
        self.assertEqual([item.split("@", 1)[0] for item in parsed["patches"]], names)
        digest = qualify.gateway_contract_digest(parsed)
        edited = json.loads(json.dumps(manifest))
        edited["patches"][0] = edited["patches"][0].split("@", 1)[0] + "@" + "0" * 64
        self.assertNotEqual(qualify.gateway_contract_digest(edited), digest)

    def test_proxy_wrapper_overrides_or_clears_both_attestation_variables(self):
        text = (NIX_DIR / "package.nix").read_text()
        self.assertIn("cliProxyApiPatches ? [ ]", text)
        # Even a pinned binary with NO patch names overrides an inherited
        # patch list with empty text. Standalone clears both variables.
        flags = text.split("  proxyFlag =", 1)[1].split(";", 1)[0]
        self.assertIn("if cliProxyApiBin != null then", flags)
        self.assertIn(
            "--set CHARMER_PROXY_BIN "
            '"${cliProxyApiBin}" --set CHARMER_PROXY_PATCHES '
            '"${pkgs.lib.concatStringsSep "," cliProxyApiPatches}"',
            flags,
        )
        self.assertIn("else\n      ''--unset CHARMER_PROXY_BIN --unset CHARMER_PROXY_PATCHES''", flags)
        phase = text.split("installPhase = ''", 1)[1]
        wrappers, proxy_wrapper = phase.split("makeWrapper ${python}/bin/python3 $out/bin/${proxyScript}", 1)
        self.assertNotIn("${proxyFlag}", wrappers)
        self.assertEqual(proxy_wrapper.count("${proxyFlag}"), 1)

    def test_built_standalone_wrapper_rejects_hostile_inherited_attestation(self):
        # tests/default.nix supplies its actual built standalone package.
        # Source runs may supply the same `package` path after nix-build.
        package = os.environ.get("package")
        if not package:
            self.skipTest("boundary: built standalone wrapper supplied by the Nix sandbox gate")
        wrapper = Path(package) / "bin" / "charmer-proxy"
        self.assertTrue(wrapper.is_file())
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        from charmer import management

        with tempfile.TemporaryDirectory(prefix="cm-wrapper-attestation-") as tmp:
            # Source the REAL makeWrapper prologue, intercepting only exec.
            # Never run the proxy entrypoint or gateway; capture only the two
            # non-secret attestation names, not the operator environment.
            env = {
                "HOME": tmp, "PATH": os.defpath,
                management.PINNED_BIN_ENV: "/hostile/gateway",
                management.PATCHES_ENV: management.ALLOWLIST_PATCH,
                management.CHANNEL_ENV: "bundle",
            }
            self.assertTrue(management.allowlist_build({**env, management.CHANNEL_ENV: "nix"}))  # live negative control
            code = (
                "import json,os; print(json.dumps({k: os.environ[k] for k in "
                "('CHARMER_PROXY_BIN','CHARMER_PROXY_PATCHES','CHARMER_CHANNEL') if k in os.environ}))"
            )
            result = subprocess.run(
                [bash, "-c", 'probe_python="$2"; probe_code="$3"; exec() { "$probe_python" -c "$probe_code"; }; source "$1"',
                 "wrapper-test", str(wrapper), sys.executable, code],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(result.stdout)
            # The wrapper names its own channel and clears both attestations.
            self.assertEqual(captured, {"CHARMER_CHANNEL": "nix"})
            self.assertFalse(management.allowlist_build(captured))

    def test_every_wrapper_names_the_nix_channel_and_never_inherits_one(self):
        text = (NIX_DIR / "package.nix").read_text()
        self.assertIn("channelFlag = ''--set CHARMER_CHANNEL nix'';", text)
        phase = text.split("installPhase = ''", 1)[1]
        wrappers = phase.count("makeWrapper ${python}/bin/python3")
        self.assertEqual(wrappers, 2)  # the loop over the launcher scripts and charmer-proxy
        self.assertEqual(phase.count("${channelFlag}"), wrappers)
        self.assertNotIn("CHARMER_CHANNEL", phase)
        from charmer import management
        self.assertEqual((management.CHANNEL_ENV, management.MANAGEMENT_CHANNEL), ("CHARMER_CHANNEL", "nix"))


class CliProxyApiDerivationTests(unittest.TestCase):
    """The gateway derivation runs the recipe; the gates are recipe records."""

    def test_cli_proxy_api_nix_shape(self) -> None:
        text = (NIX_DIR / "gateway.nix").read_text()
        expression = "\n".join(line for line in text.splitlines() if not line.startswith("#")).lstrip()
        self.assertTrue(expression.startswith("{ pkgs }:"))
        self.assertIn("recipe = ../gateway/UPSTREAM.json;", text)
        self.assertIn("buildTool = ../tools/build.py;", text)
        names = admitted_names()
        for patch in (
            "cli-proxy-api-loopback-oauth.patch",
            "cli-proxy-api-kimi-claude-compat.patch",
            "cli-proxy-api-non-claude-cache-retention.patch",
            "cli-proxy-api-management-readonly-allowlist.patch",
            "cli-proxy-api-watcher-parentdir.patch",
            "cli-proxy-api-serve-after-initial-auth-load.patch",
            "cli-proxy-api-management-env-only.patch",
        ):
            self.assertIn(patch, names)
        # The derivation applies the recipe's series in order; the
        # retention patch's hunks are pinned to the sequentially patched
        # source, so the order is part of the contract. The gateway.json
        # manifest must carry the same ordered list (manifest parity).
        manifest = json.loads((RESOURCES_ROOT / "catalog" / "gateway.json").read_text())
        self.assertEqual(manifest["gateway"]["patches"], names)
        # The network-free regression gates are recipe records; the build
        # tool runs them in the flake's gate checks (never in the shipped
        # build), so the patched tests run inside a sandboxed build.
        gates = {record["name"]: record for record in recipe()["gates"]}
        self.assertEqual(gates["executor"]["packages"], ["./internal/runtime/executor"])
        self.assertEqual(gates["config-watcher"]["packages"], ["./internal/watcher/..."])
        self.assertEqual(gates["kimi-compat-config"]["packages"], ["./internal/config", "./internal/watcher/synthesizer"])
        self.assertEqual(gates["kimi-compat-sdk"]["packages"], ["./sdk/cliproxy"])
        self.assertEqual(gates["kimi-compat-sdk"]["run"], "TestApplyOAuthModelAlias|TestBuildClaudeConfigModels|TestBuildConfigModels")
        # The management allowlist gate (see ManagementAllowlistGateTests).
        self.assertEqual(gates["management-allowlist"]["packages"], ["./internal/api/..."])
        self.assertIn("gatewayBuild gates --gates ${selection}", text)
        self.assertIn("doCheck = false;", text)
        pins = {pin["name"]: pin for pin in recipe()["source_pins"]}
        self.assertEqual(pins["substitution-log-format"]["text"], "upstream served model %q for requested model %q")
        # No v1 plugin wiring in the derivation.
        self.assertNotIn("plugin", text.lower())


class ModuleWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        if FLAKE_REPO is None:
            self.skipTest(
                "boundary: repository layout unavailable (Nix sandbox carries "
                "only the v2 tree); run in the source checkout"
            )

    def test_flake_outputs_shape(self) -> None:
        """The flake exports packages, apps, devShells and checks; no service
        or user-environment module (the product installs its own service)."""

        text = FLAKE_NIX.read_text()
        for gone in ("nix/module.nix", "nix/gateway-unit.nix", "tests/unit-triggers.nix"):
            self.assertFalse((FLAKE_REPO / gone).exists(), gone)
        for forbidden in ("homeManagerModules", "nixosModules", "systemd.user", "overrideAttrs", "go test",
                          "writeShellApplication"):
            self.assertNotIn(forbidden, text)
        self.assertIn('systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];', text)
        for output in ("packages = forAllSystems", "apps = forAllSystems", "devShells = forAllSystems",
                       "checks = lib.recursiveUpdate"):
            self.assertIn(output, text)
        self.assertEqual(text.count("import ./nix/gateway.nix"), 1)
        self.assertIn("pkgs.callPackage ./nix/package.nix", text)
        self.assertIn("import ./nix/devshell.nix", text)
        # The pinned (pre-patch) embedded registry is passed as read-only
        # package data the way cliProxyApiBin is passed.
        self.assertIn('cliProxyApiRegistry = "${cliProxyApi.src}/internal/registry/models";', text)
        shell = (NIX_DIR / "devshell.nix").read_text()
        for tool in ("pkgs.python3", "go", "pkgs.git", "pkgs.bubblewrap", "cm-go"):
            self.assertIn(tool, shell)
        package = (NIX_DIR / "package.nix").read_text()
        self.assertIn("${python}/bin/python3 -m compileall", package)
        self.assertIn("--invalidation-mode unchecked-hash", package)
        # The service spec is package data (inside src/); the shipped gateway
        # is linked at a root-relative path the service install compares.
        self.assertIn("src", installed_paths())
        self.assertTrue((RESOURCES_ROOT / "gateway-unit.json").is_file())
        self.assertIn('ln -s "${cliProxyApiBin}" $out/libexec/charmer/cli-proxy-api', package)
        self.assertNotIn("plugin", text.lower())

    def test_package_nix_installs_gateway_and_management_bins(self) -> None:
        text = (NIX_DIR / "package.nix").read_text()
        # The commands come from pyproject.toml's console scripts (no
        # one-model alias: `charmer direct` covers it).
        self.assertEqual(sorted(pyproject()["project"]["scripts"]),
                         ["charmer", "charmer-dev", "charmer-proxy"])
        self.assertIn("scripts = builtins.attrNames pyproject.project.scripts;", text)
        # The resources travel inside the Python package; the wrappers name
        # that resource directory (an inherited override never applies).
        self.assertIn('resources = "${packageDir}/data";', text)
        self.assertEqual(text.count('--set CHARMER_ASSETS "$out/${resources}"'), 2)
        self.assertEqual(text.count('--set CHARMER_HOOK_COMMAND "$out/bin/charmer"'), 1)
        self.assertIn("CHARMER_PROXY_BIN", text)
        self.assertIn("../src/charmer/data/version.json", text)
        # The registry snapshot is checked in under the resources; the build
        # compares it with the pinned source (no second, generated copy).
        self.assertIn("cliProxyApiRegistry ? null", text)
        self.assertIn('"$out/${resources}/registry/$name"', text)
        self.assertNotIn("$out/share/charmer/registry", text)
        for name in ("models.json", "codex_client_models.json"):
            self.assertIn(name, text)
            self.assertTrue((RESOURCES_ROOT / "registry" / name).is_file())

    def test_flake_drops_v1_identifiers(self) -> None:
        text = FLAKE_NIX.read_text()
        for identifier in (*legacy.LEGACY_NIX_ATTRIBUTES, "writeShellApplication"):
            self.assertNotIn(identifier, text)
        # The flake builds the gateway through nix/gateway.nix only and
        # carries no unit: the product renders it (gateway service install).
        self.assertIn("import ./nix/gateway.nix", text)
        self.assertNotIn("systemd.user.services", text)

    def test_flake_checks_wiring(self) -> None:
        text = FLAKE_NIX.read_text()
        self.assertIn("checks", text)
        self.assertIn("x86_64-linux.charmer", text)
        self.assertIn("tests/default.nix", text)
        # The flake check IS tests/default.nix (imported, never copied).
        self.assertIn("import ./tests/default.nix", text)

    def test_patches_preserved_and_present(self) -> None:
        for patch in (
            "cli-proxy-api-loopback-oauth.patch",
            "cli-proxy-api-kimi-claude-compat.patch",
            "cli-proxy-api-non-claude-cache-retention.patch",
            "cli-proxy-api-management-readonly-allowlist.patch",
            "cli-proxy-api-watcher-parentdir.patch",
            "cli-proxy-api-serve-after-initial-auth-load.patch",
            "cli-proxy-api-management-env-only.patch",
        ):
            self.assertTrue((PATCH_DIR / patch).is_file())


class ManagementAllowlistGateTests(unittest.TestCase):
    """The management allowlist Go gate is positive, not just wired.

    ``go test`` still passes if a regenerated patch drops its test file, and
    the postCheck echo marker proves only that the command ran. Pin the patch
    body (read next to the package, where the Nix sandbox stages the
    manifest's patches) and the exact, wildcard-free -skip list.
    """

    PATCH = PATCH_DIR / "cli-proxy-api-management-readonly-allowlist.patch"
    GATE_TESTS = (
        "TestManagementAllowlist_RouteTableMatchesReview",
        "TestManagementAllowlist_EveryRegisteredRouteRefusedUnlessAllowlisted",
        "TestManagementAllowlist_KeyMiddlewareRunsOnlyOnAllowlistedRoutes",
        "TestManagementAllowlist_UnregisteredManagementPathsNeverReachKeyMiddleware",
        "TestManagementAllowlist_ManagementKeyConsumersAudited",
        "TestManagementAllowlist_AllowlistedRouteWithoutKeyIs401",
        "TestManagementAllowlist_FiveBadKeysOnAllowlistedRouteBan",
        "TestManagementAllowlist_BypassVariantsRefused",
        "TestManagementAllowlist_BadKeysOnRefusedRoutesNeverCount",
        "TestManagementAllowlist_BrowserOriginatedRequestsRefusedAndNeverCount",
        "TestManagementAllowlist_PreflightRefusedBeforeCORS",
        "TestManagementAllowlist_RefusedUnreadBodyClosesConnection",
        "TestManagementAllowlist_NonLoopbackHostRefusedAndNeverCounts",
        "TestManagementAllowlist_RemoteOverrideForcedOff",
        "TestManagementAllowlist_OAuthCallbackWithPendingSessionWritesNothing",
        "TestManagementAllowlist_RefusedAPISurfaces",
        "TestManagementAllowlist_RESPSurfaceClosedWithoutKeyCheck",
        "TestManagementAllowlist_ModelDefinitionsRegistryFallback",
    )
    # Upstream tests that pin surfaces the allowlist refuses by design. A
    # change here is a reviewed edit: every other internal/api test runs.
    SKIPPED_UPSTREAM = (
        "TestRedisProtocol_SUBSCRIBE_UsageSendsSupportRefresh",
        "TestRedisProtocol_SUBSCRIBE_ErrorsReceivesErrorEvents",
        "TestRedisProtocol_AUTH_And_PopContracts",
        "TestOAuthCallbackRouteSkipsManagementKeyMiddleware",
        "TestManagementUsageRequiresManagementAuthAndPopsArray",
        "TestManagementPluginsRouteRegistered",
        "TestManagementResponseExposesPluginSupportHeaderForCORS",
        "TestConfigV8ETagExposedForCORS",
        "TestIssue5190ModelDetailLifecycle",
        "TestIssue5190ModelDetailUsesVisibleCatalog",
        "TestIssue5190ModelDetailHomeVisibility",
        # The other open 8.x surfaces, default-denied: alpha search (Home and
        # direct), live, realtime, videos and interactions.
        "TestAuditHomeCodexSearchBusyReturnsTrustedRetryAfter",
        "TestAuditHomeCodexSearchBodyCloseBeforeRelease",
        "TestHomeCodexAlphaSearchReportsUnauthorizedBeforeEarlyReturn",
        "TestHomeCodexAlphaSearchEndsSelectionAcrossDirectHTTPPaths",
        "TestCodexLiveRoutesRequireAuthAndAreRegistered",
        "TestRealtimeStandardRoutesAndClientSecretAuth",
        "TestCodexAlphaSearchForwardsRequest",
        "TestCodexAlphaSearchUsesPluginProviderTargetModel",
        "TestCodexAlphaSearchFallsBackWhenPluginDoesNotHandleRoute",
        "TestCodexAlphaSearchRejectsUnsupportedPluginRouteTarget",
        "TestCodexAlphaSearchSanitizesResponsesOnlyFields",
        "TestCodexAlphaSearchCredentialPolicy",
        "TestCodexAlphaSearchOptInAPIKeyUsesConfiguredEndpoint",
        "TestCodexAlphaSearchOptInAPIKeyStripsCredentialPrefix",
        "TestCodexAlphaSearchOptInAPIKeyResolvesModelAlias",
        "TestCodexAlphaSearchOptInAPIKeyWithoutBaseURLFailsClosed",
        "TestCodexAlphaSearchPassesGinContextToAuthSelection",
        "TestCodexAlphaSearchUsesRequestIDForSessionAffinity",
        "TestVideosRoutesKeepXAINativeAndExposeOpenAIPrefix",
        "TestInteractionsRouteRegistered",
        # 8.0.35-8.0.41 tests of refused surfaces: the RESP parser behind the
        # closed RESP surface, alpha search over SIWC, the /codex/callback page
        # and the v8 SIWC routes.
        "TestRedisProtocol_MalformedAndOverflowFramesDoNotCrashServer",
        "TestCodexAlphaSearchSIWCWithRealExecutor",
        "TestSIWCManagementRouteAccessControl",
        "TestCodexCallbackRejectsSIWCState",
        "TestSIWCStatusEndpoint",
    )
    # Upstream tests this gate excludes for a diagnosed upstream reason, not a
    # refused surface. Each keeps its record in gateway/upstream-review.json
    # test_exclusions (8.0.45): a Codex client-catalog test that fails on the
    # pristine tag, and an advisory fixture that depends on the zstd
    # encoder's window choice.
    RECORDED_EXCLUSIONS = (
        "TestModelsMultiAgentClientConfigAndReload",
        "TestCaptureRequestInfoDoesNotDecodeASingleSegmentBomb",
    )

    def _patch_text(self) -> str:
        self.assertTrue(self.PATCH.is_file(), f"{self.PATCH} missing next to the package")
        return self.PATCH.read_text(encoding="utf-8")

    def test_patch_carries_guard_and_gate_tests(self) -> None:
        text = self._patch_text()
        self.assertIn("+++ b/internal/api/server_management_allowlist.go", text)
        self.assertIn("+++ b/internal/api/server_management_allowlist_test.go", text)
        added = "\n".join(line[1:] for line in text.splitlines() if line.startswith("+"))
        # Installed before every route; remote override forced off; RESP
        # surface closed before any key check.
        self.assertIn("engine.Use(managementReadOnlyAllowlistMiddleware())", added)
        self.assertIn("allowRemoteOverride: false,", added)
        self.assertIn("if managementRESPOutputDisabled {", added)
        for name in self.GATE_TESTS:
            with self.subTest(test=name):
                self.assertIn(f"func {name}(t *testing.T)", added)
        # Gate servers are production-shaped: the plugin host is wired as
        # sdk/cliproxy/builder.go does, so NoRoute probes reach the dispatcher
        # that runs the key middleware (without a host they are vacuous).
        for wiring in (
            "host := pluginhost.New()",
            "host.ApplyConfig(context.Background(), cfg)",
            "host.RegisterFrontendAuthProviders()",
            "WithPluginHost(host)",
        ):
            with self.subTest(wiring=wiring):
                self.assertIn(wiring, added)
        # The source tripwire pins the audited management-key consumers.
        self.assertIn("var auditedManagementKeyConsumers = map[string]int{", added)
        # Both namespaces are in scope; the v8 read twins are refused, and a
        # reviewed route table names every registered route.
        self.assertIn('"/v8/management",', added)
        self.assertIn('"/v1/models/",', added)
        self.assertIn("+++ b/internal/api/server_management_route_table_test.go", text)
        self.assertRegex(added, r'"GET /v8/management/credentials":\s+routeRefuse,')
        self.assertRegex(added, r'"GET /v0/management/auth-files":\s+routeAllow,')
        self.assertRegex(added, r'"POST /v8/management/models/refresh":\s+routeRefuse,')
        self.assertRegex(added, r'"POST /v8/management/routing/model-definitions/refresh":\s+routeRefuse,')
        # The guard answers inside upstream's unconsumed-body wrapper, and the
        # server keeps its header read deadline (an unchanged context line).
        self.assertIn("+\t\tHandler:           wrapUnconsumedRequestBodyHandler(managementGuardHandler(engine, "
                      "logging.GinLogrusLogger(), logging.GinLogrusRecovery())),\n"
                      " \t\tReadHeaderTimeout: muxSniffDeadline,\n", text)

    def test_gate_runs_internal_api_with_explicit_skips(self) -> None:
        record = gate("management-allowlist")
        self.assertEqual(record["packages"], ["./internal/api/..."])
        match = re.fullmatch(r"\^\(([^()]*)\)\$", record["skip"])
        self.assertIsNotNone(match, "management allowlist skip is not an exact name list")
        names = match.group(1).split("|")
        for name in names:
            # Plain names only: a wildcard would silently swallow future
            # upstream tests, and the patch's own tests must never be skipped.
            self.assertRegex(name, r"^Test[A-Za-z0-9_]+$")
            self.assertNotIn(name, self.GATE_TESTS)
        self.assertEqual(sorted(names), sorted([*self.SKIPPED_UPSTREAM, *self.RECORDED_EXCLUSIONS]))
        self.assertEqual(len(names), len(set(names)))
        self._check_exclusion_records(json.loads(UPSTREAM_REVIEW.read_text()))
        # The patch's own tests must be in the selection.
        self.assertEqual(record["expected_tests"], list(self.GATE_TESTS))
        # No run filter and no reruns on this gate (global-state tests in
        # handlers/management fail on reruns even upstream): it runs once.
        self.assertNotIn("run", record)
        self.assertIsNone(record["count"])
        self.assertFalse(record["race"])

    def _check_exclusion_records(self, review) -> None:
        # Every non-surface skip of this gate has its review record, and no
        # refused surface is passed off as an upstream exclusion.
        recorded = [item["test"] for item in review["test_exclusions"] if item["gate"] == "management-allowlist"]
        self.assertEqual(sorted(recorded), sorted(self.RECORDED_EXCLUSIONS),
                         "a management-allowlist skip lost its test_exclusions record")
        self.assertFalse(set(recorded) & set(self.SKIPPED_UPSTREAM))

    def test_each_recorded_exclusion_keeps_its_review_record(self) -> None:
        review = json.loads(UPSTREAM_REVIEW.read_text())
        self._check_exclusion_records(review)
        # Negative controls: delete each record in turn while its skip stays.
        for name in self.RECORDED_EXCLUSIONS:
            missing = {**review, "test_exclusions": [item for item in review["test_exclusions"]
                                                     if item["test"] != name]}
            with self.subTest(deleted=name), self.assertRaises(AssertionError):
                self._check_exclusion_records(missing)


class StartupGateTests(unittest.TestCase):
    """The sixth patch and its Go gate.

    Model routes wait (bounded) for the watcher's initial auth registration.
    Pin the patch body next to the package (the sandbox stages the manifest's
    patches there), its position after the watcher patch it depends on, and
    the exact -run list of the postCheck gate, so a regenerated patch that
    drops a gate test, or a narrowed filter, fails here.
    """

    PATCH = PATCH_DIR / "cli-proxy-api-serve-after-initial-auth-load.patch"
    SDK_GATE_TESTS = (
        "TestRunFirstModelRequestDuringStartupIsServed",
        "TestRunGateOpensAtDeadline",
        "TestRunWithoutAuthFilesDoesNotWait",
        "TestRunWithoutBarrierCapableWatcherDoesNotWait",
        "TestRunGateHoldsOnlyModelRoutes",
        "TestStartupGatedRoutes",
        "TestStartupGateDeadlineIgnoresManagerLock",
    )

    def test_patch_carries_barrier_gate_and_tests(self) -> None:
        self.assertTrue(self.PATCH.is_file(), f"{self.PATCH} missing next to the package")
        text = self.PATCH.read_text(encoding="utf-8")
        for path in (
            "internal/watcher/dispatcher.go",
            "internal/watcher/dispatcher_barrier_test.go",
            "sdk/cliproxy/startup_gate.go",
            "sdk/cliproxy/startup_gate_test.go",
        ):
            with self.subTest(path=path):
                self.assertIn(f"+++ b/{path}", text)
        added = "\n".join(line[1:] for line in text.splitlines() if line.startswith("+"))
        # Bounded: the gate opens at the latest 30 s after Run began.
        self.assertIn("const initialAuthReadyTimeout = 30 * time.Second", added)
        self.assertIn("func (w *Watcher) DispatchAuthBarrier() <-chan struct{}", added)
        self.assertIn("initial auth registration not confirmed within", added)
        # The watcher barrier tests run under the existing ./internal/watcher/... gate.
        for name in ("TestDispatchAuthBarrierFollowsPendingUpdates", "TestDispatchAuthBarrierWithoutQueue"):
            with self.subTest(test=name):
                self.assertIn(f"func {name}(t *testing.T)", added)
        declared = set(re.findall(r"^func (Test[A-Za-z0-9_]+)\(t \*testing\.T\)", added, re.MULTILINE))
        for prefix in self.SDK_GATE_TESTS:
            with self.subTest(test=prefix):
                self.assertTrue(any(name.startswith(prefix) for name in declared), prefix)

    def test_gate_runs_every_startup_test_after_the_watcher_gate(self) -> None:
        applied = admitted_names()
        watcher = applied.index("cli-proxy-api-watcher-parentdir.patch")
        self.assertEqual(applied[watcher:watcher + 2], ["cli-proxy-api-watcher-parentdir.patch", self.PATCH.name])
        record = gate("startup-gate")
        self.assertEqual(record["packages"], ["./sdk/cliproxy"])
        self.assertEqual(record["run"].split("|"), list(self.SDK_GATE_TESTS))
        for prefix in self.SDK_GATE_TESTS:
            self.assertTrue(any(name.startswith(prefix) for name in record["expected_tests"]), prefix)
        self.assertIsNone(record["count"])
        names = [entry["name"] for entry in recipe()["gates"]]
        self.assertLess(names.index("config-watcher"), names.index("startup-gate"))


class ManagementEnvOnlyGateTests(unittest.TestCase):
    """The env-only patch and its Go gate.

    The management API authenticates only with MANAGEMENT_PASSWORD: a
    remote-management secret-key in config.yaml, plaintext or bcrypt, is
    dropped at every load (start and hot reload; never hashed, never written
    back), never enables management and never authenticates; a runtime local
    password (-password / TUI) neither enables nor authenticates it; the
    secret is ignored under both spellings (remote-management.secret-key and
    management.secret-key). Pin the patch body next to the package (the
    sandbox stages the manifest's patches there), its position right after
    the byte loader (watcher-parentdir), and the exact -run list of the gate,
    so a regenerated patch that drops a gate test, or a narrowed filter,
    fails here.
    """

    PATCH = PATCH_DIR / "cli-proxy-api-management-env-only.patch"
    CONFIG_GATE_TESTS = (
        "TestManagementEnvOnly_LoadConfigDropsSecretKeyWithoutWriteBack",
        "TestManagementEnvOnly_ReloadAndPayloadLoadsDropSecretKey",
        "TestManagementEnvOnly_BothSpellingsIgnoredFileUntouched",
        "TestManagementEnvOnly_V8MigrationKeepsSecuritySettings",
    )
    API_GATE_TESTS = (
        "TestManagementEnvOnly_InitialConfigSecretDoesNotEnableManagement",
        "TestManagementEnvOnly_ServerIgnoresConfigSecretAtStart",
        "TestManagementEnvOnly_HotReloadedConfigSecretDoesNotEnableManagement",
        "TestManagementEnvOnly_ReloadIgnoresConfigSecret",
        "TestManagementEnvOnly_EnvSecretIsTheOnlyKey",
        "TestManagementEnvOnly_AuthenticateManagementKeyIgnoresConfigSecret",
        "TestManagementEnvOnly_EnvOnlyManagementUnchanged",
        "TestManagementEnvOnly_LocalPasswordNeverAuthenticates",
        "TestManagementEnvOnly_AuthenticateManagementKeyIgnoresLocalPassword",
        "TestManagementEnvOnly_LocalPasswordStillGuardsKeepAlive",
    )

    def _patch_text(self) -> str:
        self.assertTrue(self.PATCH.is_file(), f"{self.PATCH} missing next to the package")
        return self.PATCH.read_text(encoding="utf-8")

    def test_patch_makes_config_secret_inert_and_carries_gate_tests(self) -> None:
        text = self._patch_text()
        for path in (
            "internal/config/parse.go",
            "internal/config/management_env_only.go",
            "internal/config/management_env_only_test.go",
            "internal/api/server.go",
            "internal/api/server_reload.go",
            "internal/api/handlers/management/handler.go",
            "internal/api/server_management_env_only_test.go",
        ):
            with self.subTest(path=path):
                self.assertIn(f"+++ b/{path}", text)
        added = "\n".join(line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = "\n".join(line[1:] for line in text.splitlines() if line.startswith("-") and not line.startswith("---"))
        # Every load goes through ParseConfigBytes (the byte loader routes
        # LoadConfig and LoadConfigBytes there), which drops the config
        # secret under either spelling instead of hashing it.
        self.assertEqual(added.count("dropConfigManagementSecret(&cfg)"), 1)
        self.assertIn("cfg.RemoteManagement.SecretKey = \"\"", added)
        self.assertIn("bcrypt.GenerateFromPassword([]byte(cfg.RemoteManagement.SecretKey), bcrypt.DefaultCost)", removed)
        self.assertIn("remote-management.secret-key and management.secret-key are ignored", added)
        # Start, reload and the key check no longer read the config secret.
        self.assertIn("hasManagementSecret := envManagementSecret\n", added + "\n")
        self.assertIn("hasManagementSecret := cfg.RemoteManagement.SecretKey != \"\" || envManagementSecret || s.localPassword != \"\"", removed)
        # The runtime local password (-password / TUI) is no management key:
        # the key check drops its branch and the source adds no reader of it
        # (it keeps guarding /keep-alive only).
        self.assertIn("if lp := h.localPassword; lp != \"\" {", removed)
        self.assertIn("bcrypt.CompareHashAndPassword([]byte(secretHash), []byte(provided))", removed)
        self.assertIn("newSecretEmpty := cfg.RemoteManagement.SecretKey == \"\"", removed)
        # The only config-secret reads the patch adds (outside its tests) are
        # the drop helper's own.
        source_added = [
            line[1:].strip()
            for chunk in text.split("diff --git ")[1:]
            if not chunk.split("\n", 1)[0].endswith("_test.go")
            for line in chunk.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        self.assertEqual(
            [line for line in source_added if "RemoteManagement.SecretKey" in line],
            ['if cfg == nil || cfg.RemoteManagement.SecretKey == "" {', 'cfg.RemoteManagement.SecretKey = ""'],
        )
        self.assertEqual(
            [line for line in source_added if "ocalPassword" in line and not line.startswith("//")], []
        )
        for name in self.CONFIG_GATE_TESTS + self.API_GATE_TESTS:
            with self.subTest(test=name):
                self.assertIn(f"func {name}(t *testing.T)", added)
        # Hot reloads go through a real config watcher into Server.UpdateClients.
        self.assertIn("watcher.NewWatcher(f.cfgPath, f.authDir, func(cfg *proxyconfig.Config) {", added)
        self.assertIn("s.UpdateClients(cfg)", added)

    def test_gate_runs_every_env_only_test_and_follows_the_byte_loader(self) -> None:
        names = admitted_names()
        self.assertEqual(names[6], self.PATCH.name)
        self.assertLess(names.index("cli-proxy-api-watcher-parentdir.patch"), names.index(self.PATCH.name))
        record = gate("management-env-only")
        self.assertEqual(record["packages"], ["./internal/config", "./internal/api"])
        match = re.fullmatch(r"\^\(([^()]*)\)\$", record["run"])
        self.assertIsNotNone(match, "management env-only run is not an exact name list")
        self.assertEqual(match.group(1).split("|"), list(self.CONFIG_GATE_TESTS + self.API_GATE_TESTS))
        self.assertEqual(record["expected_tests"], list(self.CONFIG_GATE_TESTS + self.API_GATE_TESTS))
        self.assertIsNone(record["count"])
        self.assertNotIn("skip", record)


class GatewayCheckWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        if FLAKE_REPO is None:
            self.skipTest(
                "boundary: repository layout unavailable (Nix sandbox carries "
                "only the v2 tree); run in the source checkout"
            )

    def test_separate_gateway_check_uses_shared_derivation(self) -> None:
        text = FLAKE_NIX.read_text()
        for literal in (
            "x86_64-linux.charmer-gateway",
            "import ./tests/gateway-check.nix",
            "import ./nix/gateway.nix",
            "import ./tests/default.nix",
        ):
            self.assertIn(literal, text)
        self.assertNotIn("__noChroot = true", text)

    def test_gateway_check_requires_execution_and_complete_evidence(self) -> None:
        text = (REPO_ROOT / "tests/gateway-check.nix").read_text()
        for literal in (
            "pkgs.bubblewrap", "__noChroot = false",
            "CHARMER_TEST_REQUIRE_GATEWAY=1",
            'CHARMER_TEST_CLI_PROXY_API="$cliProxyApi/bin/cli-proxy-api"',
            "--sandbox-proof", "--check-modules",
            'mkdir -m 0700 "$out/evidence"',
            '--validate-evidence "$out/evidence"',
            'test "$status" -eq 0',
            "grep -E ' \\.\\.\\. skipped|skipped='",
        ):
            self.assertIn(literal, text)
        self.assertNotIn("__noChroot = true", text)


class Go127DiagnosticWiringTests(unittest.TestCase):
    def test_roots_are_built_called_and_required_as_evidence(self):
        from unittest import mock
        import _gateway_harness as harness
        from tests import test_gateway_contracts as contracts

        cases = (
            ("test_concurrent_response_read_close", "response-read-close", "executor", "TestGatewayResponseReadClose"),
            ("test_slow_non2xx_body_release", "slow-non2xx-release", "executor", "TestGatewaySlowNon2xxBodyRelease"),
            ("test_abandoned_executor_sse", "executor-sse-abandon", "executor", "TestGatewayAbandonedExecutorSSE"),
            ("test_shutdown_resource_accounting", "shutdown-resources", "service", "TestGatewayShutdownResources"),
        )
        diagnostic = (REPO_ROOT / "tests/gateway-startup-probe.nix").read_text()
        for method, row, package, root in cases:
            with self.subTest(row=row):
                source = f"gateway_{package}_probe_test.go"
                destination = "sdk/cliproxy" if package == "service" else "internal/runtime/executor"
                self.assertIn(f"cp ${{./{source}}} {destination}/gwtest_omission_probe_test.go", diagnostic)
                text = (REPO_ROOT / "tests" / source).read_text()
                self.assertEqual(len(re.findall(rf"^func {root}\(t \*testing\.T\)", text, re.MULTILINE)), 1)
                with mock.patch.object(harness, "run_codex_diagnostic", return_value="fixture-diagnostic") as run, \
                        mock.patch.object(harness.EVIDENCE, "record") as record:
                    getattr(contracts.Go127DiagnosticTests(method), method)()
                run.assert_called_once_with(root, package=package)
                record.assert_called_once_with(contracts.MODULE, "go127", {
                    "row": row, "package": package, "root": root, "diagnostic": "fixture-diagnostic"})
        self.assertEqual(harness.REQUIRED_EVIDENCE[contracts.MODULE]["go127"],
                         {case[1] for case in cases} | {"headers-500", "headers-501",
                             "downstream-abandon", "downstream-retained-control"})
        self.assertIn("go test -mod=vendor -c -o gwtest-startup-probe ./sdk/cliproxy", diagnostic)
        self.assertIn("go test -mod=vendor -race -c -o gwtest-executor-probe ./internal/runtime/executor", diagnostic)


class CutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        if FLAKE_REPO is None:
            self.skipTest(
                "boundary: repository layout unavailable (Nix sandbox carries "
                "only the v2 tree); run in the source checkout"
            )


    def test_v2_replacements_present(self) -> None:
        for relative in (
            "nix/devshell.nix",
            "nix/package.nix",
            "src/charmer/data/gateway-unit.json",
            "src/charmer/proxy.py",
            "bin/charmer-proxy",
            "README.md",
        ):
            with self.subTest(path=relative):
                self.assertTrue((FLAKE_REPO / relative).exists(), f"{relative} missing")

    def test_removed_identifiers_absent_from_v2_runtime_and_wiring(self) -> None:
        scanned = [
            RESOURCES_ROOT / "catalog",
            RESOURCES_ROOT / "schemas",
            REPO_ROOT / "src",
            NIX_DIR / "devshell.nix",
            NIX_DIR / "gateway.nix",
            NIX_DIR / "package.nix",
            RESOURCES_ROOT / "settings.json",
            REPO_ROOT / "tests/gateway-check.nix",
            FLAKE_NIX,
        ]
        # charmer.legacy names the earlier Nix attributes only to keep them
        # out of everything else (its constants carry their lifetimes).
        legacy_module = REPO_ROOT / "src" / "charmer" / "legacy.py"
        blob = ""
        for path in scanned:
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file() and child != legacy_module and "__pycache__" not in child.parts:
                        blob += child.read_text(errors="replace")
            else:
                blob += path.read_text(errors="replace")
        for identifier in REMOVED_IDENTIFIERS + REMOVED_ALIASES:
            with self.subTest(identifier=identifier):
                self.assertNotIn(identifier, blob)

    def test_retained_aliases_present_in_catalog(self) -> None:
        # A 2.x selector that left the live lines stays in the retired
        # map (served as continuity), so retired.json is part of the blob.
        # Retired selectors keep the earlier product prefix (recorded names).
        from charmer import legacy

        blob = (RESOURCES_ROOT / "catalog" / "models.json").read_text()
        blob += (RESOURCES_ROOT / "catalog" / "providers.json").read_text()
        blob += (RESOURCES_ROOT / "catalog" / "retired.json").read_text()
        for retained in (
            "claude-fable-5",
            "claude-opus-4-8",
            legacy.LEGACY_SELECTOR_PREFIX + "kimi-k3",
            legacy.LEGACY_SELECTOR_PREFIX + "opus-4-8",
            "gpt-multi-sol-high",
            "gpt-multi-sol-xhigh",
            "gpt-multi-gpt55-high",
        ):
            with self.subTest(alias=retained):
                self.assertIn(retained, blob)

    def test_plugin_dir_only_in_blocklist_contexts(self) -> None:
        offenders: list[str] = []
        for child in tree_files(REPO_ROOT):
            if child.suffix not in (".py", ".nix"):
                continue
            text = child.read_text()
            if "--plugin-dir" not in text:
                continue
            relative = child.relative_to(REPO_ROOT)
            allowed = (
                str(relative) == "src/charmer/compiler.py"
                or str(relative).startswith("tests/")
            )
            if not allowed:
                offenders.append(str(relative))
        self.assertEqual(offenders, [])


class AdmittedPatchGateTests(unittest.TestCase):
    """Exact package selections for admitted patches; no patch 11."""

    GATES = {'./sdk/cliproxy/auth': ['TestCM053CredentialSaveGenerationOrdering',
                             'TestCM053CredentialSaveFailedOncePerTransition',
                             'TestCM053CredentialSaveSupersededCompletionIsNoReceipt',
                             'TestCM053KeyedCompatConductorClassificationPreserved',
                             'TestCM053KeyedCompatAuthIdentityRedacted',
                             'TestCM053UpdateSchedulerSnapshotConcurrentReconcile',
                             'TestCM053RegisterSchedulerSnapshotConcurrentReconcile',
                             'TestCM053AuthSchedulerSnapshotOwnership'],
     './sdk/auth': ['TestCM053CredentialSaveCommitCallback',
                    'TestCM053CredentialSaveWatcherSeesOnlyDestination',
                    'TestCM053CredentialSaveSupersededDuringDirSync',
                    'TestCM053CredentialSaveSupersededReceiptWithFileStore'],
     './internal/upstreamredirect': ['TestCM053CredentialedRedirectOriginMatrix',
                                     'TestCM053MultiHopEscapeRefused',
                                     'TestCM053SameOriginKeepsHopLimit',
                                     'TestCM053KeyedCompatModeRefusesEveryRedirect',
                                     'TestCM053UnrestrictedModeKeepsGoDefault',
                                     'TestCM053FinalRedirectWithoutFollowIsRefused',
                                     'TestCM053LocationParseErrorShape',
                                     'TestCM053StripLocationForGenericAPI',
                                     'TestCM053RedirectNegativeControl',
                                     'TestCM053GuardCopiesClient'],
     './internal/runtime/executor': ['TestCM053ClaudeRedirectEntryPoints',
                                     'TestCM053CodexRedirectEntryPoints',
                                     'TestCM053CodexCompressedRetryRedirectRefused',
                                     'TestCM053GeminiVertexRedirectEntryPoints',
                                     'TestCM053OtherExecutorRedirectEntryPoints',
                                     'TestCM053KeyedCompatRefusesEveryRedirect',
                                     'TestCM053KeylessCompatRedirectBehaviorPreserved',
                                     'TestCM053CompatPreparedRequestCredentialsRefuseRedirects',
                                     'TestCM053CompatURLCredentialsRefuseRedirects',
                                     'TestCM053GenericHttpRequestStripsLocation',
                                     'TestCM053ExecutorClientFactoryCoverage',
                                     'TestCM053CompatChatRetentionFinalBoundary',
                                     'TestCM053CompatChatRetentionLateOverride',
                                     'TestCM053CompatChatRetentionDuplicateKeys',
                                     'TestCM053CompatChatRetentionFailureSendsNothing',
                                     'TestCM053CompatStreamAltStillSanitized',
                                     'TestCM053KeyedCompatHTTPErrorSafe',
                                     'TestCM053KeyedCompatHTTP200ErrorEnvelopeSafe',
                                     'TestCM053KeyedCompatSSEClassifiedBeforeCapture',
                                     'TestCM053KeyedCompatStreamFrameBounds',
                                     'TestCM053KeyedCompatHeadersSafe',
                                     'TestCM053KeyedCompatIOErrorsSafe',
                                     'TestCM053KeyedCompatRetryClassificationPreserved',
                                     'TestCM053KeyedCompatLoggingMatrix',
                                     'TestCM053KeylessCompatErrorBehaviorPreserved',
                                     'TestCM053KeyedCompatAuditScope',
                                     'TestCM053KeyedCompatAmbiguousPayloadSafe',
                                     'TestOpenAICompatExecutorStreamRejectsPlainJSONAfterBlankLines',
                                     'TestOpenAICompatExecutorResponsesStreamPreservesUpstreamDataError',
                                     'TestOpenAICompatExecutorResponsesStreamPreservesNamedErrorEvent',
                                     'TestOpenAICompatExecutorResponsesStreamHandlesAdditionalErrorShapes',
                                     'TestCM053SetupTokenMetadataConcurrentPreparation',
                                     'TestClaudeExecutorPrepareRequestAuthIsRaceFreeOnSharedCredential',
                                     'TestClaudeExecutorSharedCredentialMetadataReadersUseOneLock',
                                     'TestClaudeExecutorSharedCredentialMetadataMixedAccess',
                                     'TestCM053SetupTokenMetadataFlagSemantics',
                                     'TestCM053SetupTokenMetadataLockNotHeldAcrossProfileFetch',
                                     'TestCM053ManagerPrepareConcurrentReaders'],
     './internal/runtime/executor/helps': ['TestCM053HelpsClientFactoriesCarrySameOriginPolicy',
                                           'TestCM053UsageWrapperPreservesRedirectPolicy',
                                           'TestCM053DecoratedClientsKeepRedirectPolicy'],
     './internal/auth/claude': ['TestCM053OAuthRefreshRefusesCrossOriginRedirect'],
     './internal/auth/codex': ['TestCM053OAuthRefreshRefusesCrossOriginRedirect'],
     './internal/compatsafe': ['TestCM053CompatSafeKindsFixed',
                               'TestCM053CompatSafeReadErrorBodyBound',
                               'TestCM053CompatSafeResponseHeaders',
                               'TestCM053CompatSafeTransport',
                               'TestCM053CompatSafeRequestRedaction',
                               'TestCM053CompatSafeClassify'],
     './internal/api': ['TestCM053ReloadConcurrentHomeHeartbeat',
                        'TestCM053ReloadConcurrentUnifiedModels',
                        'TestCM053ServerConfigSnapshotOwnership'],
     './sdk/api/handlers': ['TestCM053ReloadConcurrentSDKHandlerConfig',
                            'TestCM053ServedReadersUseRequestSnapshot']}

    # Pin all overlay tests by owning package; a prefix-only go test may exit
    # successfully when one of these packages has no matching tests.
    OVERLAY_GATES = {
        "./internal/config": (
            "TestCM053OverlayInvalidRefusedAtLoad",
            "TestCM053OverlayShapeRefused",
            "TestCM053OverlayThinkingLevelsMatchParsers",
        ),
        "./internal/watcher": ("TestCM053OverlayReloadRefresh",),
        "./sdk/cliproxy": (
            "TestCM053OverlayRegistersClaudeModel",
            "TestCM053OverlayRegistersCodexModel",
            "TestCM053OverlayAliasVisibleInModels",
            "TestCM053OverlayAbsentNoEffect",
            "TestCM053OverlayStaticWins",
            "TestCM053OverlayExclusionsApply",
            "TestCM053OverlayGlobalExclusionsAuthoritativeForSeededRecord",
            "TestCM053OverlayCapabilityThroughAlias",
            "TestCM053OverlayPassThroughClaude",
            "TestCM053OverlayPassThroughCodex",
            "TestCM053OverlayReloadRefreshRegistry",
        ),
    }

    SHUTDOWN_RACE_GATES = {
        "race-refresh-shutdown": ("./sdk/cliproxy/auth", [
            "TestRefreshShutdownWaitsForExecutor", "TestRefreshShutdownWaitsForDurableSave",
            "TestRefreshShutdownSkipsQueuedJobs", "TestRefreshShutdownCanceledAuthLockWaitDoesNotStartRefresh",
            "TestRefreshShutdownDeadline", "TestRefreshShutdownRepeatedStop", "TestRefreshShutdownPreviousRunIsJoined",
            "TestRefreshShutdownFencesRequestAdmission", "TestRefreshShutdownRequestAdmissionRace",
            "TestRefreshShutdownBackgroundStopKeepsRequest",
        ]),
        "race-service-refresh-shutdown": ("./sdk/cliproxy", ["TestServiceShutdownJoinsRefresh",
                                                             "TestServiceShutdownJoinsRequestRefresh"]),
        "race-refresh-shutdown-file-store": ("./sdk/auth", ["TestRefreshShutdownFileTokenStore"]),
    }

    def test_exact_race_gates(self):
        races = [record for record in recipe()["gates"] if record["race"]]
        by_name = {record["name"]: record for record in races}
        by_packages = {tuple(record["packages"]): record for record in races
                       if record["name"] not in self.SHUTDOWN_RACE_GATES}
        for package, names in self.GATES.items():
            with self.subTest(package=package):
                record = by_packages[(package,)]
                self.assertEqual(record["run"], f"^({'|'.join(names)})$")
                self.assertEqual(record["expected_tests"], names)
                self.assertEqual((record["count"], record["platforms"]), (100, "linux"))
        overlay = by_packages[("./internal/config", "./internal/watcher", "./sdk/cliproxy")]
        self.assertEqual(overlay["run"], "^TestCM053Overlay")
        self.assertEqual(set(overlay["expected_tests"]),
                         {name for names in self.OVERLAY_GATES.values() for name in names})
        for name, (package, names) in self.SHUTDOWN_RACE_GATES.items():
            with self.subTest(gate=name):
                record = by_name[name]
                self.assertEqual(record["packages"], [package])
                self.assertEqual(record["run"], f"^({'|'.join(names)})$")
                self.assertEqual(record["expected_tests"], names)
                self.assertEqual((record["count"], record["platforms"]), (100, "linux"))
        self.assertEqual(len(races), len(self.GATES) + 1 + len(self.SHUTDOWN_RACE_GATES))
        self.assertNotIn("cli-proxy-api-retry-after.patch", admitted_names())

    def test_no_antigravity_egress_gate(self):
        names = ("TestCM053NoAntigravityEgressLocalModel",
                 "TestCM053NoAntigravityEgressRemoteModel",
                 "TestLocalModeNoEgressSDKLocalModel",
                 "TestLocalModeNoEgressSDKRemoteModel",
                 "TestCM053LocalModeStartModelsUpdaterReadsEmbedded",
                 "TestCM053LocalModeRefusesRemoteCatalogSources",
                 "TestCM053LocalModeOnDemandRefreshRefusesRemoteSources")
        record = gate("no-antigravity-egress")
        self.assertEqual(record["packages"], ["./cmd/server", "./sdk/cliproxy", "./internal/registry"])
        self.assertEqual(record["run"], f"^({'|'.join(names)})$")
        self.assertEqual(record["expected_tests"], list(names))
        patch = (PATCH_DIR / "cli-proxy-api-no-antigravity-egress.patch").read_text()
        self.assertEqual(set(re.findall(r"^\+func (Test\w*NoAntigravityEgress\w+|Test\w*NoEgress\w+|TestCM053LocalMode\w+)\(",
                                       patch, re.MULTILINE)), set(names))
        # The SDK path's catalog updaters read the same policy,
        # and an explicit remote catalog source is refused in local mode.
        self.assertIn("+func ReplaceCatalogFetchers(", patch)
        self.assertIn("+func refuseRemoteCatalogSources(sources CatalogSources) CatalogSources {", patch)
        # One policy, set once from --local-model before any updater starts.
        self.assertIn("+++ b/internal/localmode/localmode.go", patch)
        self.assertEqual(patch.count("+\tlocalmode.Set(localModel)\n"), 1)
        # Both ordinary and embedded TUI servers start updaters through it.
        self.assertEqual(patch.count(
            "+\t\t\t\tstartBackgroundUpdaters(localModel, configFilePath, defaultBackgroundUpdaters)"), 1)
        self.assertEqual(patch.count(
            "+\t\t\tstartBackgroundUpdaters(localModel, configFilePath, defaultBackgroundUpdaters)"), 1)
        # Every network-refreshing starter reads it: Antigravity, the npm
        # (Grok CLI) updater in the SDK and executor paths, the management
        # asset updater and the latest-version lookup.
        for path in ("internal/misc/antigravity_version.go", "internal/runtime/executor/helps/xai_version.go",
                     "internal/managementasset/updater.go", "sdk/cliproxy/service_lifecycle.go",
                     "internal/api/handlers/management/config_basic.go", "cmd/server/main.go",
                     "internal/registry/catalog_sources.go", "internal/registry/model_updater.go"):
            with self.subTest(path=path):
                chunk = patch.split("+++ b/" + path + "\n", 1)[1].split("\ndiff --git ", 1)[0]
                self.assertIn("+\tif localmode.NoEgress() {", chunk)

    def test_no_antigravity_row_requires_explicit_selection(self):
        from unittest import mock
        import _gateway_harness as harness
        name = "test_local_model_never_starts_antigravity_updater"
        for required in (False, True):
            with self.subTest(required=required), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(harness, "GatewayHarness") as create:
                if required:
                    os.environ[harness.REQUIRE_ENV] = "1"
                with self.assertRaises(AssertionError if required else unittest.SkipTest):
                    getattr(NoAntigravityEgressTests(name), name)()
                create.assert_not_called()
        row = f"tests.test_packaging.NoAntigravityEgressTests.{name}"
        self.assertIn(row, harness.gateway_check_selections())
        self.assertEqual(harness.PATCH_ROWS["cli-proxy-api-no-antigravity-egress.patch"], (row,))

    CREDENTIAL_GOLDEN_ROOTS = {
        "sdk/auth": (
            "TestCM053CredentialSaveFullWriteReceipt", "TestCM053CredentialSaveLiteralJSONControl"),
        "sdk/cliproxy/auth": (
            "TestCM053CredentialSaveLineEndsAtEpoch", "TestCM053CredentialSaveLiteralEventControl"),
    }

    # Refuse Go filename platform constraints, including historical port names,
    # rather than infer eligibility from this test runner's host platform.
    CREDENTIAL_GO_FILE_TAGS = frozenset((
        "aix android darwin dragonfly freebsd hurd illumos ios js linux nacl netbsd openbsd "
        "plan9 solaris wasip1 windows zos 386 amd64 amd64p32 arm armbe arm64 arm64be loong64 "
        "mips mipsle mips64 mips64le mips64p32 mips64p32le ppc ppc64 ppc64le riscv riscv64 "
        "s390 s390x sparc sparc64 wasm"
    ).split())

    def _credential_golden_sources(self, patch):
        """Unique ordinary declarations in complete, unconditional owning files.

        This replaces a basename pin with a declaration-based static contract:
        legal neutral filenames may change. It is not a general Go parser;
        compilation/listing in the unchanged two-package gate remains authoritative.
        Return the original added text, never the lexical mask, for the goldens.
        """
        build = load_tool("build")
        scan = load_tool("history_scan")
        try:
            files = build.patch_files(patch)
        except build.BuildError as exc:
            self.fail(f"invalid credential patch: {exc}")
        paths = [entry["path"] for entry in files]
        self.assertEqual(len(paths), len(set(paths)), "repeated patch file sections")
        lines = patch.encode("utf-8").splitlines(keepends=True)
        boundaries = [index for index, line in enumerate(lines) if line.startswith(b"diff --git ")]
        self.assertTrue(boundaries, "credential patch has no sections")
        self.assertEqual(boundaries[0], 0, "credential patch must not have a wrapper")
        self.assertEqual(len(boundaries), len(files))
        owners = {name: directory for directory, names in self.CREDENTIAL_GOLDEN_ROOTS.items() for name in names}
        declarations = re.compile(rb"(?m)^[ \t]*func[ \t]+(" + b"|".join(name.encode("ascii") for name in owners) + rb")\b")
        found = {name: [] for name in owners}
        raw_sources = {}
        for entry, start, end in zip(files, boundaries, [*boundaries[1:], len(lines)]):
            section = lines[start:end]
            header = scan._PATCH_HEADER.fullmatch(section[0])
            self.assertIsNotNone(header, "invalid outer patch header")
            self.assertTrue(scan._valid_patch_section(section, header), "incomplete or unsupported patch section")
            path = entry["path"]
            self.assertEqual(header.group(2).decode("ascii"), path)
            if entry["change"] != "added" or not path.endswith(".go"):
                continue
            # The section validator requires one full, addition-only hunk for
            # a new file, so every byte below belongs to its original source.
            hunk, = [index for index, line in enumerate(section) if line.startswith(b"@@ ")]
            source = b"".join(line[1:] for line in section[hunk + 1:])
            if not any(name.encode("ascii") in source for name in owners):
                continue
            code = scan._go_code(source)
            self.assertIsNotNone(code, "required roots need lexically established Go code")
            matches = list(declarations.finditer(code))
            if not matches:
                continue
            directory, _, basename = path.rpartition("/")
            self.assertTrue(basename.endswith("_test.go") and not basename.startswith((".", "_")),
                            "required roots must be in eligible Go test files")
            # Go examines the part before the first dot, then strips a final
            # _test before checking OS/architecture suffixes after an underscore.
            suffixes = basename.split(".", 1)[0].split("_")[1:]
            if suffixes and suffixes[-1] == "test":
                suffixes.pop()
            self.assertFalse(suffixes and suffixes[-1] in self.CREDENTIAL_GO_FILE_TAGS,
                             "required roots must not have filename build constraints")
            package = scan._GO_PACKAGE.match(code)
            self.assertIsNotNone(package, "required roots need a real package declaration")
            self.assertEqual(package.group(1), b"auth", "required roots belong to package auth")
            self.assertIsNone(re.search(rb"(?m)^[ \t]*//[ \t]*(?:go:build|\+build)\b",
                                        source[:package.start(1)]), "required roots must be unconditional")
            # Only top-level ordinary functions with the required test signature
            # count. Masked comments/literals cannot introduce declarations or
            # alter delimiter depth; no Go syntax/type checking is claimed here.
            starts = {match.start() for match in matches}
            stack = []
            closing = {ord("}"): ord("{"), ord(")"): ord("("), ord("]"): ord("[")}
            for offset, byte in enumerate(code):
                if offset in starts:
                    self.assertFalse(stack, "required roots must be top-level ordinary functions")
                if byte in (ord("{"), ord("("), ord("[")):
                    stack.append(byte)
                elif byte in closing:
                    self.assertTrue(stack and stack.pop() == closing[byte], "unbalanced Go source")
            self.assertFalse(stack, "incomplete Go source")
            for match in matches:
                name = match.group(1).decode("ascii")
                self.assertEqual(directory, owners[name], f"{name}: wrong owning directory")
                self.assertIsNotNone(re.match(rb"[ \t]*\([ \t]*t[ \t]+\*[ \t]*testing\.T[ \t]*\)[ \t]*\{",
                                              code[match.end():]), f"{name}: require an ordinary func(t *testing.T)")
                found[name].append(path)
            raw_sources[path] = source.decode("utf-8")
        selected = {}
        for directory, names in self.CREDENTIAL_GOLDEN_ROOTS.items():
            for name in names:
                self.assertEqual(len(found[name]), 1, f"{name}: require exactly one declaration")
            locations = {path for name in names for path in found[name]}
            self.assertEqual(len(locations), 1, f"{directory}: both roots must share one unique file")
            path, = locations
            selected[path] = raw_sources[path]
        return selected

    def _assert_credential_golden_gate(self, record):
        self.assertEqual(record["packages"], ["./sdk/auth", "./sdk/cliproxy/auth"])
        self.assertFalse(record.get("run"), "retain the complete existing package selection")
        self.assertFalse(record.get("skip"), "do not narrow the complete package selection")
        expected = {root for group in self.CREDENTIAL_GOLDEN_ROOTS.values() for root in group}
        self.assertEqual(set(record["expected_tests"]), expected)
        self.assertEqual(len(record["expected_tests"]), len(expected))

    def _assert_credential_literal_goldens(self, sources):
        # These source tripwires are not a proof of every golden byte. The
        # unchanged Go tests own persisted-byte/event comparisons and mutations.
        stored, = (source for path, source in sources.items() if path.rpartition("/")[0] == "sdk/auth")
        receipt = stored.split("func TestCM053CredentialSaveFullWriteReceipt(", 1)[1].split("\nfunc ", 1)[0]
        self.assertIn("want := []byte(fxFullWriteJSONGolden)", receipt)
        self.assertNotIn("json.Marshal", receipt)
        self.assertIn('const fxFullWriteJSONGolden = `{"access_token":', stored)
        self.assertIn("return bytes.Equal(got, want)", stored)
        events, = (source for path, source in sources.items() if path.rpartition("/")[0] == "sdk/cliproxy/auth")
        self.assertIn('auth.Index = "0123456789abcdef"', events)
        self.assertIn("fxLiteralEventMatches(raw, fxLiteralEventBodies[index])", events)
        for operation, result in (("refresh", "persisted"), ("result", "failed")):
            self.assertIn(f'"credential_save_v1 operation={operation} result={result} provider=claude '
                          "auth_index=0123456789abcdef", events)
        self.assertIn('bytes.Replace(line, []byte("epoch=7"), []byte("epoch=8"), 1)', events)

    def test_credential_literal_goldens_are_required_by_existing_gate(self):
        self._assert_credential_golden_gate(gate("credential-store"))
        patch = (PATCH_DIR / "cli-proxy-api-credential-save-report.patch").read_bytes().decode("utf-8")
        self._assert_credential_literal_goldens(self._credential_golden_sources(patch))

    def _credential_file_patch(self, path, source):
        """One synthetic complete new file; never writes or changes the real patch."""
        self.assertTrue(source.endswith("\n"))
        lines = source.splitlines(keepends=True)
        return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
                f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
                + "".join("+" + line for line in lines))

    def _credential_fixture_files(self):
        patch = (PATCH_DIR / "cli-proxy-api-credential-save-report.patch").read_bytes().decode("utf-8")
        sources = self._credential_golden_sources(patch)
        self._assert_credential_literal_goldens(sources)  # intact positive before mutation controls
        renamed = patch
        files = {}
        for path, source in sources.items():
            neutral = path.rpartition("/")[0] + "/credential_golden_test.go"
            # Rename only these added-file headers in the complete public patch;
            # the selected source and every other section stay byte-for-byte.
            for before, after in (
                (f"diff --git a/{path} b/{path}\n", f"diff --git a/{neutral} b/{neutral}\n"),
                (f"+++ b/{path}\n", f"+++ b/{neutral}\n"),
            ):
                self.assertEqual(renamed.count(before), 1)
                renamed = renamed.replace(before, after, 1)
            self.assertNotIn(neutral, files)
            files[neutral] = source
        self.assertEqual(self._credential_golden_sources(renamed), files)
        synthetic = "".join(self._credential_file_patch(path, source) for path, source in files.items())
        self.assertEqual(self._credential_golden_sources(synthetic), files)
        return files

    def test_credential_golden_selection_accepts_neutral_filenames(self):
        files = self._credential_fixture_files()
        patch = "".join(self._credential_file_patch(path, source) for path, source in files.items())
        selected = self._credential_golden_sources(patch)
        self.assertEqual(selected, files)
        self._assert_credential_literal_goldens(selected)

    def test_credential_golden_selection_rejects_wrong_ownership(self):
        files = self._credential_fixture_files()
        for path, source in files.items():
            directory = path.rpartition("/")[0]
            others = "".join(self._credential_file_patch(name, text) for name, text in files.items() if name != path)
            cases = (
                ("nested", directory + "/nested/credential_golden_test.go", source),
                ("other-directory", "sdk/other_auth/credential_golden_test.go", source),
                ("external-package", path, source.replace("package auth\n", "package auth_test\n", 1)),
                ("wrong-package", path, source.replace("package auth\n", "package other\n", 1)),
                ("comment-package", path, source.replace("package auth\n", "// package auth\npackage other\n", 1)),
            )
            for label, changed_path, changed_source in cases:
                with self.subTest(path=path, case=label), self.assertRaises(AssertionError):
                    self._credential_golden_sources(others + self._credential_file_patch(changed_path, changed_source))

    def test_credential_golden_selection_rejects_missing_or_fake_declarations(self):
        files = self._credential_fixture_files()
        for path, source in files.items():
            directory = path.rpartition("/")[0]
            others = "".join(self._credential_file_patch(name, text) for name, text in files.items() if name != path)
            with self.subTest(path=path, case="missing-file"), self.assertRaises(AssertionError):
                self._credential_golden_sources(others)
            for name in self.CREDENTIAL_GOLDEN_ROOTS[directory]:
                declaration = f"func {name}(t *testing.T) {{"
                self.assertEqual(source.count(declaration), 1)
                renamed = source.replace(declaration, f"func Renamed{name}(t *testing.T) {{", 1)
                fake = declaration + "}\n"
                cases = (
                    ("renamed", renamed),
                    ("line-comment", renamed + "\n// " + fake),
                    ("block-comment", renamed + "\n/*\n" + fake + "*/\n"),
                    ("raw-string", renamed + "\nvar fixtureDeclarationText = `\n" + fake + "`\n"),
                    ("quoted-string", renamed + f'\nvar fixtureDeclarationText = "{fake.rstrip()}"\n'),
                    ("wrong-signature", source.replace(declaration, f"func {name}(b *testing.B) {{", 1)),
                    ("generic", source.replace(declaration, f"func {name}[T any](t *testing.T) {{", 1)),
                    ("receiver", renamed + f"\ntype fixtureReceiver struct{{}}\nfunc (fixtureReceiver) {name}(t *testing.T) {{}}\n"),
                    ("nested", renamed + "\nfunc fixtureNested() {\n" + fake + "}\n"),
                )
                for label, changed in cases:
                    with self.subTest(name=name, case=label), self.assertRaises(AssertionError):
                        self._credential_golden_sources(others + self._credential_file_patch(path, changed))

    def test_credential_golden_selection_rejects_ambiguity_and_split_pairs(self):
        files = self._credential_fixture_files()
        patch = "".join(self._credential_file_patch(path, source) for path, source in files.items())
        for path, source in files.items():
            directory = path.rpartition("/")[0]
            first, second = self.CREDENTIAL_GOLDEN_ROOTS[directory]
            others = "".join(self._credential_file_patch(name, text) for name, text in files.items() if name != path)
            split = others + "".join(self._credential_file_patch(directory + f"/part_{index}_test.go",
                f'package auth\n\nimport "testing"\n\nfunc {name}(t *testing.T) {{}}\n')
                for index, name in enumerate((first, second)))
            cases = (
                ("duplicate-declaration", others + self._credential_file_patch(path,
                    source + f"\nfunc {first}(t *testing.T) {{}}\n")),
                ("duplicate-wrong-signature", others + self._credential_file_patch(path,
                    source + f"\nfunc {first}(b *testing.B) {{}}\n")),
                ("ambiguous-file", patch + self._credential_file_patch(directory + "/other_golden_test.go", source)),
                ("repeated-section", patch + self._credential_file_patch(path, source)),
                ("split-pair", split),
            )
            for label, changed in cases:
                with self.subTest(path=path, case=label), self.assertRaises(AssertionError):
                    self._credential_golden_sources(changed)

    def test_credential_golden_selection_rejects_incomplete_patch_sections(self):
        files = self._credential_fixture_files()
        patch = "".join(self._credential_file_patch(path, source) for path, source in files.items())
        extra = self._credential_file_patch("other/fixture.txt", "synthetic\n")
        cases = (
            ("missing-newline", patch[:-1]),
            ("truncated-hunk", patch.rsplit("\n", 2)[0] + "\n"),
            ("late-truncated-non-go", patch + extra.rsplit("\n", 2)[0] + "\n"),
            ("late-incomplete-header", patch + "diff --git a/other/fixture.txt b/other/fixture.txt\n"),
            ("repeated-non-go", patch + extra + extra),
            ("wrapper", "synthetic wrapper\n" + patch),
        )
        for label, changed in cases:
            with self.subTest(case=label), self.assertRaises(AssertionError):
                self._credential_golden_sources(changed)

    def test_credential_golden_selection_rejects_ineligible_or_constrained_files(self):
        files = self._credential_fixture_files()
        for path, source in files.items():
            directory = path.rpartition("/")[0]
            others = "".join(self._credential_file_patch(name, text) for name, text in files.items() if name != path)
            for basename in ("credential_golden.go", "credential_golden_test.go.txt", "_credential_golden_test.go",
                             ".credential_golden_test.go", "credential_golden_linux_test.go",
                             "credential_golden_windows_test.go", "credential_golden_aix_test.go",
                             "credential_golden_amd64_test.go", "credential_golden_386_test.go",
                             "credential_golden_linux_arm64_test.go", "credential_golden_mips64le_test.go",
                             "credential_golden_wasm_test.go", "credential_linux.extra_test.go"):
                with self.subTest(path=path, filename=basename), self.assertRaises(AssertionError):
                    self._credential_golden_sources(others + self._credential_file_patch(directory + "/" + basename, source))
            for constraint in ("//go:build linux\n\n", "// +build ignore\n\n", "//go:build !ignore\n\n"):
                with self.subTest(path=path, constraint=constraint), self.assertRaises(AssertionError):
                    self._credential_golden_sources(others + self._credential_file_patch(path, constraint + source))
            modified = self._credential_file_patch(path, source).replace("new file mode 100644\n", "", 1)
            modified = modified.replace("--- /dev/null\n", f"--- a/{path}\n", 1)
            with self.subTest(path=path, case="not-added"), self.assertRaises(AssertionError):
                self._credential_golden_sources(others + modified)

    def test_credential_literal_golden_tripwires_reject_bad_wiring(self):
        files = self._credential_fixture_files()
        mutations = (
            ("sdk/auth", "want := []byte(fxFullWriteJSONGolden)", 'want := []byte("changed")'),
            ("sdk/auth", "func TestCM053CredentialSaveFullWriteReceipt(t *testing.T) {\n",
             "func TestCM053CredentialSaveFullWriteReceipt(t *testing.T) {\n\t_ = json.Marshal\n"),
            ("sdk/auth", 'const fxFullWriteJSONGolden = `{"access_token":',
             'const fxFullWriteJSONGolden = `{"changed_token":'),
            ("sdk/auth", "return bytes.Equal(got, want)", "return true"),
            ("sdk/cliproxy/auth", 'auth.Index = "0123456789abcdef"', 'auth.Index = "fedcba9876543210"'),
            ("sdk/cliproxy/auth", "fxLiteralEventMatches(raw, fxLiteralEventBodies[index])",
             'fxLiteralEventMatches(raw, "changed")'),
            ("sdk/cliproxy/auth", '"credential_save_v1 operation=refresh result=persisted provider=claude auth_index=0123456789abcdef',
             '"credential_save_v1 operation=refresh result=persisted provider=claude auth_index=fedcba9876543210'),
            ("sdk/cliproxy/auth", '"credential_save_v1 operation=result result=failed provider=claude auth_index=0123456789abcdef',
             '"credential_save_v1 operation=result result=failed provider=claude auth_index=fedcba9876543210'),
            ("sdk/cliproxy/auth", 'bytes.Replace(line, []byte("epoch=7"), []byte("epoch=8"), 1)',
             'bytes.Replace(line, []byte("epoch=7"), []byte("epoch=9"), 1)'),
        )
        for directory, before, after in mutations:
            path = directory + "/credential_golden_test.go"
            self.assertIn(before, files[path])
            changed = {**files, path: files[path].replace(before, after)}
            patch = "".join(self._credential_file_patch(name, source) for name, source in changed.items())
            sources = self._credential_golden_sources(patch)  # ownership must still pass
            with self.subTest(directory=directory, mutation=before), self.assertRaises(AssertionError):
                self._assert_credential_literal_goldens(sources)

    def test_credential_literal_golden_gate_rejects_narrowed_selection(self):
        record = gate("credential-store")
        self._assert_credential_golden_gate(record)
        changes = [
            ("first-package-only", {"packages": record["packages"][:1]}),
            ("second-package-only", {"packages": record["packages"][1:]}),
            ("package-glob", {"packages": ["./sdk/auth/...", "./sdk/cliproxy/auth"]}),
            ("run-filter", {"run": "^TestCM053CredentialSave"}),
            ("skip-filter", {"skip": "TestCM053CredentialSaveLiteralJSONControl"}),
            ("duplicate-root", {"expected_tests": record["expected_tests"] + record["expected_tests"][:1]}),
        ]
        changes.extend(("missing-" + name, {"expected_tests": [root for root in record["expected_tests"] if root != name]})
                       for name in record["expected_tests"])
        for label, change in changes:
            with self.subTest(case=label), self.assertRaises(AssertionError):
                self._assert_credential_golden_gate({**record, **change})

    def test_credential_receipt_format_is_pinned(self):
        self.assertEqual(gate("credential-store")["packages"], ["./sdk/auth", "./sdk/cliproxy/auth"])
        self.assertEqual(gate("redirect-metadata")["packages"],
                         ["./internal/auth/claude", "./internal/auth/codex", "./internal/runtime/executor/helps"])
        pins = {pin["name"]: pin for pin in recipe()["source_pins"]}
        self.assertEqual(pins["credential-save-format"], {
            "name": "credential-save-format", "path": "sdk/cliproxy/auth/credential_save.go",
            "text": "credential_save_v1 operation=%s result=%s provider=%s auth_index=%s credentials_changed=%t "
                    "stage=%s errno=%d category=%s bytes=%d size=%d generation=%d epoch=%d"})


    def test_pinned_gate_tests_exist_in_admitted_patches(self):
        manifest = json.loads((RESOURCES_ROOT / "catalog/gateway.json").read_text())["gateway"]["patches"]
        added = "\n".join((PATCH_DIR / name).read_text() for name in manifest)
        for names in self.GATES.values():
            for name in names:
                if name.startswith("TestCM053"):
                    self.assertIn("+func " + name + "(", added)
        # The prefix gate succeeds even when a package has no matching tests.
        # Compare the admitted overlay patch's complete inventory to this pin,
        # then check each name in its owning package's added test hunks.
        overlay = next(name for name in manifest if name.endswith("cli-proxy-api-oauth-model-overlay.patch"))
        overlay_text = (PATCH_DIR / overlay).read_text()
        expected = {name for names in self.OVERLAY_GATES.values() for name in names}
        self.assertEqual(
            set(re.findall(r"^\+func (TestCM053Overlay\w+)\(", overlay_text, re.MULTILINE)),
            expected,
        )
        for package, names in self.OVERLAY_GATES.items():
            self.assertTrue(names, package)
            for name in names:
                with self.subTest(package=package, name=name):
                    self.assertIn("+func " + name + "(", added)
                    self.assertRegex(
                        overlay_text,
                        rf"(?ms)^diff --git a/{re.escape(package[2:])}/[^\n]+\n"
                        rf"(?:(?!^diff --git ).)*^\+func {name}\(",
                    )


class NoAntigravityEgressTests(unittest.TestCase):
    """The no-egress patch: shipped server, synthetic upstream, isolated network."""

    def test_local_model_never_starts_antigravity_updater(self):
        import _gateway_harness as harness
        # Never fall back to an installed binary for this explicitly selected build.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: patch 17 needs an explicitly selected manifest build")
        gateway = harness.GatewayHarness()
        try:
            # Readiness includes the watcher info log and a successful model
            # listing. The updater's URL is private upstream state with no
            # injection seam; its unconditional info log is our discriminator,
            # not a claim to have captured every socket attempt.
            text = gateway.log_path.read_text()
            self.assertIn(harness.WATCHER_READY, text)
            self.assertFalse("periodic antigravity version refresh started" in text,
                             "Antigravity updater started under --local-model")
            self.assertFalse("failed to refresh antigravity version" in text,
                             "Antigravity manifest refresh attempted under --local-model")
            # The same policy keeps the Grok CLI (npm) version updater off.
            self.assertFalse("periodic Grok CLI version refresh started" in text,
                             "Grok CLI updater started under --local-model")
            self.assertEqual(gateway.document["discovery"], {"enabled": False})
            self.assertNotIn("discovery:", text, "the LAN discovery advertiser ran")
            harness.EVIDENCE.record(__name__, "patches", {"row": "AG1", "updater_started": False})
            harness.EVIDENCE.finalize_module(__name__)
        finally:
            gateway.close()



class AntigravityLoopbackCallbackGateTests(unittest.TestCase):
    """The --antigravity-login callback listener binds 127.0.0.1 only."""

    NAME = "cli-proxy-api-antigravity-loopback-callback.patch"
    TESTS = ("TestAntigravityCallbackListenerBindsLoopback",
             "TestAntigravityCallbackUnreachableOutsideIPv4Loopback")

    def test_patch_binds_loopback_and_carries_its_gate_tests(self):
        patch = (PATCH_DIR / self.NAME).read_text()
        names = admitted_names()
        self.assertEqual(names[names.index(self.NAME) - 1], "cli-proxy-api-codex-client-identity.patch")
        self.assertIn("--- a/sdk/auth/antigravity.go", patch)
        self.assertIn('-\taddr := fmt.Sprintf(":%d", port)', patch)
        self.assertIn('+\taddr := fmt.Sprintf("127.0.0.1:%d", port)', patch)
        self.assertIn("+\tsrv := &http.Server{Addr: listener.Addr().String(), Handler: mux}", patch)
        self.assertEqual(re.findall(r"^\+func (Test\w+)\(", patch, re.MULTILINE), list(self.TESTS))
        self.assertIn("+++ b/sdk/auth/antigravity_callback_loopback_test.go", patch)

    def test_gate_selects_exactly_the_patch_tests(self):
        # The build tool lists the selection first and fails unless it equals
        # expected_tests, so the gate is red whenever the patch is omitted.
        record = gate("antigravity-loopback-callback")
        self.assertEqual(record["packages"], ["./sdk/auth"])
        self.assertEqual(record["run"], "^(" + "|".join(self.TESTS) + ")$")
        self.assertEqual(record["expected_tests"], list(self.TESTS))
        self.assertEqual((record["platforms"], record["race"], record["count"]), ("portable", False, None))

    def test_built_gateway_row_is_the_patch_row(self):
        import _gateway_harness as harness
        row = ("tests.test_gateway_patches.LoopbackOAuthListenerTests."
               "test_antigravity_login_callback_listens_on_loopback_only")
        self.assertEqual(harness.PATCH_ROWS[self.NAME], (row,))
        self.assertIn("tests.test_gateway_patches", harness.gateway_check_selections())  # the row's module
        self.assertIn("LO4", harness.REQUIRED_EVIDENCE["tests.test_gateway_patches"]["patches"])

    def test_built_gateway_row_requires_an_explicit_build(self):
        from unittest import mock
        import _gateway_harness as harness
        import test_gateway_patches
        name = "test_antigravity_login_callback_listens_on_loopback_only"
        for required in (False, True):
            with self.subTest(required=required), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(test_gateway_patches.LoopbackOAuthListenerTests, "check_login") as login:
                if required:
                    os.environ[harness.REQUIRE_ENV] = "1"
                with self.assertRaises(AssertionError if required else unittest.SkipTest):
                    getattr(test_gateway_patches.LoopbackOAuthListenerTests(name), name)()
                login.assert_not_called()


class CodexIdentityGateTests(unittest.TestCase):
    def test_gate_and_omission_selections_nonempty(self):
        patch = (PATCH_DIR / "cli-proxy-api-codex-client-identity.patch").read_text()
        names = tuple("TestCM055CodexIdentity" + suffix for suffix in (
            "HTTPFinalHeaders", "WebsocketFinalHeaders", "VersionDefault",
            "CloakingPrecedence", "RoutingHintFinal", "StaticOverrideException"))
        self.assertEqual(set(re.findall(r"^\+func (TestCM055\w+)\(", patch, re.M)), set(names))
        # The build tool lists the selection before running it and fails
        # unless it equals expected_tests (go test accepts an empty selection).
        record = gate("codex-identity")
        self.assertEqual(record["packages"], ["./internal/runtime/executor"])
        self.assertEqual(record["run"], "^(" + "|".join(names) + ")$")
        self.assertEqual(record["expected_tests"], list(names))
        diagnostic = (REPO_ROOT / "tests/gateway-startup-probe.nix").read_text()
        self.assertIn("cp ${./gateway_codex_identity_probe_test.go} sdk/cliproxy/gwtest_codex_probe_test.go", diagnostic)
        self.assertIn("cp ${./gateway_v8_traffic_probe_test.go} sdk/cliproxy/gwtest_v8_traffic_probe_test.go", diagnostic)
        traffic = (REPO_ROOT / "tests/gateway_v8_traffic_probe_test.go").read_text()
        self.assertIn("func TestGatewayV8NativeConfigurationUpdateBoundary(", traffic)
        source = (REPO_ROOT / "tests/gateway_codex_identity_probe_test.go").read_text()
        self.assertIn("func TestOmissionCodexIdentity(", source)
        # 8.x's routing hint is part of the final-header inventory.
        self.assertIn('"X-Codex-Routing-Hint": "model=codexprobe-omission",', source)
        self.assertIn('"X-Codex-Routing-Hint": hint}', patch)
        self.assertNotIn("func TestOmissionCodexIdentity(", patch)
        from charmer import proxy
        self.assertIn('"Version", "' + proxy.CODEX_CLIENT_VERSION + '"', patch)
        self.assertIn("codex-tui/" + proxy.CODEX_CLIENT_VERSION, patch)


class CodexApiKeySafetyGateTests(unittest.TestCase):
    """Codex API-key auths: fixed local failure text, bounded error-body
    reads and the plain API-key client identity (no codex Version default,
    no session header when cloaking is off)."""

    NAME = "cli-proxy-api-codex-api-key-safety.patch"
    TESTS = ("TestCodexKeyStatusFailureIsLocal", "TestCodexKeyTerminalFailureIsLocal",
             "TestCodexKeyTransportErrorIsLocal", "TestCodexKeyErrorBodyReadIsBounded",
             "TestCodexPlainKeyIdentity", "TestCodexKeyTerminalFailureCapture", "TestCodexKeySuccessAndAccountCapture",
             "TestCodexKeyDecoderFailureIsLocal", "TestCodexKeyCompressedFailureBodyIsLocal")
    PREREQUISITES = ("cli-proxy-api-credentialed-redirects.patch", "cli-proxy-api-openai-compat-keyed-safety.patch",
                     "cli-proxy-api-codex-client-identity.patch")

    def test_patch_is_eighteenth_after_its_prerequisites_and_carries_its_tests(self):
        patch = (PATCH_DIR / self.NAME).read_text()
        names = admitted_names()
        # Eighteenth since the plugin-host-locking patch (admission id 14) retired.
        self.assertEqual(names[17], self.NAME)
        self.assertTrue(all(name in names[:17] for name in self.PREREQUISITES))
        # The regenerated patch lists its files in path order (capture tests first).
        self.assertEqual(sorted(re.findall(r"^\+func (Test\w+)\(", patch, re.MULTILINE)), sorted(self.TESTS))
        self.assertIn("+++ b/internal/runtime/executor/codex_key_safety.go", patch)
        self.assertIn('+\t"github.com/router-for-me/CLIProxyAPI/v8/internal/compatsafe"', patch)
        # The codex client Version default is replaced, never removed for accounts.
        self.assertIn('-\tmisc.EnsureHeader(r.Header, ginHeaders, "Version", "0.159.1")', patch)
        self.assertIn('+\tmisc.EnsureHeader(r.Header, ginHeaders, "Version", codexVersionDefault(cfg, auth))', patch)
        from charmer import proxy
        self.assertIn('+const codexClientVersion = "' + proxy.CODEX_CLIENT_VERSION + '"', patch)

    def test_gate_selects_exactly_the_patch_tests(self):
        # The build tool lists the selection first and fails unless it equals
        # expected_tests, so the gate is red whenever the patch is omitted.
        record = gate("codex-api-key-safety")
        self.assertEqual(record["packages"], ["./internal/runtime/executor"])
        self.assertEqual(record["run"], "^(" + "|".join(self.TESTS) + ")$")
        self.assertEqual(record["expected_tests"], list(self.TESTS))
        self.assertEqual((record["platforms"], record["race"], record["count"]), ("portable", False, None))

    def test_built_gateway_row_and_declared_prerequisites(self):
        import _gateway_harness as harness
        row = "tests.test_gateway_patches.CodexApiKeySafetyTests.test_key_route_failures_and_identity"
        self.assertEqual(harness.PATCH_ROWS[self.NAME], (row,))
        self.assertIn("tests.test_gateway_patches", harness.gateway_check_selections())
        self.assertIn("CK1", harness.REQUIRED_EVIDENCE["tests.test_gateway_patches"]["patches"])
        self.assertEqual(tuple(harness.PATCH_DEPENDENCIES[self.NAME]), self.PREREQUISITES)

    def test_built_gateway_row_requires_an_explicit_build(self):
        from unittest import mock
        import _gateway_harness as harness
        import test_gateway_patches
        name = "test_key_route_failures_and_identity"
        for required in (False, True):
            with self.subTest(required=required), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(harness, "GatewayHarness") as create:
                if required:
                    os.environ[harness.REQUIRE_ENV] = "1"
                with self.assertRaises(AssertionError if required else unittest.SkipTest):
                    getattr(test_gateway_patches.CodexApiKeySafetyTests(name), name)()
                create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
