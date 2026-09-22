from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from oauthlib.oauth2 import OAuth2Error

from .body import extract_excerpt
from .domain import LABEL_NAMES, AppError, Message, RemoteError

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MESSAGE_FIELDS = "id,threadId,labelIds,payload/headers"

API_DISABLED = (
    "Enable Gmail API in the Google Cloud project that owns your Desktop OAuth client. "
    "If you just enabled it, allow a few minutes for the change to propagate, then retry."
)
MISSING_SCOPE = (
    "Gmail permission is missing. Run `auth gmail --client PATH` again and grant "
    "the requested Gmail access (gmail.modify)."
)
ERROR_HINTS = {
    "accessNotConfigured": API_DISABLED,
    "SERVICE_DISABLED": API_DISABLED,
    "insufficientPermissions": MISSING_SCOPE,
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": MISSING_SCOPE,
    "domainPolicy": "Your Google Workspace administrator must allow this app to access Gmail.",
    "dailyLimitExceeded": "The project's Gmail API daily quota was exceeded. Check its quotas.",
    "rateLimitExceeded": "Gmail API rate limit reached. Wait and retry.",
    "userRateLimitExceeded": "Gmail API per-user rate limit reached. Wait and retry.",
    "authError": "Gmail authorization is invalid. Run `auth gmail --client PATH` again.",
}


class GmailError(RemoteError):
    def __init__(self, status, reason=None):
        super().__init__("Gmail", status)
        # Only recognized codes and locally authored hints reach the terminal.
        self.reason = reason if reason in ERROR_HINTS else None
        if self.reason:
            self.args = (
                f"Gmail request failed ({status}, {self.reason}). " + ERROR_HINTS[self.reason],
            )
        elif status == 403:
            self.args = (
                "Gmail request failed (403). Google did not provide a recognized reason. "
                "Check that Gmail API is enabled in the OAuth client's project, that Gmail "
                "access was granted, and that your account's administrator permits this app.",
            )


def gmail_error(response):
    """Read legacy Gmail and modern Google ErrorInfo codes without echoing raw payloads."""
    try:
        raw = response.json()
    except ValueError:
        return GmailError(response.status_code)
    error = raw.get("error") if isinstance(raw, dict) else None
    if not isinstance(error, dict):
        return GmailError(response.status_code)
    for key in ("details", "errors"):
        entries = error.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            reason = entry.get("reason") if isinstance(entry, dict) else None
            if isinstance(reason, str) and reason in ERROR_HINTS:
                return GmailError(response.status_code, reason)
    return GmailError(response.status_code)


