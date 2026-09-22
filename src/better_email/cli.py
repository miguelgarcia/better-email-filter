from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tomllib
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .domain import CATEGORIES, LABEL_NAMES, AppError, Message, Policy
from .engine import Engine, learn, training_records
from .evaluation import evaluate
from .gmail import Gmail, authorize, credentials_from_json
from .jev import Jev
from .local import Secrets, default_data_dir, locked_directory
from .store import Store


def positive(value):
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("Must be between 1 and 10000.")
    return number


def parser():
    p = argparse.ArgumentParser(description="Gmail triage with feedback and bounded body excerpts.")
    p.add_argument("--data-dir", type=Path, default=default_data_dir())
    p.add_argument("--config", type=Path, help="TOML policy used when creating a learning version")
    sub = p.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="Store credentials in macOS Keychain")
    auth.add_argument("provider", choices=["gmail", "jev"])
    auth.add_argument("--client", type=Path, help="Google Desktop OAuth client JSON")
    for name in ("sync", "preview", "apply"):
        command = sub.add_parser(
            name,
            help={
                "sync": "Read Gmail metadata and collect corrections; no Jev calls or Gmail writes",
                "preview": "Sync and classify with configured email context; no Gmail writes",
                "apply": "Sync and apply cached predictions as custom labels; no Jev calls",
            }[name],
        )
        command.add_argument("--limit", type=positive, default=200)
        command.add_argument(
            "--query", default="", help="Gmail query for initial discovery/backfill"
        )
        command.add_argument(
            "--backfill", action="store_true", help="Discover another inbox sample"
        )
        if name == "preview":
            command.add_argument(
                "--reclassify",
                action="store_true",
                help="Re-evaluate older policy versions; preserve user corrections",
            )
        if name == "apply":
            command.add_argument(
                "--cleanup",
                action="store_true",
                help="After labeling, trash Spam and archive Ignore/Review inbox messages",
            )
    show = sub.add_parser("list", help="Show local messages and decisions")
    show.add_argument("--limit", type=positive, default=50)
    show.add_argument(
        "--show-headers", action="store_true", help="Include sender/subject in output"
    )
    feedback = sub.add_parser("feedback", help="Confirm, revoke, export, or delete human labels")
    fsub = feedback.add_subparsers(dest="action", required=True)
    confirm = fsub.add_parser(
        "confirm", help="Explicitly label a message, including a correct prediction"
    )
    confirm.add_argument("message_id")
    confirm.add_argument("category", choices=CATEGORIES)
    confirm.add_argument("--language", choices=["es", "en", "other", "unknown"])
    revoke = fsub.add_parser(
        "revoke", help="Release a correction or suspended message for a fresh preview"
    )
    revoke.add_argument("message_id")
    export = fsub.add_parser(
        "export", help="Export human-labeled examples, including saved excerpts"
    )
    export.add_argument("path", type=Path)
    delete = fsub.add_parser(
        "delete", help="Erase feedback and invalidate example snapshots locally"
    )
    delete.add_argument("message_id")
    fsub.add_parser("prune", help="Erase expired feedback using config retention_days")
    learning = sub.add_parser(
        "learn", help="Snapshot current feedback into a version for classification"
    )
    learning.add_argument(
        "--activate", action="store_true", help="Use this version on the next preview"
    )
    learning.add_argument(
        "--limit", type=positive, default=200, help="Maximum example bodies to fetch from Gmail"
    )
    sub.add_parser("versions", help="List learning versions")
    activate = sub.add_parser("activate", help="Activate an existing version, including rollback")
    activate.add_argument("version_id")
    evaluation = sub.add_parser(
        "evaluate", help="Evaluate versions on held-out human labels using Jev"
    )
    evaluation.add_argument("--version", help="Defaults to active version")
    evaluation.add_argument(
        "--compare", help="Optional baseline version to compare on the same emails"
    )
    evaluation.add_argument("--limit", type=positive, default=200)
    evaluation.add_argument(
        "--output", type=Path, help="Write report privately instead of to stdout"
    )
    sub.add_parser("status", help="Show local counts and setup state without accessing credentials")
    sub.add_parser("probe", help="Check Jev access using synthetic sender/subject only; no Gmail")
    return p


def read_policy(path, base=None):
    try:
        settings = dict(base or {})
        if path is not None:
            settings.update(tomllib.loads(path.read_text()))
        return Policy(**settings)
    except (TypeError, tomllib.TOMLDecodeError):
        raise AppError("Invalid policy TOML. See config.example.toml.") from None


