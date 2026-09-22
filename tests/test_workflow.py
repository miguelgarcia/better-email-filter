import json

import pytest
from conftest import FakeGmail, FakeJev, message

from better_email.domain import AppError, Policy, RemoteError
from better_email.engine import Engine, learn


def pipeline(store, items=None, review=False):
    gmail = FakeGmail(items or [message()])
    jev = FakeJev(review=review)
    engine = Engine(store, gmail, jev)
    learn(store, Policy(), activate=True)
    engine.sync()
    return engine, gmail, jev


def test_preview_no_gmail_writes_or_duplicate_inference(store):
    engine, gmail, jev = pipeline(store)
    first = engine.preview()
    second = engine.preview()
    assert first["counts"]["spam"] == 1
    assert second["counts"]["cached"] == 1
    assert len(jev.calls) == 1
    assert gmail.mutations == []
    assert store.examples() == []


def test_apply_preserves_system_and_unrelated_labels(store):
    engine, gmail, jev = pipeline(store)
    engine.preview()
    assert engine.apply()["applied"] == 1
    assert gmail.messages["m1"].labels == {"INBOX", "UNREAD", "personal", "label_spam"}
    engine.sync()
    assert store.examples() == []
    assert engine.apply()["unchanged"] == 1
    assert len(jev.calls) == 1


def test_low_confidence_only_review_label(store):
    engine, gmail, _ = pipeline(store, review=True)
    preview = engine.preview()
    assert preview["predictions"][0]["category"] == "spam"
    assert preview["predictions"][0]["proposed_label"] == "BetterEmail/Review"
    engine.apply()
    assert "label_review" in gmail.messages["m1"].labels
    assert "label_spam" not in gmail.messages["m1"].labels


def test_confident_ignore_proposes_and_applies_ignore_label(store):
    engine, gmail, jev = pipeline(store)
    jev.category = "ignore"
    preview = engine.preview()
    assert preview["predictions"][0]["proposed_label"] == "BetterEmail/Ignore"
    engine.apply()
    assert "label_ignore" in gmail.messages["m1"].labels
    assert "label_review" not in gmail.messages["m1"].labels


def test_correction_preserved_and_captured_after_archiving(store):
    engine, gmail, jev = pipeline(store)
    engine.preview()
    engine.apply()
    gmail.edit("m1", {"UNREAD", "personal", "label_important"})
    engine.sync()
    engine.preview(reclassify=True)
    engine.apply()
    assert store.message("m1")["override"] == "important"
    assert store.examples()[0]["category"] == "important"
    assert len(jev.calls) == 1
    assert "INBOX" not in gmail.messages["m1"].labels
    engine.sync()
    assert len(store.rows("SELECT * FROM feedback")) == 1


@pytest.mark.parametrize(
    "labels,state",
    [
        ({"INBOX", "label_spam", "label_important"}, "conflict"),
        ({"INBOX"}, "removed"),
        ({"INBOX", "label_review"}, "review_requested"),
    ],
)
def test_ambiguous_label_edits_are_not_training_data(store, labels, state):
    engine, gmail, _ = pipeline(store)
    engine.preview()
    engine.apply()
    gmail.edit("m1", labels)
    engine.sync()
    engine.apply()
    assert store.message("m1")["state"] == state
    assert gmail.messages["m1"].labels == labels
    assert store.examples() == []


def test_remove_then_add_correction_across_runs(store):
    engine, gmail, _ = pipeline(store)
    engine.preview()
    engine.apply()
    gmail.edit("m1", {"INBOX"})
    engine.sync()
    gmail.edit("m1", {"INBOX", "label_ignore"})
    engine.sync()
    assert store.examples()[0]["category"] == "ignore"


@pytest.mark.parametrize("fail,expected", [("after", "normal"), ("before", "uncertain")])
def test_lost_write_response_reconciled_without_self_training(store, fail, expected):
    engine, gmail, _ = pipeline(store)
    engine.preview()
    gmail.fail_modify = fail
    assert engine.apply()["errors"] == 1
    gmail.fail_modify = None
    engine.sync()
    assert store.message("m1")["state"] == expected
    assert store.examples() == []
    assert not store.rows("SELECT * FROM actions WHERE status='pending'")
    if expected == "uncertain":
        engine.apply()
        assert "label_spam" not in gmail.messages["m1"].labels


def test_observed_edit_before_write_takes_precedence(store):
    engine, gmail, _ = pipeline(store)
    engine.preview()
    gmail.ensure_labels()
    gmail.edit("m1", {"INBOX", "label_important"})
    engine.apply()
    assert gmail.messages["m1"].labels == {"INBOX", "label_important"}
    assert store.message("m1")["override"] == "important"


def test_concurrent_edit_after_write_suspends_automation(store):
    engine, gmail, _ = pipeline(store)
    engine.preview()
    gmail.after_modify = lambda mid: gmail.edit(mid, {"INBOX", "label_important"})
    engine.apply()
    assert store.message("m1")["state"] == "uncertain"
    engine.apply()
    assert gmail.messages["m1"].labels == {"INBOX", "label_important"}


