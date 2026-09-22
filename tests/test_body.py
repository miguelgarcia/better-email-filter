import base64
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeGmail, FakeJev, message
from test_integrations import response

import better_email.cli as cli
from better_email.body import extract_excerpt
from better_email.cli import main, read_policy
from better_email.domain import AppError, Policy
from better_email.engine import Engine, learn, training_records
from better_email.evaluation import evaluate
from better_email.gmail import Gmail
from better_email.jev import Jev, build_payload
from better_email.store import SCHEMA, Store


def part(text, mime="text/plain", charset="utf-8", **extra):
    return {
        "mimeType": mime,
        "headers": [{"name": "Content-Type", "value": f"{mime}; charset={charset}"}],
        "body": {"data": base64.urlsafe_b64encode(text.encode(charset)).decode()},
        **extra,
    }


@pytest.mark.parametrize("charset", ["utf-8", "iso-8859-1"])
def test_plain_body_decodes_spanish_and_strips_quoted_reply(charset):
    result = extract_excerpt(
        part("Hola Alex, tu pago venció.\nEl lunes escribió:\nOLD", charset=charset), 300
    )
    assert result["body_excerpt"] == "Hola Alex, tu pago venció."
    assert result["body_truncated"]
    assert result["body_status"] == "available"


@pytest.mark.parametrize(
    "tail",
    ["On Monday someone wrote:\nOLD", "-- \nSIGNATURE", "> OLD", "-----Original Message-----\nOLD"],
)
def test_plain_quote_and_signature_removal(tail):
    assert extract_excerpt(part("Please reply\n" + tail), 300)["body_excerpt"] == "Please reply"


def test_html_omits_markup_tracking_hidden_content_and_quotes():
    result = extract_excerpt(
        part(
            "<html><head><title>SECRET</title></head><body><style>SECRET</style>"
            "<script>SECRET</script><p>Hola&nbsp;AJ &amp; familia</p>"
            '<img src="https://tracking.example/SECRET"><div hidden>SECRET</div>'
            '<div style="display: none">SECRET</div>'
            '<blockquote><p>SECRET</p></blockquote><div class="gmail_signature">SECRET</div>'
            "<p>Hay un pago pendiente.</p></body></html>",
            "text/html",
        ),
        300,
    )
    assert result["body_excerpt"] == "Hola AJ & familia Hay un pago pendiente."
    assert result["body_truncated"]


def test_mime_prefers_plain_and_ignores_attachments_and_forwarded_messages():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    part("HTML_DUPLICATE", "text/html"),
                    part("Receipt paid"),
                ],
            },
            part("SECRET_ATTACHMENT", filename="invoice.txt"),
            part(
                "SECRET_ATTACHMENT",
                headers=[{"name": "Content-Disposition", "value": "attachment"}],
            ),
            part("SECRET_IMAGE", "image/png"),
            {"mimeType": "message/rfc822", "parts": [part("SECRET_FORWARDED")]},
        ],
    }
    assert extract_excerpt(payload, 300)["body_excerpt"] == "Receipt paid"


@pytest.mark.parametrize(
    "body", [{"data": "!invalid!"}, {"attachmentId": "SECRET", "size": 2000}, {}]
)
def test_unavailable_text_is_explicit_without_attachment_fetch(body):
    result = extract_excerpt({"mimeType": "text/plain", "body": body}, 300)
    assert result["body_excerpt"] == ""
    assert result["body_status"] == "unavailable"


def test_body_caps_words_and_characters():
    result = extract_excerpt(part("word " * 300 + "SECRET_TAIL"), 300)
    assert len(result["body_excerpt"].split()) == 300
    assert "SECRET" not in result["body_excerpt"]
    assert result["body_truncated"]
    result = extract_excerpt(part("x" * 5000), 300)
    assert len(result["body_excerpt"]) == 4000
    assert result["body_truncated"]


