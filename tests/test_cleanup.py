import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeGmail, FakeJev, message

import better_email.cli as cli
from better_email.domain import Policy, RemoteError
from better_email.engine import Engine, learn
from better_email.gmail import Gmail
from better_email.store import Store


class CleanupGmail(FakeGmail):
    def __init__(self, messages):
        super().__init__(messages)
        self.cleanup_calls = []
        self.fail_cleanup = None
        self.before_get = None

    def get(self, mid):
        if self.before_get:
            callback, self.before_get = self.before_get, None
            callback(mid)
        return super().get(mid)

    def move(self, mid, kind):
        self.cleanup_calls.append((mid, kind))
        if self.fail_cleanup == "before":
            raise RemoteError("Gmail", "network")
        labels = self.messages[mid].labels - {"INBOX"}
        if kind == "trash":
            labels |= {"TRASH"}
        self.messages[mid] = replace(self.messages[mid], labels=frozenset(labels))
        self.changed.add(mid)
        if self.fail_cleanup == "after":
            raise RemoteError("Gmail", "network")

    def archive(self, mid):
        self.move(mid, "archive")

    def trash(self, mid):
        self.move(mid, "trash")


def pipeline(store, category="spam", review=False, count=1):
    gmail = CleanupGmail([message(str(i)) for i in range(count)])
    engine = Engine(store, gmail, FakeJev(category, review))
    learn(store, Policy(body_words=0), activate=True)
    engine.sync()
    engine.preview()
    engine.apply()
    return engine, gmail


@pytest.mark.parametrize(
    "category,review,kind",
    [
        ("spam", False, "trash"),
        ("ignore", False, "archive"),
        ("important", True, "archive"),
        ("important", False, None),
    ],
)
def test_cleanup_mapping_preserves_labels_read_state_and_feedback(store, category, review, kind):
    engine, gmail = pipeline(store, category, review)
    before = gmail.messages["0"].labels
    assert "INBOX" in before  # apply remains label-only by default
    result = engine.cleanup()
    if kind:
        assert result["trashed" if kind == "trash" else "archived"] == 1
        assert before - {"INBOX"} <= gmail.messages["0"].labels
        assert "INBOX" not in gmail.messages["0"].labels
        assert ("TRASH" in gmail.messages["0"].labels) == (kind == "trash")
        assert gmail.cleanup_calls == [("0", kind)]
    else:
        assert gmail.messages["0"].labels == before
        assert not gmail.cleanup_calls
    engine.sync()
    assert store.examples() == []


def test_cleanup_rechecks_important_correction_before_moving(store):
    engine, gmail = pipeline(store)
    gmail.before_get = lambda mid: gmail.edit(mid, {"INBOX", "UNREAD", "label_important"})
    engine.cleanup()
    assert not gmail.cleanup_calls
    assert store.message("0")["override"] == "important"


@pytest.mark.parametrize(
    "labels",
    [
        {"INBOX", "label_spam", "label_ignore"},
        {"INBOX"},
        {"DRAFT", "INBOX", "label_spam"},
        {"TRASH", "label_spam"},
    ],
)
def test_cleanup_skips_conflicts_removed_labels_and_non_inbox_messages(store, labels):
    engine, gmail = pipeline(store)
    gmail.edit("0", labels)
    engine.cleanup()
    assert not gmail.cleanup_calls


def test_cleanup_skips_stale_or_unapplied_predictions(store):
    engine, gmail = pipeline(store)
    learn(store, Policy(body_words=0, preferences="new"), activate=True)
    assert engine.cleanup()["needs_preview"] == 1
    engine.classifier.category = "important"
    engine.preview(reclassify=True)
    assert engine.cleanup()["needs_apply"] == 1
    assert not gmail.cleanup_calls


def test_manual_review_is_archived_and_is_not_new_training_feedback(store):
    engine, gmail = pipeline(store)
    gmail.edit("0", {"INBOX", "UNREAD", "label_review"})
    assert engine.cleanup()["archived"] == 1
    assert store.examples() == []
    assert store.message("0")["state"] == "review_requested"


@pytest.mark.parametrize("category", ["spam", "ignore"])
def test_cleanup_does_not_repeat_after_manual_restore(store, category):
    engine, gmail = pipeline(store, category)
    engine.cleanup()
    restored = (gmail.messages["0"].labels - {"TRASH"}) | {"INBOX"}
    gmail.edit("0", restored)
    engine.sync()
    engine.cleanup()
    assert len(gmail.cleanup_calls) == 1
    assert "INBOX" in gmail.messages["0"].labels


@pytest.mark.parametrize("failure,status", [("before", "uncertain"), ("after", "applied")])
def test_cleanup_recovers_lost_responses_without_repeating_write(store, failure, status):
    engine, gmail = pipeline(store)
    gmail.fail_cleanup = failure
    assert engine.cleanup()["errors"] == 1
    assert store.one("SELECT status FROM cleanup_actions")["status"] == "pending"
    gmail.changed.clear()  # Recovery must not rely on a Gmail history event.
    engine.sync()
    assert store.one("SELECT status FROM cleanup_actions")["status"] == status
    gmail.fail_cleanup = None
    engine.cleanup()
    assert len(gmail.cleanup_calls) == 1
    assert not store.examples()