def test_reply_in_existing_thread_is_classified_separately(store):
    engine, gmail, jev = pipeline(store)
    engine.preview()
    gmail.messages["m2"] = message("m2", "Payment failed", thread="m1")
    gmail.changed = {"m2"}
    engine.sync()
    engine.preview()
    assert [m.id for m, _ in jev.calls] == ["m1", "m2"]


def test_expired_history_reconciles_archived_and_queues_all_new_ids(store):
    engine, gmail, _ = pipeline(store)
    engine.preview()
    engine.apply()
    gmail.edit("m1", {"label_ignore"})
    gmail.messages.update({f"m{i}": message(f"m{i}") for i in range(2, 8)})
    gmail.expired = True
    result = engine.sync(limit=2)
    assert result["history_reset"] == 1
    assert result["queued"] == 4
    assert store.message("m1")["override"] == "ignore"
    gmail.expired = False
    gmail.changed = set()
    engine.sync(limit=10)
    assert store.one("SELECT COUNT(*) AS n FROM messages")["n"] == 7


def test_fetch_failure_keeps_work_durable_after_checkpoint(store):
    engine, gmail, _ = pipeline(store)
    gmail.changed = {"m1"}
    gmail.fail_get = "m1"
    with pytest.raises(RemoteError):
        engine.sync()
    assert store.rows("SELECT * FROM sync_queue") == [{"message_id": "m1"}]
    gmail.fail_get = None
    gmail.changed = set()
    engine.sync()
    assert not store.rows("SELECT * FROM sync_queue")


def test_failed_inference_not_ignore_and_retry_next_run(store):
    engine, _, jev = pipeline(store)
    jev.fail = True
    assert engine.preview()["counts"]["errors"] == 1
    assert store.latest("m1") is None
    jev.fail = False
    assert engine.preview()["counts"]["spam"] == 1


def test_explicit_confirmation_and_revoke(store):
    engine, _, _ = pipeline(store)
    engine.preview()
    engine.confirm("m1", "spam")
    assert store.examples()[0]["category"] == "spam"
    engine.revoke("m1")
    assert store.examples() == []
    assert store.latest("m1") is not None  # Keep the original prediction for audit.
    assert store.message("m1")["needs_prediction"]
    assert store.message("m1")["state"] == "normal"
    assert engine.apply()["needs_preview"] == 1
    learn(store, Policy(), activate=True)
    engine.preview()
    assert not store.message("m1")["needs_prediction"]


def test_deleted_feedback_cannot_return_through_rollback(store):
    engine, _, _ = pipeline(store)
    engine.confirm("m1", "spam")
    vid, _ = learn(store, Policy(), activate=True)
    store.forget_feedback(["m1"])
    assert store.examples() == []
    assert not store.rows("SELECT * FROM feedback")
    with pytest.raises(AppError):
        store.version(vid)
    assert store.message("m1")["state"] == "removed"


def test_version_changes_require_explicit_reclassification_and_preserve_overrides(store):
    engine, _, jev = pipeline(store)
    engine.preview()
    first = store.get_meta("active_version")
    second, _ = learn(store, Policy(min_probability=0.99), activate=True)
    assert first != second
    assert engine.apply()["stale_version"] == 1
    engine.preview(reclassify=True)
    assert store.latest("m1")["version_id"] == second
    store.set_meta("active_version", first)
    engine.confirm("m1", "important")
    engine.preview(reclassify=True)
    assert len(jev.calls) == 2
    assert store.message("m1")["override"] == "important"


def test_binding_prevents_cross_account_data(store):
    store.bind_account("first@example.com")
    with pytest.raises(AppError):
        store.bind_account("second@example.com")


def test_reclassification_progresses_in_batches(store):
    engine, _, jev = pipeline(store, [message(str(i)) for i in range(5)])
    engine.preview()
    second, _ = learn(store, Policy(min_probability=0.99), activate=True)
    for _ in range(3):
        engine.preview(limit=2, reclassify=True)
    assert len(jev.calls) == 10
    assert all(store.latest(str(i))["version_id"] == second for i in range(5))


def test_no_body_in_sqlite_even_if_gmail_response_contains_one(store):
    from better_email.domain import Message

    m = Message.from_gmail(
        {
            "id": "safe",
            "snippet": "SECRET_SNIPPET",
            "payload": {
                "headers": [
                    {"name": "From", "value": "a@example.com"},
                    {"name": "Subject", "value": "Receipt"},
                    {"name": "To", "value": "SECRET_RECIPIENT"},
                ],
                "body": {"data": "SECRET_BODY"},
                "parts": [{"filename": "SECRET_ATTACHMENT"}],
            },
        }
    )
    store.put_message(m)
    dump = "\n".join(store.db.iterdump())
    assert "SECRET" not in dump
    assert json.loads(store.message("safe")["labels"]) == []