def test_gmail_full_body_keeps_only_excerpt_and_does_not_follow_references():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["format"] == "full"
        assert request.url.params["fields"] == "id,threadId,labelIds,payload"
        payload = part("hello " * 300 + "SECRET_TAIL")
        payload["headers"] += [
            {"name": "From", "value": "shop@example.com"},
            {"name": "Subject", "value": "Payment received"},
        ]
        return httpx.Response(
            200,
            json={
                "id": "a",
                "threadId": "b",
                "labelIds": ["INBOX"],
                "snippet": "SECRET_SNIPPET",
                "payload": payload,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        gmail = Gmail(SimpleNamespace(valid=True, token="fake"), lambda v: None, http)
        result = gmail.get_body("a", 300)
    assert len(requests) == 1
    assert "SECRET" not in repr(result)
    assert result.body_word_limit == 300
    assert result.sender == "shop@example.com"


def test_body_and_profile_allowlist_applies_to_target_and_examples():
    policy = Policy(
        body_words=3,
        profile={"full_name": "Alex Taylor", "name_variations": ["Alex Jordan Taylor", "AJ"]},
    )
    email = replace(message(), body_excerpt="one two three SECRET", body_status="available")
    example = {
        "sender": "shop@example.com",
        "subject": "Payment",
        "category": "ignore",
        "body_excerpt": email.body_excerpt,
        "body_status": "available",
        "body": "SECRET_RAW",
        "snippet": "SECRET_SNIPPET",
        "message_id": "SECRET_ID",
    }
    payload = build_payload(email, policy, [example])
    assert payload["state"]["profile"] == policy.profile
    assert payload["state"]["email"]["body_excerpt"] == "one two three"
    assert payload["state"]["email"]["body_truncated"]
    assert payload["state"]["examples"][0]["body_excerpt"] == "one two three"
    assert "SECRET" not in json.dumps(payload)
    assert "not proof" in payload["questions"]["category"]["instructions"]
    old = build_payload(email, Policy.from_snapshot({}), [example])
    assert set(old["state"]["email"]) == {"sender", "subject"}
    assert set(old["state"]["examples"][0]) == {"sender", "subject", "category"}
    assert "profile" not in old["state"]


def test_preview_caches_excerpt_and_metadata_sync_preserves_it(store):
    gmail = FakeGmail([message()])
    gmail.bodies["m1"] = "word " * 300 + "SECRET_TAIL"
    classifier = FakeJev()
    engine = Engine(store, gmail, classifier)
    learn(store, Policy(), activate=True)
    engine.sync()
    engine.preview()
    assert len(classifier.calls[0][0].body_excerpt.split()) == 300
    assert "SECRET" not in store.message("m1")["body_excerpt"]
    engine.sync()
    assert store.message("m1")["body_status"] == "available"
    learn(store, Policy(preferences="Use the same context"), activate=True)
    engine.preview(reclassify=True)
    assert gmail.body_fetches == ["m1"]
    assert len(classifier.calls) == 2
    engine.confirm("m1", "spam")
    _, snapshot = learn(store, Policy())
    saved = snapshot["train"] + snapshot["holdout"]
    assert len(saved[0]["body_excerpt"].split()) == 300
    assert saved == training_records(store, Policy())
    store.forget_feedback(["m1"])
    assert store.message("m1")["body_excerpt"] == ""
    assert store.rows("SELECT * FROM versions") == []


def test_header_mode_never_fetches_body_or_saves_it_in_examples(store):
    gmail = FakeGmail([message()])
    engine = Engine(store, gmail, FakeJev())
    learn(store, Policy(body_words=0), activate=True)
    engine.sync()
    engine.preview()
    assert gmail.body_fetches == []
    store.put_excerpt(
        replace(message(), body_excerpt="SECRET_BODY", body_status="available", body_word_limit=300)
    )
    engine.confirm("m1", "spam")
    _, snapshot = learn(store, Policy(body_words=0))
    assert "SECRET_BODY" not in json.dumps(snapshot)


def test_body_fetch_reconciles_new_correction_before_classifying(store):
    gmail = FakeGmail([message()])
    gmail.ensure_labels()
    classifier = FakeJev()
    engine = Engine(store, gmail, classifier)
    learn(store, Policy(), activate=True)
    engine.sync()
    gmail.edit("m1", ["INBOX", "label_important"])
    result = engine.preview()
    assert result["counts"]["skipped_edit"] == 1
    assert not classifier.calls
    assert store.message("m1")["override"] == "important"


def test_enrich_existing_corrections_is_bounded_and_reuses_cache(store):
    gmail = FakeGmail([message("a"), message("b")])
    gmail.bodies.update(a="Hola Alex, pago pendiente", b="Paid receipt")
    engine = Engine(store, gmail)
    engine.sync()
    engine.confirm("a", "important")
    engine.confirm("b", "ignore")
    assert engine.enrich_examples(Policy(), limit=1) == {"fetched": 1, "remaining": 1}
    assert engine.enrich_examples(Policy(), limit=1) == {"fetched": 1}
    assert engine.enrich_examples(Policy()) == {}
    _, snapshot = learn(store, Policy())
    assert all(e["body_status"] == "available" for e in snapshot["train"] + snapshot["holdout"])
    assert sorted(gmail.body_fetches) == ["a", "b"]


def test_evaluation_uses_body_for_candidate_and_headers_for_legacy_baseline(store):
    _, old = learn(store, Policy(body_words=0))
    old["policy"].pop("body_words")
    _, new = learn(store, Policy())
    sample = [
        {
            "message_id": "a",
            "thread_id": "a",
            "sender": "shop@example.com",
            "subject": "Payment",
            "category": "ignore",
            "body_excerpt": "Paid in full",
            "body_status": "available",
        }
    ]
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        report = evaluate(Jev("fake", http), {"new": new, "old": old}, sample)
    assert report["eligible"] == 1
    assert payloads[0]["state"]["email"]["body_excerpt"] == "Paid in full"
    assert set(payloads[1]["state"]["email"]) == {"sender", "subject"}


def test_v1_database_migration_preserves_existing_state_and_reopens(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO meta VALUES ('schema_version','1')")
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "a",
                "a",
                "sender",
                "subject",
                0,
                '["INBOX"]',
                '["label_ignore"]',
                "corrected",
                "ignore",
                "es",
                0,
                "date",
            ),
        )
        db.execute("INSERT INTO predictions VALUES (1,'a','old','{}','date')")
        db.execute("INSERT INTO feedback VALUES (1,'a','ignore','gmail',1,'date')")
    migrated = Store(path)
    assert migrated.get_meta("schema_version") == "2"
    assert migrated.message("a")["baseline"] == '["label_ignore"]'
    assert migrated.examples()[0]["category"] == "ignore"
    assert migrated.message("a")["body_status"] == "not_fetched"
    assert migrated.latest("a")["version_id"] == "old"
    migrated.close()
    reopened = Store(path)
    assert reopened.examples()[0]["category"] == "ignore"
    reopened.close()


def test_profile_configuration_overlays_existing_rules_and_thresholds(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text(
        'body_words = 300\n[profile]\nfull_name = "Alex Taylor"\n'
        'name_variations = ["Alex Jordan Taylor", "AJ"]\n'
    )
    base = Policy(min_probability=0.9, preferences="My preferences", body_words=0).data()
    policy = read_policy(path, base)
    assert policy.body_words == 300
    assert policy.min_probability == 0.9
    assert policy.preferences == "My preferences"
    assert policy.profile["name_variations"] == ["Alex Jordan Taylor", "AJ"]
    assert main(["--data-dir", str(tmp_path), "--config", str(path), "learn", "--activate"]) == 0
    database = Store(tmp_path / "state.sqlite3")
    assert database.version()[1]["policy"]["profile"] == policy.profile
    database.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"body_words": -1},
        {"body_words": 301},
        {"body_words": True},
        {"profile": {"email": "not allowed"}},
        {"profile": {"name_variations": "AJ"}},
    ],
)
def test_invalid_body_and_profile_settings_are_rejected(settings):
    with pytest.raises(AppError):
        Policy(**settings)


