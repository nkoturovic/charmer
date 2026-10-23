"""Disposable signed-bundle quota journey, run only inside the test network namespace.

Uses the installed entry points, their generated environment and the real
bundled gateway. All state and credentials belong to this synthetic HOME.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    bundle, contract_path, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    home = Path(os.environ["HOME"])
    config = home / ".config/charmer"
    launcher = bundle / "bin/charmer"
    proxy = bundle / "bin/charmer-proxy"

    def run(program, *args):
        result = subprocess.run([str(program), *args], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=20)
        return result.returncode, result.stdout, result.stderr

    def quota_state(expected, exit_code):
        code, out, err = run(launcher, "quota", "--json")
        assert code == exit_code, (code, expected, out, err)
        document = json.loads(out)
        facts = {row["code"]: row for row in document["facts"]}
        assert facts["management-state"]["value"] == expected, document
        return document

    def key_snapshot():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in config.glob("management-key*")}

    def request(path, headers):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(1024 * 1024 + 1) if response.status == 200 else b""
        finally:
            connection.close()

    def start(log):
        process = subprocess.Popen([str(proxy), "run", "--prepare-and-exec"],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None, "disposable gateway exited before readiness"
            try:
                token = (config / "api-key").read_text().strip()
                code, body = request("/v1/models", {"Authorization": "Bearer " + token})
                if code == 200 and json.loads(body).get("data"):
                    return process
            except (OSError, http.client.HTTPException, ValueError):
                pass
            time.sleep(0.05)
        process.terminate()
        process.wait(timeout=15)
        raise AssertionError("disposable gateway did not register its synthetic account")

    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=15)

    code, _out, err = run(proxy, "init")
    assert code == 0, err
    quota_state("pending", 1)
    assert not (config / "management-key").exists()
    log_path = home / "gateway-test.log"
    with log_path.open("wb") as log:
        process = start(log)
        try:
            positive = quota_state("ok", 0)
            # The real entry point used by /cm, with no session writes.
            code, text, err = run(launcher, "lineup", "--session",
                                  "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f", "quota")
            assert code == 0 and "claude#1" in text, (code, text, err)
            assert "claude#1" in json.dumps(positive), positive
            key = (config / "management-key").read_text().strip()
            assert "INHERITED-VALUE" != key
            for secret in (key, "pii-email@example.invalid", "dummy-refresh-SENTINEL", "sk-ant-api03-SENTINEL"):
                assert secret not in json.dumps(positive) + text + err
            before = key_snapshot()
            # Keep the production-generated wrapper unchanged, but give its
            # packaged contract a different hash. Same basenames are not proof.
            contract = json.loads(contract_path.read_bytes())
            identity = contract["patches"][0]
            name, digest = identity.split("@")
            contract["patches"][0] = name + "@" + ("0" if digest[0] != "0" else "1") + digest[1:]
            contract_path.write_text(json.dumps(contract))
            requests_before = log_path.read_bytes().count(b"/v0/management/auth-files")
            assert requests_before == 2, requests_before
            quota_state("unavailable", 1)
            code, _out, _err = run(proxy, "init")
            assert code == 0
            assert key_snapshot() == before
            assert log_path.read_bytes().count(b"/v0/management/auth-files") == requests_before
        finally:
            stop(process)
    # The same mismatch also prevents selection and injection at a real
    # stopped start; the existing selected key cannot enable management.
    with log_path.open("ab") as log:
        process = start(log)
        try:
            assert key_snapshot() == before
            assert request("/v0/management/auth-files", {"X-Management-Key": key}) == (404, b"")
            quota_state("unavailable", 1)
        finally:
            stop(process)
    print("bundle quota: PASS prepared=ok unprepared=pending mismatched=unavailable management=off")


if __name__ == "__main__":
    main()
