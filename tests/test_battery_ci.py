"""The battery provisions an interpreter that the isolation boundary admits."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest import mock


PLATFORMS = ("linux",)
REPO = Path(__file__).resolve().parents[1]
_READ_ONLY_PROGRAM = "import os, sys; os.chmod(os.path.realpath(sys.executable), 0o555)"


def _run_owned_python(interpreter: Path, program: str) -> subprocess.CompletedProcess[str]:
    root = interpreter.parent.resolve()
    env = {"HOME": str(root), "PYTHONHOME": sys.base_prefix,
           "LD_LIBRARY_PATH": str(Path(sys.base_prefix) / "lib")}
    if log := os.environ.get("CM_ISOLATION_GUARD_LOG"):
        env["CM_ISOLATION_GUARD_LOG"] = log
    # -S suppresses site/.pth startup; -P excludes cwd from imports. Unlike
    # -I, these retain the trusted PYTHONHOME needed by the relocated copy.
    # Arm the repository tripwire explicitly, never through ambient site code.
    bootstrap = (f"import runpy; runpy.run_path({str(REPO / 'tests' / '_tripwire.py')!r})['install']()\n"
                 + program)
    return subprocess.run([str(interpreter), "-S", "-P", "-B", "-c", bootstrap], env=env, cwd=root,
                          capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)


def _run_read_only_step(script: str, interpreter: Path) -> subprocess.CompletedProcess[str]:
    """Validate the whole step, then run only the known chmod on our copy."""

    commands = [line.strip() for line in script.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    if len(commands) != 2 or commands[0] != "umask 077":
        raise ValueError("the read-only step must contain only umask 077 and python3 -c")
    command = re.fullmatch(r"python3 -c '([^']+)'", commands[1])
    if command is None:
        raise ValueError("the read-only step must contain only umask 077 and python3 -c")
    try:
        actual = ast.dump(ast.parse(command[1]), include_attributes=False)
    except SyntaxError as exc:
        raise ValueError("the read-only step has an unexpected Python program") from exc
    if actual != ast.dump(ast.parse(_READ_ONLY_PROGRAM), include_attributes=False):
        raise ValueError("the read-only step has an unexpected Python program")
    # No workflow shell runs here, and the argv uses the known program, not
    # the extracted text. The umask above is a contract check only.
    return _run_owned_python(interpreter, _READ_ONLY_PROGRAM)


class BatteryInterpreterTests(unittest.TestCase):
    def test_toolcache_interpreter_is_read_only_before_the_suite(self) -> None:
        workflow = (REPO / ".github/workflows/ci.yml").read_text()
        battery = workflow.split("\n  battery:\n", 1)[1].split("\n  pty:\n", 1)[0]
        step = re.search(
            r"^      - name: Read-only interpreter for isolated probes\n"
            r"        run: \|\n(.*?)(?=^      - |\Z)", battery, re.M | re.S,
        )
        self.assertIsNotNone(step, "the battery must provision its writable setup-python interpreter")
        assert step is not None
        self.assertLess(step.start(), battery.index("      - name: Full suite"))
        script = step[1]
        with tempfile.TemporaryDirectory(prefix="cm-battery-python-") as tmp:
            root = Path(tmp)
            interpreter = root / "interpreter"
            shutil.copyfile(Path(sys.executable).resolve(), interpreter)
            interpreter.chmod(0o775)
            (root / "python3").symlink_to(interpreter)
            done = _run_read_only_step(script, root / "python3")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(stat.S_IMODE(interpreter.stat().st_mode), 0o555)
            done = _run_owned_python(root / "python3", "print('ready')")
            self.assertEqual((done.returncode, done.stdout), (0, "ready\n"), done.stderr)

    def test_owned_children_ignore_hostile_startup_hooks(self) -> None:
        script = f"umask 077\npython3 -c '{_READ_ONLY_PROGRAM}'\n"
        with tempfile.TemporaryDirectory(prefix="cm-battery-startup-") as tmp:
            root = Path(tmp)
            owned = root / "owned"
            owned.mkdir(mode=0o700)
            interpreter = owned / "python3"
            shutil.copyfile(Path(sys.executable).resolve(), interpreter)
            sentinel = root / "outside-sentinel"
            sentinel.write_bytes(b"outside\n")
            sentinel.chmod(0o640)
            marker = root / "startup-ran"
            userbase = root / "userbase"
            user_site = Path(sysconfig.get_path("purelib", scheme="posix_user", vars={"userbase": str(userbase)}))
            user_site.mkdir(parents=True)
            ambient = root / "ambient"
            ambient.mkdir()
            # This reproduces the false pass: a startup hook chmods the copy
            # itself, then redirects the approved chmod to a file outside it.
            hook = (f"import os, sys; open({str(marker)!r}, 'a').close(); "
                    "os.chmod(os.path.realpath(sys.executable), 0o555); "
                    f"sys.executable = {str(sentinel)!r}\n")
            for path in (user_site / "hostile.pth", user_site / "usercustomize.py",
                         ambient / "sitecustomize.py", ambient / "socket.py",
                         owned / "sitecustomize.py", owned / "socket.py"):
                path.write_text(hook, encoding="utf-8")
            hostile_env = {"PYTHONUSERBASE": str(userbase), "PYTHONPATH": str(ambient),
                           "PYTHONHOME": str(ambient), "PYTHONSTARTUP": str(ambient / "sitecustomize.py"),
                           "LD_LIBRARY_PATH": str(ambient / "untrusted-lib"),
                           "LD_PRELOAD": str(ambient / "untrusted-preload.so"), "HOME": str(ambient)}
            guard_log = os.environ.get("CM_ISOLATION_GUARD_LOG") or None
            smoke = (
                "import os, socket, sys; assert sys.flags.no_site and sys.flags.safe_path; "
                "assert not {'PYTHONPATH', 'PYTHONUSERBASE', 'PYTHONSTARTUP'} & os.environ.keys(); "
                "assert os.getcwd() == os.environ['HOME']; "
                f"assert os.environ.get('LD_LIBRARY_PATH') == {str(Path(sys.base_prefix) / 'lib')!r}; "
                "assert 'LD_PRELOAD' not in os.environ; "
                f"assert os.environ.get('CM_ISOLATION_GUARD_LOG') == {guard_log!r}; "
                "assert getattr(socket.socket.connect, '_cm_test_tripwire', False); "
                "assert getattr(socket.socket.connect_ex, '_cm_test_tripwire', False); "
                "assert getattr(sys, '_cm_listener_tripwire', False); print('ready')"
            )
            for phase in ("chmod", "smoke"):
                with self.subTest(phase=phase):
                    interpreter.chmod(0o775)
                    with mock.patch.dict(os.environ, hostile_env):
                        done = (_run_read_only_step(script, interpreter) if phase == "chmod"
                                else _run_owned_python(interpreter, smoke))
                    self.assertEqual((done.returncode, done.stdout),
                                     (0, "" if phase == "chmod" else "ready\n"), done.stderr)
                    self.assertEqual(stat.S_IMODE(interpreter.stat().st_mode),
                                     0o555 if phase == "chmod" else 0o775)
                    self.assertFalse(marker.exists(), "an inherited startup/import hook executed")
                    self.assertEqual((sentinel.read_bytes(), stat.S_IMODE(sentinel.stat().st_mode)),
                                     (b"outside\n", 0o640))

    def test_read_only_step_rejects_extra_commands_before_starting_a_child(self) -> None:
        script = f"umask 077\npython3 -c '{_READ_ONLY_PROGRAM}'\n"
        for bad in (
                script + "sudo localedef -i en_US -f ISO-8859-1 en_US.ISO-8859-1\n",
                script + "\nprintf extra\n",
                script.rstrip() + "; printf extra\n",
                script.replace("python3 -c", "sudo python3 -c"),
                script.replace("0o555", "0o777"),
                script.replace("sys.executable", '"another-interpreter"'),
                script.replace("import os, sys", "import os, sys, subprocess"),
                script.replace(_READ_ONLY_PROGRAM, _READ_ONLY_PROGRAM + '; print("extra")'),
                script.replace(_READ_ONLY_PROGRAM, "not valid Python")):
            with self.subTest(script=bad), mock.patch.object(subprocess, "run") as child:
                with self.assertRaises(ValueError):
                    _run_read_only_step(bad, Path("unused-owned-python3"))
                child.assert_not_called()
