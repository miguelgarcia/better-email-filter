import json
import os
from pathlib import Path

import pytest

from better_email.cli import main, read_policy, write_private
from better_email.domain import AppError
from better_email.local import locked_directory


def test_local_cli_works_without_credentials(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path), "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["account_connected"]
    assert report["messages"] == 0
    assert main(["--data-dir", str(tmp_path), "learn"]) == 0
    learned = json.loads(capsys.readouterr().out)
    assert learned["active"]
    assert main(["--data-dir", str(tmp_path), "versions"]) == 0
    versions = json.loads(capsys.readouterr().out)
    assert versions["active"] == learned["version"]


def test_private_storage_permissions_and_lock(tmp_path):
    with locked_directory(tmp_path):
        with pytest.raises(AppError, match="Another"):
            with locked_directory(tmp_path):
                pass
    assert os.stat(tmp_path).st_mode & 0o777 == 0o700
    assert main(["--data-dir", str(tmp_path), "status"]) == 0
    assert os.stat(tmp_path / "state.sqlite3").st_mode & 0o777 == 0o600


def test_export_does_not_clobber_existing_file(tmp_path):
    path = tmp_path / "export.json"
    write_private(path, {"hello": "world"})
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private(path, {"changed": True})
    assert json.loads(path.read_text()) == {"hello": "world"}


def test_example_config_and_bad_config(tmp_path):
    policy = read_policy(Path("config.example.toml"))
    assert policy.max_examples == 6
    bad = tmp_path / "bad.toml"
    bad.write_text("min_probability = 1.5")
    with pytest.raises(AppError):
        read_policy(bad)