def emit(value):
    # Escaped JSON keeps email text from injecting terminal control sequences.
    print(json.dumps(value, ensure_ascii=True, indent=2))


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Do not silently overwrite a dataset or report.
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        json.dump(value, handle, ensure_ascii=True, indent=2)
        handle.write("\n")


def run(args):
    with locked_directory(args.data_dir), ExitStack() as stack:
        store = Store(args.data_dir / "state.sqlite3")
        stack.callback(store.close)

        def secrets():
            return Secrets(args.data_dir)

        def gmail():
            vault = secrets()
            client = Gmail(
                credentials_from_json(vault.get("gmail")), lambda value: vault.set("gmail", value)
            )
            stack.callback(client.close)
            return client

        def jev():
            key = os.environ.get("TYPESAFE_API_KEY") or secrets().get("jev")
            client = Jev(key)
            stack.callback(client.close)
            return client

        def policy_for_learning():
            base = {}
            if store.get_meta("active_version"):
                base = store.version()[1]["policy"]
            elif store.get_meta("last_policy"):
                base = json.loads(store.get_meta("last_policy"))
            return read_policy(args.config, base)

        if args.command == "auth":
            vault = secrets()
            if args.provider == "jev":
                key = getpass.getpass("Jev API key (saved in macOS Keychain): ").strip()
                if not key:
                    raise AppError("API key cannot be empty.")
                vault.set("jev", key)
                return {"saved": "jev"}
            if not args.client:
                raise AppError("Use `auth gmail --client /path/to/desktop-client.json`.")
            credentials = authorize(args.client)
            client = Gmail(credentials, lambda value: None)
            stack.callback(client.close)
            store.bind_account(client.profile()["emailAddress"])
            vault.set("gmail", credentials.to_json())
            return {"saved": "gmail"}

        if args.command == "probe":
            from dataclasses import asdict

            from .jev import build_payload

            policy = policy_for_learning()
            sample = Message(
                "synthetic",
                "synthetic",
                "Tienda <offers@example.com>",
                "Oferta especial: 50% de descuento",
            )
            snapshot = {
                "policy": policy.data(),
                "train": [],
                "questions": build_payload(sample, policy, [])["questions"],
            }
            return {"synthetic_probe": asdict(jev().classify(sample, snapshot))}

        if args.command in {"sync", "preview", "apply"}:
            engine = Engine(store, gmail())
            synced = engine.sync(args.limit, args.query, args.backfill)
            if args.command == "sync":
                return {"sync": synced, "human_examples": len(store.examples())}
            if args.command == "preview":
                if not store.get_meta("active_version"):
                    learn(store, policy_for_learning(), activate=True)
                _, current = store.version()
                saved = sorted(current["train"] + current["holdout"], key=lambda e: e["message_id"])
                active_policy = Policy.from_snapshot(current["policy"])
                refresh_needed = saved != sorted(
                    training_records(store, active_policy), key=lambda e: e["message_id"]
                )
                engine.classifier = jev()
                context = "sender/subject"
                if active_policy.body_words:
                    context += f" and up to {active_policy.body_words} body words"
                print(f"Classifying {context} with Jev; no Gmail changes.", file=sys.stderr)
                return {
                    "sync": synced,
                    "preview": engine.preview(args.limit, args.reclassify),
                    "learning_refresh_needed": refresh_needed,
                    "learning_command": "learn --activate" if refresh_needed else None,
                }
            applied = engine.apply(args.limit)
            result = {"sync": synced, "apply": applied}
            if args.cleanup:
                if applied.get("errors") or applied.get("uncertain"):
                    result["cleanup"] = {
                        "skipped": "Label application failed or was uncertain",
                        "errors": 1,
                    }
                else:
                    result["cleanup"] = engine.cleanup(args.limit)
            return result

        if args.command == "learn":
            policy = policy_for_learning()
            excerpts = {}
            if policy.body_words and store.examples():
                excerpts = Engine(store, gmail()).enrich_examples(policy, args.limit)
            vid, snapshot = learn(store, policy, args.activate)
            return {
                "version": vid,
                "active": store.get_meta("active_version") == vid,
                "training_examples": len(snapshot["train"]),
                "held_out_examples": len(snapshot["holdout"]),
                "example_excerpts": excerpts,
                "body_words": policy.body_words,
                "profile": policy.profile,
            }

        if args.command == "versions":
            return {
                "active": store.get_meta("active_version"),
                "versions": store.rows(
                    "SELECT id,created_at FROM versions ORDER BY created_at DESC"
                ),
            }
        if args.command == "activate":
            vid, _ = store.version(args.version_id)
            store.set_meta("active_version", vid)
            return {"active": vid}
        if args.command == "evaluate":
            vid, snapshot = store.version(args.version)
            snapshots = {vid: snapshot}
            if args.compare:
                other, baseline = store.version(args.compare)
                snapshots[other] = baseline
            report = evaluate(jev(), snapshots, snapshot["holdout"], args.limit)
            if args.output:
                write_private(args.output, report)
                return {"report": str(args.output)}
            return report
        if args.command == "feedback":
            if args.action == "export":
                write_private(args.path, store.examples())
                return {"export": str(args.path), "examples": len(store.examples())}
            if args.action in {"delete", "prune"}:
                if args.action == "delete":
                    if not store.message(args.message_id):
                        raise AppError("Unknown message ID.")
                    mids = [args.message_id]
                else:
                    policy = policy_for_learning()
                    if not policy.retention_days:
                        return {"deleted": 0, "retention": "until explicitly deleted"}
                    cutoff = (datetime.now(UTC) - timedelta(days=policy.retention_days)).isoformat()
                    mids = [
                        r["message_id"]
                        for r in store.rows(
                            "SELECT message_id FROM feedback GROUP BY message_id "
                            "HAVING MAX(created_at)<?",
                            (cutoff,),
                        )
                    ]
                if mids:
                    store.forget_feedback(mids)
                return {
                    "deleted": len(mids),
                    "snapshots_invalidated": bool(mids),
                    "gmail_changed": False,
                }
            engine = Engine(store, gmail())
            engine.sync()
            if args.action == "confirm":
                engine.confirm(args.message_id, args.category)
                if args.language:
                    store.execute(
                        "UPDATE messages SET language=? WHERE id=?",
                        (args.language, args.message_id),
                    )
            else:
                engine.revoke(args.message_id)
            return {
                "feedback": args.action,
                "message_id": args.message_id,
                "next": "Run learn --activate to refresh the examples used by Jev.",
            }
        if args.command == "list":
            results = []
            for row in store.rows(
                "SELECT * FROM messages ORDER BY updated_at DESC,id LIMIT ?", (args.limit,)
            ):
                prediction = store.latest(row["id"])
                proposed_label = None
                if row["state"] in {"normal", "corrected"}:
                    if row["override"]:
                        proposed_label = LABEL_NAMES[row["override"]]
                    elif (
                        prediction
                        and not row["needs_prediction"]
                        and (prediction["version_id"] == store.get_meta("active_version"))
                    ):
                        decision = prediction["decision"]
                        proposed_label = LABEL_NAMES[
                            "review" if decision["review"] else decision["category"]
                        ]
                result = {
                    "id": row["id"],
                    "state": row["state"],
                    "user_category": row["override"],
                    "prediction": prediction["decision"] if prediction else None,
                    "proposed_label": proposed_label,
                    "cleanup": store.one(
                        "SELECT kind,status FROM cleanup_actions WHERE message_id=?", (row["id"],)
                    ),
                }
                if args.show_headers:
                    result.update(sender=row["sender"], subject=row["subject"])
                results.append(result)
            return {"messages": results}
        return {
            "account_connected": bool(store.get_meta("account")),
            "messages": store.one("SELECT COUNT(*) AS n FROM messages")["n"],
            "human_examples": len(store.examples()),
            "active_version": store.get_meta("active_version"),
            "queued": store.one("SELECT COUNT(*) AS n FROM sync_queue")["n"],
            "cleanup": store.rows(
                "SELECT status,COUNT(*) AS count FROM cleanup_actions GROUP BY status"
            ),
            "states": store.rows("SELECT state,COUNT(*) AS count FROM messages GROUP BY state"),
        }


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        emit(result)
        if has_errors(result):
            return 1
    except AppError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, KeyboardInterrupt):
        print(
            "Stopped: local I/O failed or command was interrupted. Pending work is retained.",
            file=sys.stderr,
        )
        return 1
    return 0


def has_errors(value):
    if isinstance(value, dict):
        return any(
            (key == "errors" and isinstance(item, int) and item > 0) or has_errors(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(has_errors(item) for item in value)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
