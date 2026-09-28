import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import better_email.cli as cli
import better_email.local as local
import better_email.workflow as workflow
from better_email.domain import AppError, Policy, digest
from better_email.engine import learn
from better_email.gmail import SCOPE
from better_email.local import Secrets, export_secret_json, locked_directory, read_secret_json
from better_email.store import Store


def gmail_credentials():
    return json.dumps(
        {
            "token": "synthetic-access",
            "refresh_token": "synthetic-refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "synthetic-client",
            "client_secret": "synthetic-secret",
            "scopes": [SCOPE],
        }
    )


@pytest.fixture
def file_backend(monkeypatch):
    monkeypatch.setenv("BETTER_EMAIL_SECRETS_BACKEND", "file")


def test_platform_paths_and_explicit_override(monkeypatch, tmp_path):
    monkeypatch.delenv("BETTER_EMAIL_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(local.sys, "platform", "darwin")
    assert local.default_data_dir() == tmp_path / "Library/Application Support/BetterEmail"
    monkeypatch.setattr(local.sys, "platform", "linux")
    assert local.default_data_dir() == tmp_path / ".local/state/better-email"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert local.default_data_dir() == tmp_path / "state/better-email"
    monkeypatch.setenv("XDG_STATE_HOME", "relative-is-invalid")
    assert local.default_data_dir() == tmp_path / ".local/state/better-email"
    monkeypatch.setenv("BETTER_EMAIL_DATA_DIR", str(tmp_path / "explicit"))
    assert local.default_data_dir() == tmp_path / "explicit"


def test_linux_requires_explicit_file_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("BETTER_EMAIL_SECRETS_BACKEND", raising=False)
    monkeypatch.setattr(local.sys, "platform", "linux")
    with pytest.raises(AppError, match="explicitly select"):
        Secrets(tmp_path)
    assert not (tmp_path / "credentials.json").exists()
    vault = Secrets(tmp_path, backend="file")
    vault.set("jev", "synthetic-key")
    assert vault.get("jev") == "synthetic-key"


def test_mac_keychain_keeps_existing_namespace(monkeypatch, tmp_path):
    monkeypatch.delenv("BETTER_EMAIL_SECRETS_BACKEND", raising=False)
    monkeypatch.setattr(local.sys, "platform", "darwin")
    calls = []
    backend = SimpleNamespace(
        get_password=lambda service, name: "existing-synthetic-key",
        set_password=lambda *args: calls.append(args),
    )
    monkeypatch.setitem(
        sys.modules, "keyring.backends.macOS", SimpleNamespace(Keyring=lambda: backend)
    )
    vault = Secrets(tmp_path)
    assert vault.kind == "keychain"
    assert vault.get("jev") == "existing-synthetic-key"
    vault.set("jev", "updated-synthetic-key")
    assert calls == [
        ("better-email:" + digest(str(tmp_path.resolve())), "jev", "updated-synthetic-key")
    ]
    assert not (tmp_path / "credentials.json").exists()


def test_explicit_backend_flag_overrides_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_EMAIL_SECRETS_BACKEND", "keychain")
    monkeypatch.setattr(local.sys, "platform", "linux")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-key")
    assert cli.main(["--data-dir", str(tmp_path), "--secrets-backend", "file", "auth", "jev"]) == 0
    assert Secrets(tmp_path, backend="file").get("jev") == "synthetic-key"


def test_file_backend_private_permissions_and_refresh_preserves_other_keys(tmp_path, file_backend):
    vault = Secrets(tmp_path)
    vault.set_many({"gmail": gmail_credentials(), "jev": "synthetic-key"})
    old_inode = vault.path.stat().st_ino
    vault.set("gmail", "refreshed synthetic value")
    assert vault.path.stat().st_ino != old_inode  # Replacement, never truncate-in-place.
    assert vault.get("gmail") == "refreshed synthetic value"
    assert vault.get("jev") == "synthetic-key"
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert vault.path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".credentials-*"))


