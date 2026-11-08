"""Skip decorators for tests that exercise behaviour only one platform has. Each says why in its reason."""
import functools
import os
import socket
import tempfile
import unittest
from pathlib import Path

WINDOWS = os.name == "nt"

needs_af_unix = unittest.skipUnless(
    hasattr(socket, "AF_UNIX"),
    "needs socket.AF_UNIX; on Windows Claude workers use named pipes (tests/test_claude_worker_pipe.py)")


def posix_only(reason):
    """Skip on Windows; `reason` names the POSIX behaviour (permissions, FIFOs, signals, process groups...)."""
    return unittest.skipIf(WINDOWS, f"POSIX only: {reason}")


@functools.cache
def _can_symlink():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(tmp, Path(tmp) / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
        return True


def needs_symlinks(test):
    """Windows creates symlinks only with Developer Mode or SeCreateSymbolicLinkPrivilege (WinError 1314)."""
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        if not _can_symlink():
            raise unittest.SkipTest("this account cannot create symlinks (Windows needs Developer Mode or admin)")
        return test(*args, **kwargs)
    return wrapper
