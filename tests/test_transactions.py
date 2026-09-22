import sqlite3

import pytest
from conftest import FakeGmail, message

from better_email.engine import Engine


def abort_observation(store):
    store.db.executescript("""
        CREATE TRIGGER fail_observation BEFORE UPDATE OF baseline ON messages
        BEGIN SELECT RAISE(ABORT, 'simulate interrupted transaction'); END;
    """)


def test_action_acknowledgement_and_baseline_are_atomic(store):
    gmail = FakeGmail([message()])
    engine = Engine(store, gmail)
    engine.sync()
    engine.labels = gmail.ensure_labels()
    aid = store.action("m1", set(), {"label_spam"})
    gmail.edit("m1", {"INBOX", "label_spam"})
    abort_observation(store)
    with pytest.raises(sqlite3.IntegrityError):
        engine.reconcile(gmail.get("m1"))
    assert store.one("SELECT status FROM actions WHERE id=?", (aid,))["status"] == "pending"
    assert store.message("m1")["baseline"] == "[]"
    store.db.execute("DROP TRIGGER fail_observation")
    engine.reconcile(gmail.get("m1"))
    assert store.examples() == []
    assert store.one("SELECT status FROM actions WHERE id=?", (aid,))["status"] == "applied"


def test_feedback_and_baseline_are_atomic(store):
    gmail = FakeGmail([message()])
    engine = Engine(store, gmail)
    engine.sync()
    engine.labels = gmail.ensure_labels()
    gmail.edit("m1", {"INBOX", "label_important"})
    abort_observation(store)
    with pytest.raises(sqlite3.IntegrityError):
        engine.reconcile(gmail.get("m1"))
    assert not store.rows("SELECT * FROM feedback")
    store.db.execute("DROP TRIGGER fail_observation")
    engine.reconcile(gmail.get("m1"))
    assert store.examples()[0]["category"] == "important"


def test_apply_limit_caps_attempts_even_when_remote_writes_fail(store):
    from conftest import FakeJev

    from better_email.domain import Policy
    from better_email.engine import learn

    gmail = FakeGmail([message(str(i)) for i in range(4)])
    engine = Engine(store, gmail, FakeJev())
    learn(store, Policy(), activate=True)
    engine.sync()
    engine.preview()
    gmail.fail_modify = "before"
    assert engine.apply(limit=2)["errors"] == 2
    assert len(store.rows("SELECT * FROM actions")) == 2
