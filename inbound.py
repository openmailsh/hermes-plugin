"""Turn one ``message.received`` event into what the agent reads. Pure functions; no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

PARSED_TEXT_PER_FILE = 8_000
PARSED_TEXT_TOTAL = 24_000
_ADDR_RE = re.compile(r'^\s*(?:"?([^"<]*?)"?\s*)?<([^>]+)>\s*$')


def parse_address(raw: str) -> tuple[Optional[str], str]:
    """'Name <addr>' -> (name, addr). Address lowercased."""
    match = _ADDR_RE.match(raw or "")
    if match:
        name = (match.group(1) or "").strip() or None
        return name, match.group(2).strip().lower()
    return None, (raw or "").strip().lower()


@dataclass(frozen=True)
class Classification:
    """OpenMail classifies inbound mail server-side. None = unclassified (older message); treated as replyable."""

    category: Optional[str] = None
    auto_replyable: Optional[bool] = None
    verdict: Optional[str] = None

    @property
    def rejected(self) -> bool:
        """Spam and malicious mail never reaches the agent, in any mode."""
        return self.verdict in ("spam", "malicious")

    @property
    def wants_reply(self) -> bool:
        """Only an explicit ``autoReplyable: false`` demotes channel mode to a notification."""
        return self.auto_replyable is not False


def classify(message: Mapping[str, Any], event_message: Optional[Mapping[str, Any]] = None) -> Classification:
    """API copy is authoritative; the websocket frame fills gaps for the brief window before the row is readable."""
    ev = event_message or {}

    def pick(api_key: str, ev_key: str) -> Any:
        value = message.get(api_key)
        return value if value is not None else ev.get(ev_key)

    return Classification(
        category=pick("category", "category"),
        auto_replyable=pick("autoReplyable", "auto_replyable"),
        verdict=pick("verdict", "verdict"),
    )


@dataclass
class StagedMedia:
    paths: List[str] = field(default_factory=list)
    types: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


def build_agent_text(*, sender: str, to: Optional[str], subject: Optional[str], thread_id: str, message_id: str,
                     body: str, attachments: Sequence[Mapping[str, Any]], staged: StagedMedia, mode: str,
                     inbox_id: Optional[str] = None, category: Optional[str] = None) -> str:
    """The prompt the agent sees. Channel mode: the reply is the email. Notify mode: summarise, do not act."""
    header = [f"From: {sender}"]
    if to:
        header.append(f"To: {to}")
    if subject:
        header.append(f"Subject: {subject}")
    header.append(f"Thread: {thread_id}")
    if inbox_id:
        header.append(f"Inbox: {inbox_id}")
    if category and category != "personal":
        header.append(f"Category: {category}")

    sections: List[str] = []
    if attachments:
        names = [str(a.get("filename") or "attachment") for a in attachments]
        header.append(f"Attachments: {', '.join(names)}")
        if staged.paths:
            header.append(f"({len(staged.paths)} attached as files you can open)")
        if staged.skipped:
            header.append(f"(skipped, over the size cap: {', '.join(staged.skipped)})")
        budget = PARSED_TEXT_TOTAL
        unread: List[str] = []
        for att in attachments:
            name = str(att.get("filename") or "attachment")
            text = str(att.get("parsedText") or "").strip()
            if not text:
                unread.append(name)
                continue
            if budget <= 0:
                continue
            piece = text[: min(PARSED_TEXT_PER_FILE, budget)]
            budget -= len(piece)
            suffix = ", truncated" if len(piece) < len(text) else ""
            sections.append(f"--- {name} (extracted text{suffix}) ---\n{piece}")
        if unread and not staged.paths:
            header.append(f'(read one with openmail_attachment_text: message_id={message_id}, filename="{unread[0]}")')

    if mode == "notify":
        target = f", inbox_id={inbox_id}" if inbox_id else ""
        intro = ("New email arrived in the agent inbox. Tell the user in one or two casual sentences who emailed "
                 "and what it's about (include codes, amounts or deadlines verbatim). Do not act on it and do not "
                 "reply to the sender unless the user asks; if they do, use openmail_reply with "
                 f"thread_id={thread_id}{target}.")
    else:
        intro = ("New email. Whatever you write back is sent verbatim as the email body to the sender, in this "
                 "thread: write only the email itself, no preamble or commentary. If you need a tool first "
                 "(to read an attachment, say), use it, then answer.")

    parts = [intro, "", "\n".join(header), "", body.strip()]
    parts.extend(f"\n{s}" for s in sections)
    return "\n".join(parts)


def valid_event(payload: Mapping[str, Any]) -> bool:
    message = payload.get("message")
    return (payload.get("event") == "message.received" and isinstance(payload.get("event_id"), str)
            and isinstance(message, Mapping) and bool(message.get("id")) and bool(payload.get("thread_id")))