def test_failed_atomic_refresh_preserves_existing_credentials(tmp_path, file_backend, monkeypatch):
    vault = Secrets(tmp_path)
    vault.set("gmail", "original")

    def fail_replace(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(local.os, "replace", fail_replace)
    with pytest.raises(OSError):
        vault.set("gmail", "new")
    assert vault.get("gmail") == "original"
    assert not list(tmp_path.glob(".credentials-*"))


def test_file_backend_rejects_symlinks_permissions_and_malformed_data(tmp_path, file_backend):
    vault = Secrets(tmp_path)
    target = tmp_path / "other.json"
    target.write_text('{"jev":"synthetic"}')
    target.chmod(0o600)
    vault.path.symlink_to(target)
    with pytest.raises(AppError):
        vault.get("jev")
    with pytest.raises(AppError):
        vault.set("jev", "replacement")
    vault.path.unlink()
    vault.path.write_text('{"jev":"synthetic"}')
    vault.path.chmod(0o644)
    with pytest.raises(AppError, match="chmod 600"):
        vault.get("jev")
    vault.path.chmod(0o600)
    vault.path.write_text("PRIVATE_MALFORMED_PAYLOAD")
    with pytest.raises(AppError) as error:
        vault.get("jev")
    assert "PRIVATE_MALFORMED" not in str(error.value)


def test_credential_directory_cannot_be_symlink(tmp_path):
    target = tmp_path / "actual"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(AppError, match="symlink"):
        Secrets(link, backend="file")


def test_credential_export_import_round_trip(tmp_path, file_backend, monkeypatch, capsys):
    source, destination = tmp_path / "source", tmp_path / "destination"
    vault = Secrets(source)
    vault.set_many({"gmail": gmail_credentials(), "jev": "synthetic-jev-private"})
    export = tmp_path / "transfer" / "credentials.json"
    assert cli.main(["--data-dir", str(source), "credentials", "export", str(export)]) == 0
    assert export.stat().st_mode & 0o777 == 0o600
    calls = []

    def client(credentials, save):
        calls.append(credentials)
        return SimpleNamespace(
            profile=lambda: {"emailAddress": "owner@example.com"}, close=lambda: None
        )

    monkeypatch.setattr(cli, "Gmail", client)
    assert cli.main(["--data-dir", str(destination), "credentials", "import", str(export)]) == 0
    assert len(calls) == 1
    imported = Secrets(destination)
    assert imported.get("jev") == "synthetic-jev-private"
    assert json.loads(imported.get("gmail"))["refresh_token"] == "synthetic-refresh"
    output = capsys.readouterr()
    assert "synthetic-jev-private" not in output.out + output.err
    assert "synthetic-refresh" not in output.out + output.err
    original = export.read_bytes()
    assert cli.main(["--data-dir", str(source), "credentials", "export", str(export)]) == 1
    assert export.read_bytes() == original


def test_import_wrong_account_does_not_replace_credentials(tmp_path, file_backend, monkeypatch):
    data = tmp_path / "state"
    vault = Secrets(data)
    vault.set("jev", "original-key")
    store = Store(data / "state.sqlite3")
    store.bind_account("first@example.com")
    store.close()
    bundle = tmp_path / "transfer" / "credentials.json"
    export_secret_json(
        bundle, {"format": 1, "credentials": {"gmail": gmail_credentials(), "jev": "new-key"}}
    )
    monkeypatch.setattr(
        cli,
        "Gmail",
        lambda credentials, save: SimpleNamespace(
            profile=lambda: {"emailAddress": "second@example.com"}, close=lambda: None
        ),
    )
    assert cli.main(["--data-dir", str(data), "credentials", "import", str(bundle)]) == 1
    assert vault.get("jev") == "original-key"
    with pytest.raises(AppError, match="No saved gmail"):
        vault.get("gmail")


@pytest.mark.parametrize(
    "bundle",
    [
        {"format": True, "credentials": {}},
        {"format": 2, "credentials": {}},
        {"format": 1, "credentials": {"gmail": "PRIVATE_INVALID"}},
        {"format": 1, "credentials": {"jev": "synthetic"}},
        {"format": 1, "credentials": {"gmail": gmail_credentials(), "unknown": "secret"}},
    ],
)
def test_invalid_import_never_calls_gmail(tmp_path, file_backend, monkeypatch, capsys, bundle):
    path = tmp_path / "transfer" / "credentials.json"
    export_secret_json(path, bundle)
    monkeypatch.setattr(cli, "Gmail", lambda *args: pytest.fail("Unexpected network call"))
    assert (
        cli.main(["--data-dir", str(tmp_path / "state"), "credentials", "import", str(path)]) == 1
    )
    assert "PRIVATE_INVALID" not in capsys.readouterr().err


def test_gmail_only_export_and_environment_jev_key(tmp_path, file_backend, monkeypatch):
    data = tmp_path / "state"
    Secrets(data).set("gmail", gmail_credentials())
    first = tmp_path / "transfer" / "gmail.json"
    assert (
        cli.main(["--data-dir", str(data), "credentials", "export", str(first), "--gmail-only"])
        == 0
    )
    assert set(read_secret_json(first)["credentials"]) == {"gmail"}
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-env-key")
    second = tmp_path / "transfer" / "both.json"
    assert cli.main(["--data-dir", str(data), "credentials", "export", str(second)]) == 0
    assert read_secret_json(second)["credentials"]["jev"] == "synthetic-env-key"


def test_backup_retains_learning_state_without_credentials(tmp_path, file_backend):
    data = tmp_path / "state"
    Secrets(data).set("jev", "PRIVATE_KEY_NOT_IN_BACKUP")
    store = Store(data / "state.sqlite3")
    store.bind_account("owner@example.com")
    version, _ = learn(store, Policy(preferences="synthetic preference"), activate=True)
    store.close()
    backup = tmp_path / "transfer" / "state.sqlite3"
    assert cli.main(["--data-dir", str(data), "backup", str(backup)]) == 0
    with sqlite3.connect(backup) as db:
        assert (
            db.execute("SELECT value FROM meta WHERE key='active_version'").fetchone()[0] == version
        )
    assert b"PRIVATE_KEY_NOT_IN_BACKUP" not in backup.read_bytes()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert cli.main(["--data-dir", str(data), "backup", str(backup)]) == 1


@pytest.mark.parametrize("no_cleanup", [False, True])
def test_workflow_holds_lock_for_all_steps_and_forwards_settings(tmp_path, monkeypatch, no_cleanup):
    data = tmp_path / "state"
    monkeypatch.setenv("BETTER_EMAIL_DATA_DIR", str(data))
    monkeypatch.chdir(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    (private / "profile.toml").write_text("body_words=0")
    calls = []

    def run(command):
        with pytest.raises(AppError):
            with locked_directory(data, "workflow.lock"):
                pass
        # The child command's separate lock remains available.
        with locked_directory(data):
            pass
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(workflow.subprocess, "run", run)
    args = ["--limit", "37"] + (["--no-cleanup"] if no_cleanup else [])
    assert workflow.main(args) == 0
    assert len(calls) == 4
    assert all(
        c[:5] == [sys.executable, "-m", "better_email.cli", "--data-dir", str(data)] for c in calls
    )
    assert calls[0][5:] == ["sync", "--limit", "37"]
    assert calls[1][5:] == [
        "--config",
        "private/profile.toml",
        "learn",
        "--activate",
        "--limit",
        "37",
    ]
    assert calls[2][5:] == ["preview", "--reclassify", "--limit", "37"]
    assert ("--cleanup" in calls[3]) == (not no_cleanup)


def test_workflow_stops_after_failure_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_EMAIL_DATA_DIR", str(tmp_path))
    calls = []

    def run(command):
        calls.append(command)
        return SimpleNamespace(returncode=7 if len(calls) == 2 else 0)

    monkeypatch.setattr(workflow.subprocess, "run", run)
    assert workflow.main([]) == 7
    assert len(calls) == 2
    with locked_directory(tmp_path, "workflow.lock"):
        pass


def test_second_workflow_process_is_rejected_before_commands(tmp_path):
    env = dict(
        os.environ,
        BETTER_EMAIL_DATA_DIR=str(tmp_path),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
    )
    with locked_directory(tmp_path, "workflow.lock"):
        result = subprocess.run(
            [sys.executable, "-m", "better_email.workflow"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 1
    assert "Another Better Email" in result.stderr
    assert not (tmp_path / "state.sqlite3").exists()
