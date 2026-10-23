"""TEST-signed production bundle wrapper and real, isolated gateway quota.

The runtime is the release-build fixture's stand-in for this interpreter;
this is not native-runtime qualification. The gateway is a selected build,
verified against its build record and the packaged contract before assembly.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from charmer import endpoint, probe, state
from _fake_release import make_key, needs_ssh_keygen, signers_line, ssh_env
from _gateway import BUILD_RECORD_ENV, _gateway_binary, host_target
from _layout import GATEWAY_CONTRACT, REPO_ROOT
from _release import load_tool, requires_release_tree
from _tier import fast_tier_boundary
import test_release_build as builds

PLATFORMS = ("linux",)


@requires_release_tree
@needs_ssh_keygen()
class SignedBundleQuotaTests(unittest.TestCase):
    def test_real_generated_wrapper_reads_quota_and_refuses_mismatched_attestation(self):
        boundary = fast_tier_boundary() or probe.network_isolation_available()
        if boundary:
            self.skipTest("BOUNDARY: " + boundary)
        binary, reason = _gateway_binary()
        if binary is None:
            self.skipTest("BOUNDARY: " + reason)
        binary = Path(binary)
        target = host_target()
        if str(binary).startswith("/nix/store/"):
            metadata = binary.parent.parent / "share/cli-proxy-api"
            record_path = metadata / "BUILD.json"
        else:
            record_path = Path(os.environ[BUILD_RECORD_ENV])
            metadata = record_path.parent
        record = json.loads(record_path.read_bytes())
        self.assertEqual((metadata / "gateway-contract.json").read_bytes(), GATEWAY_CONTRACT.read_bytes())
        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), record["targets"][target]["sha256"])
        with tempfile.TemporaryDirectory(prefix="charmer-bundle-quota-") as temporary:
            root = Path(temporary)
            fixture = builds.Fixture(root)
            # Replace the synthetic input BEFORE assembly, never a bundled
            # executable afterwards. The real build record remains intact.
            shutil.copyfile(binary, fixture.dist / target / "cli-proxy-api")
            shutil.copyfile(record_path, fixture.dist / "BUILD.json")
            tree = fixture.keyless_tree()
            key = make_key(root, "quota-test")
            signers = root / "allowed_signers"
            signers.write_text(signers_line(key))
            (tree / "src/charmer/data/release-trust/allowed_signers").write_text(signers.read_text())
            tool = builds.build_tool()
            host = load_tool(REPO_ROOT / "tools/_build/python_runtime.py", "quota_runtime").host_target()
            out = root / "release"
            args = tool.parse_args(fixture.argv("bundle", out, root / "work", "--target", host, "--repo", str(tree)))
            # The single-target assembler is also used for a release. Mark
            # this fixture as TEST in the checksummed manifest, never publishable.
            args.test_build = True
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                tool.run_release(tool.ReleaseContext(args))
            manifest = json.loads((out / "MANIFEST.json").read_bytes())
            self.assertIs(manifest["test_build"], True)
            sums = out / "SHA256SUMS"
            subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "charmer-release", str(sums)],
                           check=True, capture_output=True, env=ssh_env(root), timeout=30)
            verified = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", "release@charmer",
                                       "-n", "charmer-release", "-s", str(sums) + ".sig"],
                                      input=sums.read_bytes(), capture_output=True, env=ssh_env(root), timeout=30)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            for line in sums.read_text().splitlines():
                digest, name = line.split("  ")
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)
            asset = next(iter(manifest["assets"]))
            unpacked = root / "unpacked"
            unpacked.mkdir()
            with tarfile.open(out / asset, "r:gz") as archive:
                archive.extractall(unpacked, filter="data")
            bundle = unpacked / asset.removesuffix(".tar.gz")
            contract_path = bundle / builds.SITE / "charmer/data/gateway-contract.json"
            wrapper = bundle / "bin/charmer"
            wrapper_digest = hashlib.sha256(wrapper.read_bytes()).hexdigest()
            self.assertEqual(hashlib.sha256((bundle / "libexec/charmer/cli-proxy-api").read_bytes()).hexdigest(),
                             record["targets"][target]["sha256"])
            home = state.ensure_private_dir(root / "home")
            port = 18329  # only inside the new network namespace, never a host listener
            endpoint.write_config(home, endpoint.EndpointConfig(port=port))
            auth = state.ensure_private_dir(home / ".local/share/charmer/auth")
            state.atomic_write(auth / "claude-name-SENTINEL.json", json.dumps({
                "type": "claude", "access_token": "sk-ant-api03-SENTINEL",
                "refresh_token": "dummy-refresh-SENTINEL", "email": "pii-email@example.invalid",
                "expired": "2099-01-01T00:00:00Z", "last_refresh": "2098-01-01T00:00:00Z",
            }).encode())
            process = probe.start_isolated_process(
                [str(Path(sys.executable).resolve()), "-I", str(REPO_ROOT / "tests/bundle_quota_driver.py"),
                 str(bundle), str(contract_path), str(port)], cwd=root,
                env={"HOME": str(home), "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
                     "CHARMER_CHANNEL": "source", "CHARMER_PROXY_BIN": "/nonexistent/inherited-gateway",
                     "CHARMER_PROXY_PATCHES": "untrusted", "MANAGEMENT_PASSWORD": "INHERITED-VALUE"},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                stdout, stderr = process.communicate(timeout=90)
                self.assertEqual(process.returncode, 0, (stdout + stderr).decode())
                self.assertIn(b"bundle quota: PASS", stdout)
            finally:
                probe.stop_isolated_process(process)
            self.assertEqual(hashlib.sha256(wrapper.read_bytes()).hexdigest(), wrapper_digest)


if __name__ == "__main__":
    unittest.main()
