from types import SimpleNamespace

import httpx
import pytest

from better_email.gmail import Gmail, GmailError


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"error": {"details": [{"reason": "SERVICE_DISABLED"}]}}, "Enable Gmail API"),
        ({"error": {"errors": [{"reason": "accessNotConfigured"}]}}, "Enable Gmail API"),
        ({"error": {"errors": [{"reason": "insufficientPermissions"}]}}, "gmail.modify"),
        ({"error": {"errors": [{"reason": "domainPolicy"}]}}, "administrator"),
        ({"error": {"errors": [{"reason": "SECRET"}]}}, "recognized reason"),
        ({"error": {"details": {"unexpected": "SECRET"}}}, "recognized reason"),
    ],
)
def test_permission_errors_explain_known_reasons_without_exposing_payloads(body, expected):
    body["error"]["message"] = "SECRET private response details"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        gmail = Gmail(SimpleNamespace(valid=True, token="fake"), lambda _: None, http)
        with pytest.raises(GmailError) as exc:
            gmail.profile()
    assert expected in str(exc.value)
    assert "SECRET" not in str(exc.value)
    assert exc.value.status == 403
    assert len(calls) == 1


@pytest.mark.parametrize("write,attempts", [(False, 3), (True, 1)])
def test_rate_limit_retries_reads_but_not_uncertain_label_writes(write, attempts):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            403,
            json={
                "error": {"errors": [{"reason": "userRateLimitExceeded"}]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        gmail = Gmail(
            SimpleNamespace(valid=True, token="fake"), lambda _: None, http, sleep=lambda _: None
        )
        with pytest.raises(GmailError):
            if write:
                gmail.modify("m1", {"label_spam"}, set(), {"label_spam"})
            else:
                gmail.profile()
    assert len(calls) == attempts
