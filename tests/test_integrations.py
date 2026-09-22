import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from conftest import message

from better_email.domain import CATEGORIES, AppError, Message, Policy, RemoteError, select_examples
from better_email.engine import learn
from better_email.gmail import Gmail
from better_email.jev import ENDPOINT, Jev, build_payload, parse_response


def response(**changes):
    value = {
        "model": "jev-1.13.0",
        "answers": {
            "category": {
                "type": "choice",
                "choice": "spam",
                "confidence": 0.90,
                "probabilities": {"spam": 0.95, "ignore": 0.03, "important": 0.02},
            },
            "sufficient_evidence": {"type": "noul", "noul": 0.95},
        },
        "usage": {"input_tokens": 300, "output_tokens": 20},
    }
    value.update(changes)
    return value


def test_jev_http_contract_and_allowlist_for_examples(store):
    raw_example = {
        "message_id": "SECRET_ID",
        "thread_id": "SECRET_THREAD",
        "sender": "offers@example.com",
        "subject": "Oferta 30%",
        "category": "spam",
        "snippet": "SECRET_SNIPPET",
        "body": "SECRET_BODY",
        "language": "SECRET_LANG",
    }
    _, snapshot = learn(store, Policy(body_words=0))
    snapshot["train"] = [raw_example]
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == ENDPOINT
        assert request.headers["Authorization"] == "Bearer fake-key"
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert set(payload) == {"model", "state", "questions"}
        assert payload["state"]["email"] == message().content()
        assert set(payload["state"]["examples"][0]) == {"sender", "subject", "category"}
        assert "SECRET" not in request.content.decode()
        return httpx.Response(200, json=response())

    client = Jev("fake-key", httpx.Client(transport=httpx.MockTransport(handler)))
    decision = client.classify(message(), snapshot)
    assert decision.category == "spam"
    assert decision.model == "jev-1.13.0"
    assert decision.example_ids == ["SECRET_ID"]  # IDs stay local, for audit.
    assert len(requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["answers"]["category"].update(choice="other"),
        lambda r: r["answers"]["category"].update(confidence=float("nan")),
        lambda r: r["answers"]["category"].update(confidence=True),
        lambda r: r["answers"]["category"]["probabilities"].update(spam=0.1),
        lambda r: r["answers"]["category"].update(choice="important"),
        lambda r: r.update(model="SECRET\ntext"),
        lambda r: r.update(usage={"input_tokens": -1}),
        lambda r: r["answers"].pop("sufficient_evidence"),
    ],
)
def test_invalid_jev_responses_fail_closed_without_echoing_payload(change):
    raw = response()
    change(raw)
    with pytest.raises(AppError, match="invalid classification response") as error:
        parse_response(raw, Policy())
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("field,value", [("confidence", 0.2), ("evidence", 0.2)])
def test_uncertainty_routes_to_review(field, value):
    raw = response()
    if field == "confidence":
        raw["answers"]["category"][field] = value
    else:
        raw["answers"]["sufficient_evidence"]["noul"] = value
    assert parse_response(raw, Policy(evidence_gate=True)).review


def test_confident_category_is_not_blocked_by_optional_evidence_score():
    raw = response()
    raw["answers"]["category"].update(
        confidence=0.98, probabilities={"spam": 0.99, "ignore": 0.01, "important": 0.0}
    )
    raw["answers"]["sufficient_evidence"]["noul"] = 0.42
    decision = parse_response(raw, Policy())
    assert decision.category == "spam"
    assert not decision.review
    assert decision.evidence == 0.42
    assert decision.review_reasons == []
    strict = parse_response(raw, Policy(evidence_gate=True))
    assert strict.review
    assert strict.review_reasons == ["insufficient_evidence"]


def test_low_category_scores_still_require_review_without_evidence_gate():
    raw = response()
    raw["answers"]["category"].update(
        choice="important",
        confidence=0.64,
        probabilities={"spam": 0.0, "ignore": 0.24, "important": 0.76},
    )
    raw["answers"]["sufficient_evidence"]["noul"] = 0.51
    decision = parse_response(raw, Policy())
    assert decision.review
    assert decision.review_reasons == ["low_category_probability", "low_category_confidence"]


def test_old_snapshot_keeps_its_evidence_gate_until_new_version_is_learned(store):
    _, snapshot = learn(store, Policy())
    snapshot["policy"].pop("evidence_gate")
    raw = response()
    raw["answers"]["sufficient_evidence"]["noul"] = 0.42
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw))
    ) as http:
        client = Jev("fake", http)
        assert client.classify(message(), snapshot).review
        _, upgraded = learn(store, Policy(**snapshot["policy"]))
        assert not client.classify(message(), upgraded).review
        assert client.classify(message(), snapshot).review  # Rollback semantics unchanged.


def test_incomplete_metadata_does_not_call_jev(store):
    _, snapshot = learn(store, Policy())
    client = Jev(
        "unused",
        httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("No request expected"))
        ),
    )
    for m in (replace(message(), subject=""), replace(message(), truncated=True)):
        decision = client.classify(m, snapshot)
        assert decision.review
        assert decision.reason == "incomplete_metadata"


def test_bounded_jev_retry_and_error_body_not_exposed(store):
    _, snapshot = learn(store, Policy())
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="SECRET_REMOTE_ERROR")

    client = Jev(
        "unused", httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append
    )
    with pytest.raises(RemoteError) as error:
        client.classify(message(), snapshot)
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert "SECRET" not in str(error.value)


