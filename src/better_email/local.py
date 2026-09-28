from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .domain import AppError, digest

PROVIDERS = {"gmail", "jev"}
MAX_SECRET_BYTES = 128 * 1024


def default_data_dir():
    override = os.environ.get("BETTER_EMAIL_DATA_DIR")
    if override:
        return Path(override).expanduser().absolute()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BetterEmail"
    base = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not base.is_absolute():
        base = Path.home() / ".local" / "state"
    return base / "better-email"


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AppError(
            "Private storage must be a directory owned by the current user, not a symlink."
        )
    path.chmod(0o700)


@contextmanager
def locked_directory(path: Path, lock_name="run.lock"):
    os.umask(0o077)
    private_directory(path)
    with (path / lock_name).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError("Another Better Email command is using this data directory.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_secret_json(path):
    """Never follow a credential-file symlink or echo malformed secret content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise AppError("Credential files must be regular, owner-only files (chmod 600).")
            raw = handle.read(MAX_SECRET_BYTES + 1)
        if len(raw) > MAX_SECRET_BYTES:
            raise ValueError("Too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return value
    except FileNotFoundError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise AppError(
            "Could not read credential file; check its format and permissions."
        ) from None


@contextmanager
def private_output(path):
    """Publish a completed private file without overwriting an existing destination."""
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".better-email-", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        yield temporary
        os.link(temporary, path)  # Atomic and fails if any destination already exists.
    finally:
        temporary.unlink(missing_ok=True)


def export_secret_json(path, value):
    with private_output(path) as temporary:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())


def validate_secrets(values):
    if (
        not isinstance(values, dict)
        or set(values) - PROVIDERS
        or any(not isinstance(v, str) or not v.strip() for v in values.values())
    ):
        raise AppError("Invalid credential data.")
    return values


class Secrets:
    def __init__(self, data_dir: Path, backend=None):
        self.kind = backend or os.environ.get("BETTER_EMAIL_SECRETS_BACKEND", "keychain")
        self.path = data_dir / "credentials.json"
        if self.kind == "file":
            private_directory(data_dir)
            self.backend = None
        elif self.kind == "keychain":
            if sys.platform != "darwin":
                raise AppError(
                    "macOS Keychain is unavailable. On Linux explicitly select "
                    "--secrets-backend file or BETTER_EMAIL_SECRETS_BACKEND=file."
                )
            from keyring.backends.macOS import Keyring

            self.backend = Keyring()
        else:
            raise AppError("Unknown credentials backend; choose keychain or file.")
        self.service = "better-email:" + digest(str(data_dir.resolve()))

    def _file_values(self):
        try:
            return validate_secrets(read_secret_json(self.path))
        except FileNotFoundError:
            return {}

    def get(self, name):
        if name not in PROVIDERS:
            raise AppError("Unknown credential provider.")
        if self.kind == "file":
            value = self._file_values().get(name)
        else:
            try:
                value = self.backend.get_password(self.service, name)
            except Exception:
                raise AppError("Could not read macOS Keychain.") from None
        if not value:
            raise AppError(f"No saved {name} credential. Run `auth {name}` or import credentials.")
        return value

    def set(self, name, value):
        self.set_many({name: value})

    def set_many(self, values):
        validate_secrets(values)
        if self.kind == "file":
            existing = self._file_values()  # Reject unsafe existing files before replacing them.
            existing.update(values)
            fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(existing, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        else:
            try:
                for name, value in values.items():
                    self.backend.set_password(self.service, name, value)
            except Exception:
                raise AppError("Could not save the credential in macOS Keychain.") from None
