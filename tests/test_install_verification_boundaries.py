"""Verifier authority and malformed-input controls, using signed private fixtures.

The real HTTPS transport is exercised with an in-memory opener, never a socket.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import urllib.request
from unittest import mock

import charmer
from _fake_release import FakeRelease
from _layout import REPO_ROOT
from charmer import install_verify, release_update, tui
from charmer.cli.screens import findings
from test_install_verification import json_bytes, put
from test_install_verification_cli import _VerificationCase, snapshot
from test_tui import FakeWindow, RIGHT

PLATFORMS = ("linux", "darwin")
ESC, ENTER = "\x1b", "\n"


class VerificationBoundaryTests(_VerificationCase):
    @contextlib.contextmanager
    def metadata_transport(self, *, response=None):
        real = release_update.HttpsTransport
        urls = []

        def opener(url, **kwargs):
            urls.append(url)
            if response is not None:
                return response(url)
            return io.BytesIO((self.fixture.dir / url.rsplit("/", 1)[-1]).read_bytes())

        def make(*args, **kwargs):
            transport = real(*args, **kwargs)
            transport._opener = opener
            return transport

        with mock.patch.object(release_update, "HttpsTransport", side_effect=make):
            yield urls

    def report(self, *, online=False):
        try:
            code, output, error = (self.invoke(["--verify", "--json"], online=True) if online else
                                   self.offline("--json"))
        except (ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
            self.fail(f"expected boundary failure escaped the shared report: {type(exc).__name__}")
        self.assertTrue(output, "verification returned no JSON document")
        document = json.loads(output)
        self.assertEqual(code, document["exit_code"])
        return document, error

    def screen_report(self, *, online=False):
        runtime = SimpleNamespace(environ=self.env, home=Path(self.env["HOME"]),
                                  _session_store_root=Path(self.env["HOME"]) / ".local/state/charmer")
        keys = ([RIGHT, RIGHT, ENTER, RIGHT, ENTER, ESC] if online else
                [RIGHT, ENTER, *str(self.fixture.dir), ENTER, ESC])
        shown = []
        real_view = tui.TextView

        class Recording(real_view):
            def run(self, win):
                shown.append(tuple(self.lines))
                return super().run(win)

        before = snapshot(self.base)
        with self.boundary(online=online), mock.patch.object(tui, "TextView", Recording):
            try:
                findings.run_install_verification(runtime, FakeWindow(keys), tui.MONO_PALETTE)
            except (ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
                self.fail(f"expected boundary failure escaped TUI: {type(exc).__name__}")
        self.assertEqual(before, snapshot(self.base))
        self.assertEqual(len(shown), 1)
        return shown[0]

    def assert_shared(self, document, lines):
        result = install_verify.Verification(document["version"], document["target"],
                                            tuple(install_verify.Check(**check) for check in document["checks"]))
        self.assertEqual(result.lines(), lines)

    def canonical_urls(self):
        # Build input is a test comparison, never the runtime authority.
        product = json.loads((REPO_ROOT / "packaging/product.json").read_text())
        base = product["release_base_url"].replace("{version}", self.fixture.version)
        return [base + "/" + name for name in ("SHA256SUMS", "SHA256SUMS.sshsig", "MANIFEST.json")]

    def test_untrusted_inner_location_cannot_select_origin_latest_or_malformed_url(self):
        locations = ("https://attacker.invalid/releases/v{version}",
                     "https://github.com/nkoturovic/charmer/releases/latest/download/MANIFEST.json?v={version}",
                     "https://github.com/nkoturovic/charmer/releases/latest/download#v={version}",
                     "https://[invalid]/{version}")
        for location in locations:
            with self.subTest(location=location):
                self.fixture.inner["release"]["base_url"] = location
                put(self.fixture.inner_path, json_bytes(self.fixture.inner))  # deliberately not authenticated
                with self.metadata_transport() as urls:
                    document, error = self.report(online=True)
                    self.assertEqual(urls, self.canonical_urls(), "untrusted manifest selected verification URLs")
                    self.assertEqual(document["status"], "mismatch")
                    self.assertEqual(document["checks"][-1]["component"], "inner-manifest")
                    self.assertNotIn("attacker.invalid", error)
                    self.assertIn("contacting github.com", error)
                    self.assert_shared(document, self.screen_report(online=True))
                    self.assertEqual(urls, self.canonical_urls() * 2)

    def test_authentic_bundle_uses_canonical_code_location_with_no_manifest_location(self):
        self.fixture.inner.pop("release")
        self.fixture.authenticate_inner()
        with self.metadata_transport() as urls:
            document, _ = self.report(online=True)
        self.assertEqual(document["exit_code"], 0)
        self.assertEqual(urls, self.canonical_urls())

    def observation_resources(self):
        resources = self.fixture.signers.parent.parent
        catalog = resources / "catalog/gateway.json"
        put(catalog, json_bytes({"gateway": {"base_url": "http://127.0.0.1:19999", "health_path": "/health"}}))
        digest, files = self.fixture.builder.content_digest(self.fixture.bundle, exclude=("MANIFEST.json",))
        self.fixture.inner["content"] = {"sha256": digest, "files": files}
        self.fixture.asset["content_sha256"] = digest
        self.fixture.authenticate_inner()
        return resources, catalog

    def test_invalid_packaged_observation_keeps_content_mismatch_and_shared_report(self):
        resources, catalog = self.observation_resources()
        with mock.patch.object(charmer, "resources_root", return_value=resources):
            good, _ = self.report()
            self.assertEqual(good["exit_code"], 0)
            for malformed in ({}, [], {"gateway": None}, {"gateway": {}},
                              {"gateway": {"base_url": 42}},
                              {"gateway": {"base_url": "https://[invalid]"}}):
                with self.subTest(malformed=malformed):
                    put(catalog, json_bytes(malformed))
                    document, _ = self.report()
                    self.assertEqual(document["exit_code"], 1, "content mismatch must outrank observation failure")
                    checks = {check["component"]: check for check in document["checks"]}
                    self.assertEqual(checks["bundle-content"]["status"], "mismatch")
                    self.assertEqual(checks["running-gateway"]["status"], "unavailable")
                    self.assert_shared(document, self.screen_report())

    def test_oversized_json_integer_is_a_sanitized_inner_manifest_mismatch(self):
        put(self.fixture.inner_path, b'{"format":' + b"1" * 5000 + b"}")
        document, error = self.report()
        self.assertEqual(document["exit_code"], 1)
        self.assertEqual(document["checks"][-1]["component"], "inner-manifest")
        self.assertNotIn("1111111111", json.dumps(document) + error)
        self.assert_shared(document, self.screen_report())

    def test_oversized_json_integer_in_signed_outer_manifest_is_a_mismatch(self):
        put(self.fixture.dir / "MANIFEST.json", b'{"format":' + b"1" * 5000 + b"}")
        FakeRelease.write_sums(self.fixture)
        FakeRelease.sign(self.fixture, self.key)
        document, _ = self.report()
        self.assertEqual(document["exit_code"], 1)
        self.assertEqual(document["checks"][-1]["component"], "signed-release")
        self.assert_shared(document, self.screen_report())

    def test_truncated_chunked_metadata_is_incomplete_on_cli_and_tui(self):
        responses = []
        broken = ["SHA256SUMS"]

        def response(url):
            if not url.endswith("/" + broken[0]):
                return io.BytesIO((self.fixture.dir / url.rsplit("/", 1)[-1]).read_bytes())
            fake = SimpleNamespace(makefile=lambda *a, **kw: io.BytesIO(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab"))
            result = http.client.HTTPResponse(fake)
            result.url = url
            result.begin()
            responses.append(result)
            return result

        with self.metadata_transport(response=response):
            for name in ("SHA256SUMS", "SHA256SUMS.sshsig", "MANIFEST.json"):
                with self.subTest(name=name):
                    broken[0] = name
                    document, error = self.report(online=True)
                    self.assertEqual(document["exit_code"], 3)
                    self.assertEqual(document["status"], "incomplete")
                    self.assertNotIn("IncompleteRead", json.dumps(document) + error)
                    self.assert_shared(document, self.screen_report(online=True))
            self.assertTrue(all(response.closed for response in responses))

    def test_online_keyboard_interrupt_remains_an_interrupt(self):
        def interrupted(url):
            raise KeyboardInterrupt

        with self.metadata_transport(response=interrupted):
            code, output, error = self.invoke(["--verify", "--json"], online=True)
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        self.assertIn("cancelled", error)


class VerificationLocationTests(unittest.TestCase):
    def canonical_base(self):
        product = json.loads((REPO_ROOT / "packaging/product.json").read_text())
        return product["release_base_url"].replace("{version}", "2.0.1")

    def test_location_validation_rejects_wrong_origin_latest_query_fragment_and_malformed_urls(self):
        base = self.canonical_base()
        bad = ("https://attacker.invalid/releases/download/v2.0.1",
               base.replace("download/v2.0.1", "latest/download?v=2.0.1"),
               base.replace("download/v2.0.1", "latest/download#v=2.0.1"),
               base.replace("v2.0.1", "v2.1.0"), base + "?v=2.0.1", base + "#v=2.0.1",
               base + "?", base + "#", base.replace("github.com", "github.com:443"),
               "https://[invalid]/{version}", base + "\n")
        release_update._verification_location(base, "2.0.1")
        for url in bad:
            with self.subTest(url=url), self.assertRaises(release_update.TransportError):
                release_update._verification_location(url, "2.0.1")

    def test_invalid_origin_is_refused_before_transport_construction_or_disclosure(self):
        for origin in ("https://attacker.invalid", "https://[invalid]"):
            with self.subTest(origin=origin), \
                    mock.patch.object(release_update, "_VERIFICATION_ORIGIN", origin), \
                    mock.patch.object(release_update, "HttpsTransport") as make:
                disclosure = mock.Mock()
                with self.assertRaises(release_update.TransportError):
                    release_update.verification_transport("2.0.1", environ={}, disclose=disclosure)
                make.assert_not_called()
                disclosure.assert_not_called()

    def test_primary_redirects_stay_on_exact_file_and_cdn_urls_are_permitted(self):
        expected = self.canonical_base() + "/MANIFEST.json"
        redirects = release_update._VerificationRedirects(expected)
        request = urllib.request.Request(expected)
        good = (expected, "https://release-assets.githubusercontent.com/fixture?signature=fixture",
                "https://objects.githubusercontent.com/fixture?signature=fixture")
        for url in good:
            with self.subTest(url=url):
                result = redirects.redirect_request(request, None, 302, "Found", {}, url)
                self.assertEqual(result.full_url, url)
        bad = (expected.replace("download/v2.0.1", "latest/download"),
               expected.replace("v2.0.1", "v2.1.0"), expected + "?v=2.0.1",
               "https://[invalid]/{version}", "https://attacker.invalid/MANIFEST.json",
               "http://release-assets.githubusercontent.com/fixture",
               "https://fixture@release-assets.githubusercontent.com/fixture",
               "https://release-assets.githubusercontent.com/fixture#fragment")
        for url in bad:
            with self.subTest(url=url), self.assertRaises(release_update.TransportError):
                redirects.redirect_request(request, None, 302, "Found", {}, url)

    def test_real_transport_strict_opener_checks_final_location_and_closes_response(self):
        expected = self.canonical_base() + "/MANIFEST.json"
        locations = (expected, "https://release-assets.githubusercontent.com/fixture?signature=fixture",
                     expected.replace("download/v2.0.1", "latest/download"), "https://[invalid]/{version}")
        for index, location in enumerate(locations):
            with self.subTest(location=location):
                events, seen = [], []
                transport = release_update.verification_transport("2.0.1", environ={}, disclose=events.append)
                response = io.BytesIO(b"fixture metadata")
                response.geturl = lambda: location

                def open_request(request, **kwargs):
                    self.assertTrue(events, "disclosure must precede requests")
                    seen.append(request.full_url)
                    return response

                opener = SimpleNamespace(open=open_request)
                with mock.patch.object(release_update.urllib.request, "build_opener", return_value=opener) as build, \
                        mock.patch.object(release_update.tls, "context", return_value=None):
                    if index < 2:
                        self.assertEqual(transport.fetch("MANIFEST.json", "2.0.1", 1024), b"fixture metadata")
                    else:
                        with self.assertRaises(release_update.TransportError):
                            transport.fetch("MANIFEST.json", "2.0.1", 1024)
                self.assertEqual(seen, [expected])
                self.assertTrue(response.closed)
                self.assertTrue(any(isinstance(handler, release_update._VerificationRedirects)
                                    for handler in build.call_args.args))

    def test_strict_opener_refuses_unrequested_files_before_building_network_transport(self):
        transport = release_update.verification_transport("2.0.1", environ={}, disclose=lambda text: None)
        with mock.patch.object(release_update.urllib.request, "build_opener") as build:
            with self.assertRaises(release_update.TransportError):
                transport.fetch("unexpected.tar.gz", "2.0.1", 1024)
            build.assert_not_called()
