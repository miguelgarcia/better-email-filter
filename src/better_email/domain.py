from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Any

from .body import MAX_BODY_WORDS, cap_excerpt

CATEGORIES = ("spam", "ignore", "important")
LABEL_NAMES = {value: f"BetterEmail/{value.title()}" for value in (*CATEGORIES, "review")}
MAX_FIELD = 1000


class AppError(Exception):
    """User-facing error containing no remote payloads or credentials."""


class RemoteError(AppError):
    def __init__(self, service: str, status: int | str):
        self.status = status
        super().__init__(f"{service} request failed ({status}).")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:16]


def clean_header(value: str) -> tuple[str, bool]:
    try:
        value = str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        pass
    value = " ".join(value.split())
    return value[:MAX_FIELD], len(value) > MAX_FIELD


@dataclass(frozen=True)
class Message:
    id: str
    thread_id: str
    sender: str
    subject: str
    labels: frozenset[str] = frozenset()
    truncated: bool = False
    body_excerpt: str = ""
    body_truncated: bool = False
    body_status: str = "not_fetched"
    body_word_limit: int = 0

    @classmethod
    def from_gmail(cls, raw: dict) -> Message:
        # Deliberate allowlist. Never retain or serialize the raw API response.
        headers = {}
        for h in raw.get("payload", {}).get("headers", []):
            name = h.get("name", "").lower()
            if name in {"from", "subject"}:
                headers[name] = str(h.get("value", ""))
        sender, short_sender = clean_header(headers.get("from", ""))
        subject, short_subject = clean_header(headers.get("subject", ""))
        return cls(
            str(raw["id"]),
            str(raw.get("threadId", raw["id"])),
            sender,
            subject,
            frozenset(raw.get("labelIds", [])),
            short_sender or short_subject,
        )

    def content(self, body_words=0) -> dict:
        content = {"sender": self.sender, "subject": self.subject}
        if body_words:
            excerpt, capped = cap_excerpt(self.body_excerpt, body_words)
            content.update(
                body_excerpt=excerpt,
                body_truncated=self.body_truncated or capped,
                body_status=self.body_status,
            )
        return content


@dataclass
class Policy:
    model: str = "jev-latest"
    max_examples: int = 6
    min_probability: float = 0.85
    min_confidence: float = 0.70
    min_evidence: float = 0.80
    evidence_gate: bool = False
    retention_days: int = 0
    preferences: str = ""
    rules: list[dict[str, str]] = field(default_factory=list)
    body_words: int = 300
    profile: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.body_words) is not int or not 0 <= self.body_words <= MAX_BODY_WORDS:
            raise AppError(f"body_words must be an integer between 0 and {MAX_BODY_WORDS}.")
        if not isinstance(self.profile, dict) or set(self.profile) - {
            "full_name",
            "name_variations",
            "company_names",
        }:
            raise AppError("profile supports full_name, name_variations, and company_names.")
        name = self.profile.get("full_name", "")
        if not isinstance(name, str) or len(name) > 200:
            raise AppError("profile.full_name must be text of at most 200 characters.")
        for key in ("name_variations", "company_names"):
            values = self.profile.get(key, [])
            if (
                not isinstance(values, list)
                or len(values) > 20
                or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values)
            ):
                raise AppError(f"profile.{key} must contain up to 20 nonempty short strings.")
        if type(self.evidence_gate) is not bool:
            raise AppError("evidence_gate must be true or false.")
        if not isinstance(self.model, str) or not self.model:
            raise AppError("model must be a nonempty string.")
        if type(self.max_examples) is not int or not 0 <= self.max_examples <= 20:
            raise AppError("max_examples must be an integer between 0 and 20.")
        if type(self.retention_days) is not int or self.retention_days < 0:
            raise AppError("retention_days must be a nonnegative integer.")
        for name in ("min_probability", "min_confidence", "min_evidence"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 <= value <= 1:
                raise AppError(f"{name} must be between 0 and 1.")
        if not isinstance(self.preferences, str) or len(self.preferences) > 4000:
            raise AppError("preferences must be text of at most 4000 characters.")
        for rule in self.rules:
            if set(rule) != {"sender", "subject_contains", "category"}:
                raise AppError("Rules require sender, subject_contains, and category.")
            if rule["category"] not in CATEGORIES or not rule["sender"]:
                raise AppError("Rules require a sender and a valid category.")
            if not all(isinstance(v, str) for v in rule.values()):
                raise AppError("Rule fields must be strings.")

    def data(self):
        return asdict(self)

    @classmethod
    def from_snapshot(cls, data: dict) -> Policy:
        # Previously saved versions always gated on evidence. Preserve their meaning
        # for evaluation/rollback; newly learned versions use the current defaults.
        return cls(**{"evidence_gate": True, "body_words": 0, **data})


@dataclass
class Decision:
    category: str
    probabilities: dict[str, float]
    confidence: float
    evidence: float
    review: bool
    reason: str
    model: str
    example_ids: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    review_reasons: list[str] = field(default_factory=list)


def template_key(example: dict) -> str:
    subject = example["subject"].casefold()
    subject = re.sub(r"^(?:(?:re|fw|fwd):\s*)+", "", subject)
    subject = re.sub(r"\d+", "#", subject)
    return digest([parseaddr(example["sender"])[1].casefold(), subject])


def grouped(records: list[dict]) -> list[list[dict]]:
    """Connected groups: shared thread OR normalized sender/subject template."""
    parents = list(range(len(records)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    seen = {}
    for i, record in enumerate(records):
        for key in (("thread", record["thread_id"]), ("template", template_key(record))):
            if key in seen:
                parents[root(i)] = root(seen[key])
            seen[key] = i
    groups = {}
    for i, record in enumerate(records):
        groups.setdefault(root(i), []).append(record)
    return list(groups.values())


def split_examples(records: list[dict]) -> tuple[list[dict], list[dict]]:
    train, holdout = [], []
    for group in grouped(records):
        key = min(template_key(r) for r in group)
        destination = holdout if int(key, 16) % 5 == 0 else train
        destination.extend(group)
    return train, holdout


def select_examples(message: Message, examples: list[dict], limit: int) -> list[dict]:
    tokens = set(re.findall(r"\w+", message.subject.casefold()))
    sender = parseaddr(message.sender)[1].casefold()

    def score(example):
        other = set(re.findall(r"\w+", example["subject"].casefold()))
        overlap = len(tokens & other) / max(1, len(tokens | other))
        return overlap + (parseaddr(example["sender"])[1].casefold() == sender)

    candidates = [
        e
        for e in examples
        if e["message_id"] != message.id and e["thread_id"] != message.thread_id and score(e) > 0
    ]
    candidates.sort(key=lambda e: (-score(e), e["message_id"]))
    return candidates[:limit]
