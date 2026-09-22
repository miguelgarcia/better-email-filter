"""Extract bounded text locally; never render HTML or follow attachment/link references."""

from __future__ import annotations

import base64
import binascii
import re
from email.message import Message as MimeHeaders
from html.parser import HTMLParser

MAX_BODY_WORDS = 300
MAX_EXCERPT_CHARS = 4000
MAX_DECODE_BYTES = 256_000
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
BLOCK_TAGS = {"p", "div", "br", "hr", "li", "tr", "h1", "h2", "h3", "table", "section"}


def cap_excerpt(text: str, words: int) -> tuple[str, bool]:
    words = max(0, min(words, MAX_BODY_WORDS))
    tokens = text.split()
    excerpt = " ".join(tokens[:words])
    shortened = len(tokens) > words or len(excerpt) > MAX_EXCERPT_CHARS
    return excerpt[:MAX_EXCERPT_CHARS], shortened


class BodyHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.quoted_removed = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").lower().split())
        quote = tag == "blockquote" or bool(
            classes
            & {
                "gmail_quote",
                "yahoo_quoted",
                "moz-cite-prefix",
                "gmail_signature",
                "moz-signature",
            }
        )
        self.quoted_removed |= quote
        style = re.sub(r"\s+", "", attrs.get("style") or "").lower()
        blocked = (
            any(block for _, block in self.stack)
            or quote
            or tag in {"script", "style", "head", "template", "noscript"}
            or "hidden" in attrs
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if not blocked and tag in BLOCK_TAGS:
            self.text.append("\n")
        if tag not in VOID_TAGS:
            self.stack.append((tag, blocked))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in BLOCK_TAGS and not any(block for _, block in self.stack):
            self.text.append("\n")

    def handle_data(self, data):
        if not any(block for _, block in self.stack):
            self.text.append(data)


def strip_quoted(text):
    kept = []
    removed = False
    for line in text.splitlines():
        clean = line.strip()
        if (
            re.match(r"^(On .+ wrote:|El .+ escribi[oó]:)$", clean, re.IGNORECASE)
            or re.match(
                r"^-+\s*(Original Message|Mensaje original|Forwarded message)\s*-+$",
                clean,
                re.IGNORECASE,
            )
            or line == "-- "
        ):
            removed = True
            break
        if clean.startswith(">"):
            removed = True
            continue
        kept.append(line)
    return "\n".join(kept), removed


def extract_excerpt(payload: dict, words: int) -> dict:
    """Prefer plain text alternatives; omit file attachments and forwarded MIME messages."""
    visited = 0

    def visit(part, depth=0):
        nonlocal visited
        visited += 1
        if depth > 20 or visited > 1000:
            return "", True, False
        headers = {h.get("name", "").lower(): h.get("value", "") for h in part.get("headers", [])}
        disposition = headers.get("content-disposition", "").split(";", 1)[0].strip().lower()
        if part.get("filename") or disposition == "attachment":
            return "", False, False
        mime = part.get("mimeType", "").lower()
        if mime.startswith("multipart/"):
            results = [visit(child, depth + 1) for child in part.get("parts", [])]
            if mime == "multipart/alternative":
                # Gmail usually lists plain text first, but don't rely on that order.
                pairs = list(zip(part.get("parts", []), results, strict=True))
                pairs.sort(key=lambda pair: pair[0].get("mimeType") != "text/plain")
                return next(
                    (r for _, r in pairs if r[2] and r[0].strip()),
                    ("", any(r[1] for r in results), False),
                )
            return (
                "\n".join(r[0] for r in results if r[2]),
                any(r[1] for r in results),
                any(r[2] for r in results),
            )
        if mime not in {"text/plain", "text/html"}:
            return "", False, False
        body = part.get("body", {})
        data = body.get("data")
        # No attachments.get calls, even when Gmail stores a large text part externally.
        if not isinstance(data, str):
            return "", bool(body.get("size") or body.get("attachmentId")), False
        max_encoded = (MAX_DECODE_BYTES // 3) * 4
        shortened = len(data) > max_encoded
        encoded = data[:max_encoded]
        try:
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        except (ValueError, binascii.Error):
            return "", True, False
        content_type = MimeHeaders()
        content_type["content-type"] = headers.get("content-type", mime)
        charset = content_type.get_content_charset() or "utf-8"
        try:
            text = decoded.decode(charset, errors="replace")
        except LookupError:
            text = decoded.decode("utf-8", errors="replace")
            shortened = True
        shortened |= "\ufffd" in text
        if mime == "text/html":
            html = BodyHTML()
            html.feed(text)
            html.close()
            text = "".join(html.text)
            shortened |= html.quoted_removed
        text, quoted = strip_quoted(text)
        return text, shortened or quoted, True

    text, truncated, available = visit(payload)
    excerpt, capped = cap_excerpt(text, words)
    return {
        "body_excerpt": excerpt,
        "body_truncated": truncated or capped,
        "body_status": "available" if available and excerpt else "unavailable",
        "body_word_limit": words,
    }