def test_cli_learning_enriches_existing_feedback_and_keeps_profile(tmp_path, monkeypatch, capsys):
    gmail = FakeGmail([message()])
    gmail.bodies["m1"] = "Hola Alex, pago recibido"
    gmail.close = lambda: None
    classifier = FakeJev()
    classifier.close = lambda: None
    monkeypatch.setattr(cli, "Secrets", lambda directory: SimpleNamespace(get=lambda name: "fake"))
    monkeypatch.setattr(cli, "credentials_from_json", lambda value: object())
    monkeypatch.setattr(cli, "Gmail", lambda credentials, save: gmail)
    monkeypatch.setattr(cli, "Jev", lambda key: classifier)
    database = Store(tmp_path / "state.sqlite3")
    engine = Engine(database, gmail)
    engine.sync()
    engine.confirm("m1", "ignore")
    learn(database, Policy(body_words=0, min_probability=0.9), activate=True)
    database.close()
    profile = tmp_path / "profile.toml"
    profile.write_text('body_words = 300\n[profile]\nfull_name = "Alex Taylor"\n')
    common = ["--data-dir", str(tmp_path)]
    assert main(common + ["--config", str(profile), "learn", "--activate"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["example_excerpts"] == {"fetched": 1}
    assert report["profile"]["full_name"] == "Alex Taylor"
    assert main(common + ["learn", "--activate"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["example_excerpts"] == {}
    assert report["profile"]["full_name"] == "Alex Taylor"
    assert main(common + ["preview"]) == 0
    assert not json.loads(capsys.readouterr().out)["learning_refresh_needed"]
    assert gmail.body_fetches == ["m1"]
    database = Store(tmp_path / "state.sqlite3")
    snapshot = database.version()[1]
    assert snapshot["policy"]["min_probability"] == 0.9
    assert (snapshot["train"] + snapshot["holdout"])[0]["body_excerpt"] == gmail.bodies["m1"]
    database.close()


def test_increasing_word_limit_refetches_excerpt(store):
    gmail = FakeGmail([message()])
    gmail.bodies["m1"] = "word " * 100
    engine = Engine(store, gmail, FakeJev())
    engine.sync()
    learn(store, Policy(body_words=10), activate=True)
    engine.preview()
    assert len(store.message("m1")["body_excerpt"].split()) == 10
    learn(store, Policy(body_words=300), activate=True)
    engine.preview(reclassify=True)
    assert len(store.message("m1")["body_excerpt"].split()) == 100
    assert gmail.body_fetches == ["m1", "m1"]