def test_gmail_get_requests_metadata_only_and_rejects_extra_content():
    def handler(request):
        params = request.url.params
        assert params["format"] == "metadata"
        assert params.get_list("metadataHeaders") == ["From", "Subject"]
        assert params["fields"] == "id,threadId,labelIds,payload/headers"
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "snippet": "SECRET",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "sender@example.com"},
                        {"name": "Subject", "value": "=?utf-8?b?UGFnbyByZWNpYmlkbw==?="},
                    ],
                    "body": {"data": "SECRET"},
                },
            },
        )

    client = Gmail(
        SimpleNamespace(valid=True, token="fake"),
        lambda v: None,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    m = client.get("m1")
    assert m.subject == "Pago recibido"
    assert "SECRET" not in repr(m)


def test_gmail_list_pagination_respects_cap_and_inbox():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["labelIds"] == "INBOX"
        if "pageToken" not in request.url.params:
            assert request.url.params["maxResults"] == "3"
            return httpx.Response(
                200, json={"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "next"}
            )
        assert request.url.params["maxResults"] == "1"
        return httpx.Response(200, json={"messages": [{"id": "c"}]})

    client = Gmail(
        SimpleNamespace(valid=True, token="fake"),
        lambda v: None,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.inbox_ids(3) == ["a", "b", "c"]
    assert len(requests) == 2


def test_gmail_history_pagination_uses_final_checkpoint():
    def handler(request):
        assert request.url.params["startHistoryId"] == "100"
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "history": [{"messages": [{"id": "a"}]}],
                    "historyId": "200",
                    "nextPageToken": "next",
                },
            )
        return httpx.Response(
            200, json={"history": [{"messages": [{"id": "b"}, {"id": "a"}]}], "historyId": "201"}
        )

    client = Gmail(
        SimpleNamespace(valid=True, token="fake"),
        lambda v: None,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.history("100") == ({"a", "b"}, "201")


def test_gmail_mutation_guard_and_no_blind_retry():
    calls = []

    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload == {"addLabelIds": ["label_spam"], "removeLabelIds": []}
        return httpx.Response(503, text="SECRET")

    client = Gmail(
        SimpleNamespace(valid=True, token="fake"),
        lambda v: None,
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(AppError, match="outside"):
        client.modify("m1", {"SPAM"}, {"INBOX"}, {"label_spam"})
    assert not calls
    with pytest.raises(RemoteError):
        client.modify("m1", {"label_spam"}, set(), {"label_spam"})
    assert len(calls) == 1


def test_examples_exclude_same_message_thread_and_irrelevant_content():
    m = message()
    records = [
        {"message_id": "m1", "thread_id": "m1", "sender": m.sender, "subject": m.subject},
        {"message_id": "m2", "thread_id": "m1", "sender": m.sender, "subject": m.subject},
        {"message_id": "m3", "thread_id": "m3", "sender": "x@elsewhere.test", "subject": "hello"},
        {"message_id": "m4", "thread_id": "m4", "sender": m.sender, "subject": "Oferta 20%"},
    ]
    assert [e["message_id"] for e in select_examples(m, records, 6)] == ["m4"]


def test_rules_require_subject_match_and_conflicts_review(store):
    rules = [{"sender": "offers@example.com", "subject_contains": "oferta", "category": "spam"}]
    _, snapshot = learn(store, Policy(rules=rules))
    client = Jev(
        "unused",
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response()))
        ),
    )
    assert client.classify(message(), snapshot).reason == "explicit_rule"
    assert client.classify(message(subject="Payment failed"), snapshot).reason == "classifier"
    rules.append(dict(rules[0], category="important"))
    _, snapshot = learn(store, Policy(rules=rules))
    assert client.classify(message(), snapshot).reason == "rule_conflict"


def test_subject_header_length_is_bounded():
    m = Message.from_gmail(
        {
            "id": "m1",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "a" * 1200},
                    {"name": "From", "value": "a@test.com"},
                ]
            },
        }
    )
    assert len(m.subject) == 1000
    assert m.truncated


def test_classification_policy_covers_user_preferences():
    payload = build_payload(message(), Policy(), [])
    instructions = payload["questions"]["category"]["instructions"]
    assert "Spanish and English" in instructions
    assert "All promotions" in instructions
    assert "misdirected" in instructions
    assert set(payload["questions"]["category"]["criteria"]) == set(CATEGORIES)


def test_exclusion_preference_reaches_both_questions_and_saved_versions(store):
    preference = (
        "Mail about services I explicitly exclude is spam. "
        "Example Insurance is excluded, even for genuine requests with deadlines. "
        "Do not infer country from language or a generic .com domain."
    )
    baseline_id, baseline = learn(store, Policy())
    candidate_id, candidate = learn(store, Policy(preferences=preference), activate=True)
    email = replace(
        message(subject="Apertura de folio"),
        body_excerpt="Example Insurance. Hola Alex, responde dentro de 72 horas.",
        body_status="available",
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        Jev("fake", http).classify(email, candidate)
    for question in requests[0]["questions"].values():
        assert preference in question["instructions"]
        assert "Explicit user preferences take precedence" in question["instructions"]
        assert "Do not invent missing facts" in question["instructions"]
    assert candidate_id != baseline_id
    assert store.version()[1]["policy"]["preferences"] == preference
    assert store.version(baseline_id)[1] == baseline
    assert all(preference not in q["instructions"] for q in baseline["questions"].values())
