from __future__ import annotations

import json
from collections import Counter

from .domain import CATEGORIES, LABEL_NAMES, AppError, Message, Policy, RemoteError, split_examples
from .jev import build_payload, example_content
from .store import Store, row_message


def training_records(store: Store, policy: Policy):
    records = []
    for example in store.examples():
        record = {k: v for k, v in example.items() if not k.startswith("body_")}
        record.update(example_content(example, policy.body_words))
        records.append(record)
    return records


def learn(store: Store, policy: Policy, activate=False):
    train, holdout = split_examples(training_records(store, policy))
    questions = build_payload(Message("", "", "", ""), policy, [])["questions"]
    snapshot = {
        "format": 2,
        "questions": questions,
        "policy": policy.data(),
        "train": train,
        "holdout": holdout,
    }
    vid = store.save_version(snapshot)
    if activate or not store.get_meta("active_version"):
        store.set_meta("active_version", vid)
    return vid, snapshot


class Engine:
    def __init__(self, store, gmail, classifier=None):
        self.store = store
        self.gmail = gmail
        self.classifier = classifier
        self.labels = {}

    def owned(self, message):
        return set(message.labels) & set(self.labels.values())

    def ensure_excerpt(self, row, policy):
        message = row_message(row)
        if not policy.body_words or (
            message.body_status != "not_fetched" and message.body_word_limit >= policy.body_words
        ):
            return message
        enriched = self.gmail.get_body(message.id, policy.body_words)
        self.reconcile(enriched)
        self.store.put_excerpt(enriched)
        return enriched

    def enrich_examples(self, policy, limit=200):
        counts = Counter()
        if not policy.body_words:
            return dict(counts)
        self.store.bind_account(self.gmail.profile()["emailAddress"])
        self.labels = self.gmail.labels()
        attempts = 0
        for example in self.store.examples():
            if (
                example["body_status"] != "not_fetched"
                and example["body_word_limit"] >= policy.body_words
            ):
                continue
            if attempts >= limit:
                counts["remaining"] += 1
                continue
            attempts += 1
            try:
                self.ensure_excerpt(self.store.message(example["message_id"]), policy)
                counts["fetched"] += 1
            except RemoteError as exc:
                if exc.status != 404:
                    raise
                self.store.execute(
                    "UPDATE messages SET state='missing' WHERE id=?", (example["message_id"],)
                )
                counts["missing"] += 1
        return dict(counts)

    def reconcile(self, message):
        old = self.store.message(message.id)
        current = self.owned(message)
        pending = self.store.one(
            "SELECT * FROM actions WHERE message_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (message.id,),
        )
        self.store.put_message(message)
        self.store.reconcile_cleanup(message)
        if pending:
            target = set(json.loads(pending["target_labels"]))
            if current == target:
                # Our write may have succeeded before a crash; this is NOT feedback.
                self.store.finish_action(
                    pending["id"], message.id, current, "applied", old["state"], old["override"]
                )
                return
            # Even original state could mean a user undid our write. Don't guess/retry.
            self.store.finish_action(
                pending["id"], message.id, current, "uncertain", "uncertain", old["override"]
            )
            return
        before = set(json.loads(old["baseline"])) if old else set()
        if current == before:
            return
        choices = [c for c in CATEGORIES if self.labels.get(c) in current]
        override = old["override"] if old else None
        if len(choices) == 1:
            override = choices[0]
            self.store.feedback(message.id, override, "gmail", current)
            return
        elif len(choices) > 1:
            state = "conflict"
        elif not current:
            state = "removed"
        else:
            state = "review_requested"
        self.store.observe(message.id, current, state, override)

    def sync(self, limit=200, query="", backfill=False):
        profile = self.gmail.profile()
        self.store.bind_account(profile["emailAddress"])
        self.labels = self.gmail.labels()
        checkpoint = self.store.get_meta("history_id")
        ids = set()
        reset = False
        if checkpoint:
            try:
                ids, new_checkpoint = self.gmail.history(checkpoint)
            except RemoteError as exc:
                if exc.status != 404:
                    raise
                reset = True
                # Full inbox ID discovery on expiry, bounded metadata fetch below.
                ids.update(self.gmail.inbox_ids(limit=None))
                ids.update(r["id"] for r in self.store.rows("SELECT id FROM messages"))
                new_checkpoint = profile["historyId"]
        else:
            ids.update(self.gmail.inbox_ids(limit=limit, query=query))
            new_checkpoint = profile["historyId"]  # Captured before discovery.
        if backfill:
            ids.update(self.gmail.inbox_ids(limit=limit, query=query))
        ids.update(
            r["message_id"]
            for r in self.store.rows("SELECT message_id FROM actions WHERE status='pending'")
        )
        ids.update(
            r["message_id"]
            for r in self.store.rows(
                "SELECT message_id FROM cleanup_actions WHERE status='pending'"
            )
        )
        # Advance only after IDs are durable; failed or capped fetches stay queued.
        with self.store.db:
            self.store.db.executemany(
                "INSERT OR IGNORE INTO sync_queue VALUES (?)", [(mid,) for mid in ids]
            )
            self.store.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('history_id',?)", (new_checkpoint,)
            )
        queue = self.store.rows(
            """SELECT q.message_id,m.id AS known FROM sync_queue q LEFT JOIN messages m
               ON m.id=q.message_id ORDER BY (m.id IS NULL),q.message_id"""
        )
        counts = Counter(history_reset=int(reset))
        new_count = 0
        for item in queue:
            if not item["known"] and new_count >= limit:
                continue
            mid = item["message_id"]
            try:
                message = self.gmail.get(mid)
            except RemoteError as exc:
                if exc.status == 404:
                    self.store.execute("UPDATE messages SET state='missing' WHERE id=?", (mid,))
                    self.store.execute(
                        "UPDATE cleanup_actions SET status='missing' "
                        "WHERE message_id=? AND status='pending'",
                        (mid,),
                    )
                    self.store.execute("DELETE FROM sync_queue WHERE message_id=?", (mid,))
                    counts["missing"] += 1
                    continue
                self.store.failure(mid, "sync", exc.status)
                # Stop: don't run inference using feedback we failed to synchronize.
                raise
            if item["known"] or (
                "INBOX" in message.labels and not {"SPAM", "TRASH", "DRAFT"} & message.labels
            ):
                self.reconcile(message)
                counts["synced"] += 1
                if not item["known"]:
                    new_count += 1
            self.store.execute("DELETE FROM sync_queue WHERE message_id=?", (mid,))
        counts["queued"] = self.store.one("SELECT COUNT(*) AS n FROM sync_queue")["n"]
        return dict(counts)

    def preview(self, limit=200, reclassify=False):
        vid, snapshot = self.store.version()
        policy = Policy.from_snapshot(snapshot["policy"])
        counts = Counter()
        review_reasons = Counter()
        results = []
        calls = 0
        for row in self.store.rows("SELECT * FROM messages ORDER BY updated_at DESC,id"):
            message = row_message(row)
            if "INBOX" not in message.labels or {"TRASH", "SPAM", "DRAFT"} & message.labels:
                continue
            if row["state"] not in {"normal", "corrected"}:
                counts[row["state"]] += 1
                continue
            if row["override"]:
                counts["user_corrected"] += 1
                continue
            latest = self.store.latest(message.id)
            if (
                latest
                and not row["needs_prediction"]
                and (not reclassify or latest["version_id"] == vid)
            ):
                counts["cached"] += 1
                continue
            if calls >= limit:
                counts["remaining"] += 1
                continue
            calls += 1
            try:
                message = self.ensure_excerpt(row, policy)
                current = self.store.message(message.id)
                if current["override"] or current["state"] != "normal":
                    counts["skipped_edit"] += 1
                    continue
                if "INBOX" not in message.labels or {"SPAM", "TRASH", "DRAFT"} & message.labels:
                    counts["outside_inbox"] += 1
                    continue
                decision = self.classifier.classify(message, snapshot)
            except AppError as exc:
                self.store.failure(message.id, "classification", getattr(exc, "status", "invalid"))
                counts["errors"] += 1
                # Authentication/config errors shouldn't consume the entire batch.
                if isinstance(exc, RemoteError) and exc.status in {401, 403, 402}:
                    raise
                continue
            self.store.predict(message.id, vid, decision)
            counts["review" if decision.review else decision.category] += 1
            review_reasons.update(decision.review_reasons)
            counts["input_tokens"] += decision.input_tokens
            counts["output_tokens"] += decision.output_tokens
            results.append(
                {
                    "id": message.id,
                    "category": decision.category,
                    "proposed_label": LABEL_NAMES[
                        "review" if decision.review else decision.category
                    ],
                    "review": decision.review,
                    "reason": decision.reason,
                    "review_reasons": decision.review_reasons,
                }
            )
        return {
            "version": vid,
            "counts": dict(counts),
            "review_reasons": dict(review_reasons),
            "predictions": results,
        }

    def apply(self, limit=200):
        self.labels = self.gmail.ensure_labels()
        counts = Counter()
        attempts = 0
        candidates = self.store.rows("SELECT * FROM messages ORDER BY updated_at DESC,id")
        for old in candidates:
            if attempts >= limit:
                break
            if old["state"] not in {"normal", "corrected"}:
                continue
            prediction = self.store.latest(old["id"])
            if not old["override"] and old["needs_prediction"]:
                counts["needs_preview"] += 1
                continue
            if not old["override"] and not prediction:
                continue
            if not old["override"] and prediction["version_id"] != self.store.get_meta(
                "active_version"
            ):
                counts["stale_version"] += 1
                continue
            try:
                message = self.gmail.get(old["id"])
                self.reconcile(message)
                row = self.store.message(old["id"])
                if row["state"] not in {"normal", "corrected"}:
                    counts["skipped_edit"] += 1
                    continue
                if "INBOX" not in message.labels or {"SPAM", "TRASH", "DRAFT"} & message.labels:
                    counts["outside_inbox"] += 1
                    continue
                if row["override"]:
                    category = row["override"]
                else:
                    decision = prediction["decision"]
                    category = "review" if decision["review"] else decision["category"]
                target = {self.labels[category]}
                before = self.owned(message)
                if before == target:
                    counts["unchanged"] += 1
                    continue
                action_id = self.store.action(message.id, before, target)
                attempts += 1
                self.gmail.modify(
                    message.id, target - before, before - target, self.labels.values()
                )
                # Reconcile the result as an application write, never as training data.
                after = self.gmail.get(message.id)
                self.reconcile(after)
                action = self.store.one("SELECT status FROM actions WHERE id=?", (action_id,))
                counts[action["status"]] += 1
            except RemoteError as exc:
                self.store.failure(old["id"], "apply", exc.status)
                counts["errors"] += 1
                # Leave the journal pending; next sync checks whether Gmail applied it.
                if exc.status in {401, 403}:
                    raise
        return dict(counts)

    def cleanup(self, limit=200):
        """Explicitly enabled inbox cleanup, with at most one attempt per message."""
        self.store.bind_account(self.gmail.profile()["emailAddress"])
        self.labels = self.gmail.labels()
        counts = Counter()
        attempts = 0
        for old in self.store.rows("SELECT * FROM messages ORDER BY updated_at DESC,id"):
            if attempts >= limit:
                break
            if self.store.one("SELECT 1 FROM cleanup_actions WHERE message_id=?", (old["id"],)):
                # Respect manual restores and never blindly repeat an uncertain write.
                counts["previously_attempted"] += 1
                continue
            if old["state"] not in {"normal", "corrected", "review_requested"}:
                continue
            if "INBOX" not in row_message(old).labels:
                continue
            try:
                message = self.gmail.get(old["id"])
                self.reconcile(message)
                row = self.store.message(message.id)
                if row["state"] not in {"normal", "corrected", "review_requested"}:
                    counts["skipped_edit"] += 1
                    continue
                if "INBOX" not in message.labels or {"TRASH", "SPAM", "DRAFT"} & message.labels:
                    continue
                owned = self.owned(message)
                if len(owned) != 1:
                    counts["conflicting_or_missing_labels"] += 1
                    continue
                category = next(c for c, label in self.labels.items() if label in owned)
                if category == "important":
                    continue
                manual_review = row["state"] == "review_requested" and category == "review"
                if row["override"] and not manual_review:
                    if row["override"] != category:
                        counts["skipped_edit"] += 1
                        continue
                elif not manual_review:
                    prediction = self.store.latest(message.id)
                    if (
                        not prediction
                        or row["needs_prediction"]
                        or prediction["version_id"] != self.store.get_meta("active_version")
                    ):
                        counts["needs_preview"] += 1
                        continue
                    decision = prediction["decision"]
                    predicted = "review" if decision["review"] else decision["category"]
                    if predicted != category:
                        counts["needs_apply"] += 1
                        continue
                kind = "trash" if category == "spam" else "archive"
                self.store.start_cleanup(message.id, kind, message.labels)
                attempts += 1
                if kind == "trash":
                    self.gmail.trash(message.id)
                else:
                    self.gmail.archive(message.id)
                self.reconcile(self.gmail.get(message.id))
                action = self.store.one(
                    "SELECT status FROM cleanup_actions WHERE message_id=?", (message.id,)
                )
                if action["status"] == "applied":
                    counts["trashed" if kind == "trash" else "archived"] += 1
                else:
                    counts["uncertain"] += 1
                    counts["errors"] += 1
            except AppError as exc:
                self.store.failure(old["id"], "cleanup", getattr(exc, "status", "invalid"))
                counts["errors"] += 1
                if isinstance(exc, RemoteError) and exc.status in {401, 403}:
                    raise
        return dict(counts)

    def confirm(self, mid, category):
        message = self.gmail.get(mid)
        if not self.store.message(mid):
            raise AppError("Message is not tracked. Discover it with `sync --backfill` first.")
        self.reconcile(message)
        self.store.feedback(mid, category, "explicit", self.owned(message))

    def revoke(self, mid):
        if not self.store.message(mid):
            raise AppError("Unknown message ID.")
        message = self.gmail.get(mid)
        self.reconcile(message)
        # Resume is explicit; baseline current labels prevents re-importing old correction.
        self.store.feedback(mid, None, "revoked", self.owned(message), "normal")
