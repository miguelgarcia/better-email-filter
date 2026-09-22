from conftest import FakeGmail, FakeJev, message

from better_email.domain import CATEGORIES, Policy, grouped, split_examples
from better_email.engine import Engine, learn
from better_email.evaluation import evaluate, metrics, uncontaminated


def example(mid, thread=None, subject="Receipt 123", sender="shop@example.com", category="ignore"):
    return {
        "message_id": mid,
        "thread_id": thread or mid,
        "sender": sender,
        "subject": subject,
        "category": category,
    }


def test_transitive_thread_and_template_grouping():
    records = [
        example("a", "thread-a"),
        example("b", "thread-b", "Receipt 456"),
        example("c", "thread-b", "Different subject"),
        example("d", subject="Unrelated", sender="other@example.com"),
    ]
    groups = grouped(records)
    assert sorted(len(g) for g in groups) == [1, 3]
    train, holdout = split_examples(records)
    a = {r["message_id"] for r in train}
    b = {r["message_id"] for r in holdout}
    assert {"a", "b", "c"} <= a or {"a", "b", "c"} <= b


def test_evaluation_excludes_leakage_from_either_compared_version():
    holdout = [example("test1", subject="Receipt 100"), example("test2", subject="Appointment")]
    snapshots = [{"train": []}, {"train": [example("train1", subject="Receipt 200")]}]
    assert [r["message_id"] for r in uncontaminated(holdout, snapshots)] == ["test2"]


def test_metrics_show_important_misses_review_errors_and_denominators():
    results = [
        {
            "actual": "important",
            "category": "spam",
            "review": False,
            "input_tokens": 5,
            "output_tokens": 0,
        },
        {
            "actual": "important",
            "category": "ignore",
            "review": True,
            "input_tokens": 5,
            "output_tokens": 0,
        },
        {"actual": "spam", "error": "failed"},
    ]
    report = metrics(results)
    assert report["sample_size"] == 3
    assert report["important_labeled_low_without_review"] == 1
    assert report["review_count"] == 1
    assert report["errors"] == 1
    assert report["precision"]["important"] is None
    assert report["support"]["important"] == 2


def test_learning_only_uses_human_examples_and_versions_can_roll_back(store):
    gmail = FakeGmail(
        [message(str(i), subject=f"Oferta especial {i}", thread=str(i)) for i in range(20)]
    )
    engine = Engine(store, gmail, FakeJev())
    base, _ = learn(store, Policy(), activate=True)
    engine.sync()
    engine.preview()
    assert store.examples() == []
    engine.confirm("1", "spam")
    candidate, snapshot = learn(store, Policy())
    assert store.get_meta("active_version") == base
    assert len(snapshot["train"]) + len(snapshot["holdout"]) == 1
    assert candidate != base
    store.set_meta("active_version", candidate)
    assert store.version()[0] == candidate
    store.set_meta("active_version", base)
    assert store.message("1")["override"] == "spam"


def test_evaluation_reports_same_samples_per_version_and_language(store):
    _, base = learn(store, Policy())
    sample = [
        dict(example("a", subject="Oferta", category="spam"), language="es"),
        dict(example("b", subject="Offer", category="spam"), language="en"),
    ]
    report = evaluate(FakeJev(), {"baseline": base, "candidate": base}, sample)
    assert report["eligible"] == 2
    assert set(report["versions"]["candidate"]["by_language"]) == {"es", "en"}
    assert report["versions"]["baseline"]["metrics"] == report["versions"]["candidate"]["metrics"]
    assert set(report["versions"]["candidate"]["metrics"]["confusion_matrix"]) == set(CATEGORIES)