class Gmail:
    def __init__(self, credentials, save_credentials, client=None, sleep=time.sleep):
        self.credentials = credentials
        self.save_credentials = save_credentials
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.sleep = sleep

    def request(self, method, path, *, params=None, body=None, retry=True):
        for attempt in range(3 if retry else 1):
            try:
                if not self.credentials.valid:
                    self.credentials.refresh(Request())
                    self.save_credentials(self.credentials.to_json())
            except GoogleAuthError:
                raise AppError(
                    "Gmail authorization expired or failed. Run `auth gmail` again."
                ) from None
            try:
                response = self.client.request(
                    method,
                    BASE + path,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {self.credentials.token}"},
                )
            except httpx.TransportError:
                if not retry or attempt == 2:
                    raise RemoteError("Gmail", "network") from None
            else:
                if response.status_code in {200, 201, 204}:
                    try:
                        return response.json() if response.content else {}
                    except ValueError:
                        raise AppError("Gmail returned invalid JSON.") from None
                error = gmail_error(response)
                transient = response.status_code in {429, 500, 502, 503, 504} or (
                    response.status_code == 403
                    and error.reason in {"rateLimitExceeded", "userRateLimitExceeded"}
                )
                if not transient or not retry or attempt == 2:
                    raise error
            self.sleep(2**attempt)
        raise RemoteError("Gmail", "unavailable")

    def profile(self):
        return self.request("GET", "/profile", params={"fields": "emailAddress,historyId"})

    def labels(self):
        raw = self.request("GET", "/labels", params={"fields": "labels(id,name)"})
        by_name = {entry["name"]: entry["id"] for entry in raw.get("labels", [])}
        return {
            category: by_name[name] for category, name in LABEL_NAMES.items() if name in by_name
        }

    def ensure_labels(self):
        labels = self.labels()
        for category, name in LABEL_NAMES.items():
            if category not in labels:
                # If a create succeeds but its response is lost, the next run lists it.
                result = self.request(
                    "POST",
                    "/labels",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                    retry=False,
                )
                labels[category] = result["id"]
        return labels

    def get(self, mid):
        return Message.from_gmail(
            self.request(
                "GET",
                f"/messages/{mid}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject"],
                    "fields": MESSAGE_FIELDS,
                },
            )
        )

    def get_body(self, mid, words):
        if words == 0:
            return self.get(mid)
        raw = self.request(
            "GET",
            f"/messages/{mid}",
            params={
                "format": "full",
                "fields": "id,threadId,labelIds,payload",
            },
        )
        return replace(Message.from_gmail(raw), **extract_excerpt(raw.get("payload", {}), words))

    def inbox_ids(self, limit=200, query=""):
        ids = []
        token = None
        while limit is None or len(ids) < limit:
            params = {
                "labelIds": "INBOX",
                "maxResults": min(500, limit - len(ids)) if limit else 500,
                "fields": "messages/id,nextPageToken",
            }
            if query:
                params["q"] = query
            if token:
                params["pageToken"] = token
            raw = self.request("GET", "/messages", params=params)
            ids.extend(m["id"] for m in raw.get("messages", []))
            token = raw.get("nextPageToken")
            if not token:
                break
        return ids

    def history(self, checkpoint):
        ids = set()
        token = None
        while True:
            params = {
                "startHistoryId": checkpoint,
                "maxResults": 500,
                "fields": "history(messages/id),nextPageToken,historyId",
            }
            if token:
                params["pageToken"] = token
            raw = self.request("GET", "/history", params=params)
            for entry in raw.get("history", []):
                ids.update(m["id"] for m in entry.get("messages", []))
            token = raw.get("nextPageToken")
            if not token:
                return ids, raw["historyId"]

    def modify(self, mid, add, remove, allowed):
        if not (set(add) | set(remove)) <= set(allowed):
            raise AppError("Refusing to change a label outside BetterEmail's category labels.")
        # Do not retry writes blindly: an uncertain result is reconciled via the journal.
        self.request(
            "POST",
            f"/messages/{mid}/modify",
            body={
                "addLabelIds": sorted(add),
                "removeLabelIds": sorted(remove),
            },
            params={"fields": "id,labelIds"},
            retry=False,
        )

    def archive(self, mid):
        self.request(
            "POST",
            f"/messages/{mid}/modify",
            body={"removeLabelIds": ["INBOX"]},
            params={"fields": "id,labelIds"},
            retry=False,
        )

    def trash(self, mid):
        self.request(
            "POST",
            f"/messages/{mid}/trash",
            params={"fields": "id,labelIds"},
            retry=False,
        )

    def close(self):
        self.client.close()


def authorize(client_file: Path) -> Credentials:
    try:
        raw = json.loads(client_file.read_text())
        if "installed" not in raw:
            raise AppError("Use a Google OAuth client of type Desktop app.")
        flow = InstalledAppFlow.from_client_config(raw, [SCOPE])
        return flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            authorization_prompt_message="Complete Gmail authorization in your browser.",
            success_message=(
                "Authorization response received. You can close this window. "
                "Return to the terminal to check whether Gmail access was verified."
            ),
        )
    except (ValueError, GoogleAuthError, OAuth2Error):
        raise AppError(
            "Gmail authorization failed. Check the Desktop OAuth client configuration."
        ) from None


def credentials_from_json(value: str):
    try:
        credentials = Credentials.from_authorized_user_info(json.loads(value), [SCOPE])
        if not credentials.has_scopes([SCOPE]):
            raise ValueError("Missing scope")
        return credentials
    except (ValueError, TypeError):
        raise AppError("Saved Gmail credentials are invalid. Run `auth gmail` again.") from None