def test_cleanup_caps_attempts_including_failures(store):
    engine, gmail = pipeline(store, count=4)
    gmail.fail_cleanup = "before"
    assert engine.cleanup(limit=2)["errors"] == 2
    assert len(gmail.cleanup_calls) == 2
    assert len(store.rows("SELECT * FROM cleanup_actions")) == 2


def test_cleanup_is_message_level_with_shared_thread(store):
    engine, gmail = pipeline(store, count=2)
    gmail.messages["1"] = replace(gmail.messages["1"], thread_id="0")
    gmail.edit("1", {"INBOX", "label_important"})
    engine.cleanup()
    assert gmail.cleanup_calls == [("0", "trash")]
    assert "INBOX" in gmail.messages["1"].labels


@pytest.mark.parametrize("operation", ["archive", "trash"])
def test_gmail_cleanup_uses_scoped_message_endpoints_without_retries(operation):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        if operation == "archive":
            assert request.url.path.endswith("/messages/m1/modify")
            assert json.loads(request.content) == {"removeLabelIds": ["INBOX"]}
        else:
            assert request.url.path.endswith("/messages/m1/trash")
            assert not request.content
        return httpx.Response(503, text="PRIVATE_ERROR")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        gmail = Gmail(SimpleNamespace(valid=True, token="fake"), lambda v: None, http)
        with pytest.raises(RemoteError):
            getattr(gmail, operation)("m1")
    assert len(calls) == 1


@pytest.mark.parametrize("enabled,fail_apply", [(False, False), (True, False), (True, True)])
def test_cli_cleanup_opt_in_and_stops_on_label_failure(
    tmp_path, monkeypatch, capsys, enabled, fail_apply
):
    store = Store(tmp_path / "state.sqlite3")
    engine, gmail = pipeline(store)
    if fail_apply:
        gmail.edit("0", {"INBOX", "label_ignore"})
        # Store a human override whose label still needs to be written.
        engine.confirm("0", "spam")
        gmail.fail_modify = "before"
    store.close()
    gmail.close = lambda: None
    monkeypatch.setattr(cli, "Secrets", lambda p: SimpleNamespace(get=lambda n: "fake"))
    monkeypatch.setattr(cli, "credentials_from_json", lambda v: object())
    monkeypatch.setattr(cli, "Gmail", lambda c, s: gmail)
    args = ["--data-dir", str(tmp_path), "apply", "--limit", "1"]
    if enabled:
        args.append("--cleanup")
    assert cli.main(args) == int(fail_apply)
    result = json.loads(capsys.readouterr().out)
    if enabled and not fail_apply:
        assert result["cleanup"]["trashed"] == 1
    else:
        assert not gmail.cleanup_calls
    if fail_apply:
        assert "skipped" in result["cleanup"]


@pytest.mark.parametrize(
    "args,limit,cleanup",
    [
        ([], "100", True),
        (["250"], "250", True),
        (["--cleanup"], "100", True),
        (["25", "--cleanup"], "25", True),
        (["--cleanup", "25"], "25", True),
        (["--no-cleanup"], "100", False),
        (["25", "--no-cleanup"], "25", False),
    ],
)
def test_process_script_cleanup_options(tmp_path, args, limit, cleanup):
    fake = tmp_path / "uv"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n')
    fake.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / "process.sh"
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        ["bash", str(script), *args], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 4
    assert all(f"--limit {limit}" in line for line in lines)
    assert lines[-1] == f"run better-email apply --limit {limit}" + (
        " --cleanup" if cleanup else ""
    )


@pytest.mark.parametrize(
    "args",
    [
        ["0"],
        ["10001"],
        ["abc"],
        [""],
        ["2", "3"],
        ["--cleanup", "--no-cleanup"],
        ["--cleanup", "--cleanup"],
    ],
)
def test_process_rejects_invalid_arguments_before_running_uv(tmp_path, args):
    fake = tmp_path / "uv"
    fake.write_text('#!/bin/sh\necho "UNEXPECTED_CALL"\n')
    fake.chmod(0o700)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    script = Path(__file__).resolve().parents[1] / "process.sh"
    result = subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "UNEXPECTED_CALL" not in result.stdout


def test_additive_cleanup_schema_upgrade_preserves_state(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    engine, _ = pipeline(store)
    saved = store.latest("0")
    store.execute("DROP TABLE cleanup_actions")  # Earlier schema v2 has no cleanup journal.
    store.close()
    upgraded = Store(path)
    assert upgraded.latest("0") == saved
    assert upgraded.rows("SELECT * FROM cleanup_actions") == []
    upgraded.close()


def test_uncertain_label_write_skips_cleanup_and_returns_failure(tmp_path, monkeypatch, capsys):
    gmail = CleanupGmail([])
    gmail.close = lambda: None
    monkeypatch.setattr(cli, "Secrets", lambda p: SimpleNamespace(get=lambda n: "fake"))
    monkeypatch.setattr(cli, "credentials_from_json", lambda v: object())
    monkeypatch.setattr(cli, "Gmail", lambda c, s: gmail)
    monkeypatch.setattr(Engine, "apply", lambda self, limit: {"uncertain": 1})
    assert cli.main(["--data-dir", str(tmp_path), "apply", "--cleanup"]) == 1
    assert "skipped" in json.loads(capsys.readouterr().out)["cleanup"]
    assert not gmail.cleanup_calls
