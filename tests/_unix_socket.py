"""Bind fixture sockets without depending on the runner's temporary-directory depth."""

import os
import socket
import sys
import threading
import unittest
from pathlib import Path


def bind_unix_socket(sock: socket.socket, path: Path) -> None:
    """Bind at ``path`` using a short address when its encoded pathname is too long."""

    limit = 107 if sys.platform == "linux" else 103  # sun_path bytes, excluding the terminating NUL.
    if len(os.fsencode(path)) <= limit:
        sock.bind(str(path))
    elif sys.platform == "linux":
        # The directory fd keeps the actual fixture location without changing process cwd.
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            sock.bind(f"/proc/self/fd/{parent}/{path.name}")
        finally:
            os.close(parent)
    else:
        if threading.active_count() != 1:
            raise unittest.SkipTest("long AF_UNIX fixture path needs chdir; unsafe with active fixture threads")
        cwd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.chdir(path.parent)
            sock.bind(path.name)
        finally:
            try:
                os.fchdir(cwd)
            finally:
                os.close(cwd)
