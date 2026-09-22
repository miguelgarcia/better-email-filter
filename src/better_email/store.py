from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .domain import AppError, Decision, Message, canonical, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
 truncated INTEGER NOT NULL, labels TEXT NOT NULL, baseline TEXT NOT NULL DEFAULT '[]',
 state TEXT NOT NULL DEFAULT 'normal', override TEXT, language TEXT NOT NULL DEFAULT 'unknown',
 needs_prediction INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
 id INTEGER PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
 version_id TEXT NOT NULL, decision TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prediction_message ON predictions(message_id, id DESC);
CREATE TABLE IF NOT EXISTS feedback (
 id INTEGER PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
 category TEXT, source TEXT NOT NULL, prediction_id INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
 id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
 id INTEGER PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
 before_labels TEXT NOT NULL, target_labels TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (
 id INTEGER PRIMARY KEY, message_id TEXT, stage TEXT NOT NULL,
 code TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_queue (message_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS cleanup_actions (
 message_id TEXT PRIMARY KEY REFERENCES messages(id), kind TEXT NOT NULL,
 before_labels TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        # Check before changing a future database schema.
        has_meta = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        version = self.get_meta("schema_version") if has_meta else None
        if version not in (None, "1", "2"):
            self.db.close()
            raise AppError("Unsupported database version.")
        self.db.executescript(SCHEMA)
        path.chmod(0o600)
        if version != "2":
            with self.db:
                self.db.execute("BEGIN")
                for definition in (
                    "body_excerpt TEXT NOT NULL DEFAULT ''",
                    "body_truncated INTEGER NOT NULL DEFAULT 0",
                    "body_status TEXT NOT NULL DEFAULT 'not_fetched'",
                    "body_word_limit INTEGER NOT NULL DEFAULT 0",
                ):
                    self.db.execute("ALTER TABLE messages ADD COLUMN " + definition)
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version','2')")

    def close(self):
        self.db.close()

    def execute(self, sql, params=()):
        with self.db:
            return self.db.execute(sql, params)

    def rows(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params)]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_meta(self, key):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key, value):
        self.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def bind_account(self, account):
        current = self.get_meta("account")
        if current and current != account:
            raise AppError("This data directory belongs to a different Gmail account.")
        self.set_meta("account", account)

    def message(self, mid):
        return self.one("SELECT * FROM messages WHERE id=?", (mid,))

    def put_message(self, message: Message):
        self.execute(
            """INSERT INTO messages
               (id,thread_id,sender,subject,truncated,labels,updated_at) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET thread_id=excluded.thread_id,
               sender=excluded.sender,subject=excluded.subject,truncated=excluded.truncated,
               labels=excluded.labels,updated_at=excluded.updated_at""",
            (
                message.id,
                message.thread_id,
                message.sender,
                message.subject,
                message.truncated,
                canonical(sorted(message.labels)),
                now(),
            ),
        )

    def observe(self, mid, owned, state, override):
        self.execute(
            "UPDATE messages SET baseline=?,state=?,override=? WHERE id=?",
            (canonical(sorted(owned)), state, override, mid),
        )

    def put_excerpt(self, message: Message):
        self.execute(
            "UPDATE messages SET body_excerpt=?,body_truncated=?,body_status=?,body_word_limit=? "
            "WHERE id=?",
            (
                message.body_excerpt,
                message.body_truncated,
                message.body_status,
                message.body_word_limit,
                message.id,
            ),
        )

    def latest(self, mid):
        row = self.one(
            "SELECT * FROM predictions WHERE message_id=? ORDER BY id DESC LIMIT 1", (mid,)
        )
        if row:
            row["decision"] = json.loads(row["decision"])
        return row

    def predict(self, mid, version_id, decision: Decision):
        with self.db:
            self.db.execute(
                "INSERT INTO predictions(message_id,version_id,decision,created_at) "
                "VALUES (?,?,?,?)",
                (mid, version_id, canonical(asdict(decision)), now()),
            )
            self.db.execute("UPDATE messages SET needs_prediction=0 WHERE id=?", (mid,))

    def feedback(self, mid, category, source, owned, state="corrected"):
        prediction = self.latest(mid)
        with self.db:
            self.db.execute(
                """INSERT INTO feedback(message_id,category,source,prediction_id,created_at)
                   VALUES (?,?,?,?,?)""",
                (mid, category, source, prediction["id"] if prediction else None, now()),
            )
            self.db.execute(
                "UPDATE messages SET baseline=?,state=?,override=? WHERE id=?",
                (canonical(sorted(owned)), state, category, mid),
            )
            if source == "revoked":
                self.db.execute(
                    "UPDATE actions SET status='cancelled' WHERE message_id=? "
                    "AND status IN ('pending','uncertain')",
                    (mid,),
                )
                self.db.execute("UPDATE messages SET needs_prediction=1 WHERE id=?", (mid,))
                self.invalidate_versions()

    def finish_action(self, aid, mid, owned, action_status, state, override):
        # The journal and baseline must commit together. Otherwise a crash between
        # them can turn an application write into apparent human feedback.
        with self.db:
            self.db.execute("UPDATE actions SET status=? WHERE id=?", (action_status, aid))
            self.db.execute(
                "UPDATE messages SET baseline=?,state=?,override=? WHERE id=?",
                (canonical(sorted(owned)), state, override, mid),
            )

    def invalidate_versions(self):
        # Called inside a transaction; preserve configuration without retaining examples.
        if self.get_meta("active_version"):
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('last_policy',?)",
                (canonical(self.version()[1]["policy"]),),
            )
        self.db.execute("DELETE FROM versions")
        self.db.execute("DELETE FROM meta WHERE key='active_version'")

    def examples(self):
        return self.rows(
            """SELECT m.id AS message_id,m.thread_id,m.sender,m.subject,
               m.override AS category,m.language,f.created_at,
               m.body_excerpt,m.body_truncated,m.body_status,m.body_word_limit
               FROM messages m JOIN feedback f ON f.id=(
                 SELECT MAX(id) FROM feedback WHERE message_id=m.id)
               WHERE m.override IS NOT NULL AND m.state='corrected' AND f.category IS NOT NULL"""
        )

    def save_version(self, snapshot):
        vid = digest(snapshot)
        self.execute(
            "INSERT OR IGNORE INTO versions VALUES (?,?,?)", (vid, canonical(snapshot), now())
        )
        return vid

    def version(self, vid=None):
        vid = vid or self.get_meta("active_version")
        row = self.one("SELECT * FROM versions WHERE id=?", (vid,))
        if not row:
            raise AppError("Unknown policy version. Run `learn` to create one.")
        return row["id"], json.loads(row["snapshot"])

    def action(self, mid, before, target):
        return self.execute(
            """INSERT INTO actions(message_id,before_labels,target_labels,status,created_at)
               VALUES (?,?,?,'pending',?)""",
            (mid, canonical(sorted(before)), canonical(sorted(target)), now()),
        ).lastrowid

    def failure(self, mid, stage, code):
        # Only application-owned error codes, never a remote exception body.
        self.execute(
            "INSERT INTO failures(message_id,stage,code,created_at) VALUES (?,?,?,?)",
            (mid, stage, str(code), now()),
        )

    def start_cleanup(self, mid, kind, labels):
        self.execute(
            "INSERT INTO cleanup_actions VALUES (?,?,?,'pending',?)",
            (mid, kind, canonical(sorted(labels)), now()),
        )

    def reconcile_cleanup(self, message):
        action = self.one(
            "SELECT * FROM cleanup_actions WHERE message_id=? AND status='pending'",
            (message.id,),
        )
        if not action:
            return
        complete = (
            "TRASH" in message.labels
            if action["kind"] == "trash"
            else not {"INBOX", "TRASH", "SPAM"} & message.labels
        )
        self.execute(
            "UPDATE cleanup_actions SET status=? WHERE message_id=?",
            ("applied" if complete else "uncertain", message.id),
        )

    def forget_feedback(self, mids: list[str]):
        """Delete human labels and ALL snapshots which could contain those examples."""
        with self.db:
            for mid in mids:
                self.db.execute("DELETE FROM feedback WHERE message_id=?", (mid,))
                self.db.execute(
                    "UPDATE messages SET override=NULL,state='removed',body_excerpt='',"
                    "body_truncated=0,body_status='not_fetched',body_word_limit=0 WHERE id=?",
                    (mid,),
                )
            # Avoid resurrecting erased data through rollback; predictions store IDs only.
            self.invalidate_versions()
        self.db.execute("VACUUM")


def row_message(row):
    return Message(
        row["id"],
        row["thread_id"],
        row["sender"],
        row["subject"],
        frozenset(json.loads(row["labels"])),
        bool(row["truncated"]),
        row.get("body_excerpt", ""),
        bool(row.get("body_truncated", False)),
        row.get("body_status", "not_fetched"),
        row.get("body_word_limit", 0),
    )
