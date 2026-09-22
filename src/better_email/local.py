from __future__ import annotations

import fcntl
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from .domain import AppError, digest


def default_data_dir():
    return Path.home() / "Library" / "Application Support" / "BetterEmail"


@contextmanager
def locked_directory(path: Path):
    os.umask(0o077)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    with (path / "run.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError("Another Better Email command is using this data directory.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Secrets:
    def __init__(self, data_dir: Path):
        if sys.platform != "darwin":
            raise AppError("This version stores credentials in macOS Keychain and requires macOS.")
        # Select macOS Keychain explicitly; never fall back to a plaintext backend.
        from keyring.backends.macOS import Keyring

        self.backend = Keyring()
        self.service = "better-email:" + digest(str(data_dir.resolve()))

    def get(self, name):
        try:
            value = self.backend.get_password(self.service, name)
        except Exception:
            raise AppError("Could not read macOS Keychain.") from None
        if not value:
            raise AppError(f"No saved {name} credential. Run `auth {name}` first.")
        return value

    def set(self, name, value):
        try:
            self.backend.set_password(self.service, name, value)
        except Exception:
            raise AppError("Could not save the credential in macOS Keychain.") from None
