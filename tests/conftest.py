from dataclasses import replace

import pytest

from better_email.body import cap_excerpt
from better_email.domain import CATEGORIES, Decision, Message, RemoteError
from better_email.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "test.sqlite3")
    yield database
    database.close()


def message(mid="m1", subject="Oferta exclusiva: 50% de descuento", labels=None, thread=None):
    return Message(
        mid,
        thread or mid,
        "Tienda <offers@example.com>",
        subject,
        frozenset(labels if labels is not None else ["INBOX", "UNREAD", "personal"]),
    )


class FakeGmail:
    def __init__(self, messages):
        self.messages = {m.id: m for m in messages}
        self.label_map = {}
        self.changed = set(self.messages)
        self.mutations = []
        self.expired = False
        self.fail_get = None
        self.fail_modify = None
        self.after_modify = None
        self.bodies = {}
        self.body_fetches = []

    def profile(self):
        return {"emailAddress": "owner@example.com", "historyId": "100"}

    def labels(self):
        return self.label_map.copy()

    def ensure_labels(self):
        for c in (*CATEGORIES, "review"):
            if c not in self.label_map:
                self.label_map[c] = "label_" + c
                self.mutations.append(("create", c))
        return self.labels()

    def get(self, mid):
        if mid == self.fail_get:
            raise RemoteError("Gmail", 503)
        if mid not in self.messages:
            raise RemoteError("Gmail", 404)
        return self.messages[mid]

    def get_body(self, mid, words):
        self.body_fetches.append(mid)
        original = self.get(mid)
        excerpt, truncated = cap_excerpt(self.bodies.get(mid, ""), words)
        return replace(
            original,
            body_excerpt=excerpt,
            body_truncated=truncated,
            body_status="available" if excerpt else "unavailable",
            body_word_limit=words,
        )

    def inbox_ids(self, limit=200, query=""):
        ids = [m.id for m in self.messages.values() if "INBOX" in m.labels]
        return ids[:limit] if limit else ids

    def history(self, checkpoint):
        if self.expired:
            raise RemoteError("Gmail", 404)
        return self.changed.copy(), "101"

    def modify(self, mid, add, remove, allowed):
        assert (set(add) | set(remove)) <= set(self.label_map.values())
        if self.fail_modify == "before":
            raise RemoteError("Gmail", "network")
        m = self.messages[mid]
        self.messages[mid] = replace(m, labels=frozenset((m.labels - set(remove)) | set(add)))
        self.mutations.append(("modify", mid, set(add), set(remove)))
        self.changed.add(mid)
        if self.after_modify:
            self.after_modify(mid)
        if self.fail_modify == "after":
            raise RemoteError("Gmail", "network")

    def edit(self, mid, labels):
        self.messages[mid] = replace(self.messages[mid], labels=frozenset(labels))
        self.changed.add(mid)


class FakeJev:
    def __init__(self, category="spam", review=False):
        self.category = category
        self.review = review
        self.calls = []
        self.fail = False

    def classify(self, message, snapshot):
        self.calls.append((message, snapshot))
        if self.fail:
            raise RemoteError("Jev", 503)
        return Decision(
            self.category,
            {c: float(c == self.category) for c in CATEGORIES},
            1,
            1,
            self.review,
            "classifier",
            "jev-test",
            input_tokens=20,
        )
