"""Native Hermes tools over the OpenMail REST API. The key stays in the plugin; the model never sees it."""

from __future__ import annotations

import time

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from .api import OpenMailApi, OpenMailApiError
from .config import read_config

logger = logging.getLogger(__name__)
TOOLSET = "openmail"

_api: Optional[OpenMailApi] = None
_inbox_id: Optional[str] = None  # the adapter's inbox, so tools default to it


def bind(api: OpenMailApi, inbox_id: Optional[str]) -> None:
    """Called by the adapter once it knows which inbox it runs as."""
    global _api, _inbox_id
    _api, _inbox_id = api, inbox_id


def _client() -> OpenMailApi:
    global _api
    if _api is None:
        cfg = read_config()
        if not cfg.configured:
            raise RuntimeError("OPENMAIL_API_KEY is not set")
        _api = OpenMailApi(cfg.base_url, cfg.api_key)
    return _api


def configured() -> bool:
    return _api is not None or read_config().configured


def _out(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _err(exc: Exception) -> str:
    payload: Dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, OpenMailApiError):
        payload["status"] = exc.status
        if isinstance(exc.body, dict) and exc.body.get("code"):
            payload["code"] = exc.body["code"]
    return _out(payload)


def _inbox(args: Dict[str, Any], api: OpenMailApi) -> str:
    inbox_id = str(args.get("inbox_id") or _inbox_id or "").strip()
    if inbox_id:
        return inbox_id
    inboxes = api.list_inboxes()
    if len(inboxes) == 1:
        return str(inboxes[0]["id"])
    raise RuntimeError("inbox_id is required: the key can see several inboxes" if inboxes else "no inbox found")


def _tool(fn: Callable[[Dict[str, Any], OpenMailApi], Any]) -> Callable[..., str]:
    def handler(args: Dict[str, Any], **_kwargs: Any) -> str:
        try:
            return _out(fn(args or {}, _client()))
        except Exception as exc:  # noqa: BLE001 — the model gets a structured error, never a traceback
            return _err(exc)

    handler.__name__ = fn.__name__
    return handler


# ---- handlers --------------------------------------------------------------------------------
def openmail_whoami(args: Dict[str, Any], api: OpenMailApi) -> Any:
    inboxes = api.list_inboxes()
    return {"default_inbox_id": _inbox_id, "inboxes": [
        {"id": i.get("id"), "address": i.get("address"), "podId": i.get("podId")} for i in inboxes]}


def openmail_send(args: Dict[str, Any], api: OpenMailApi) -> Any:
    to, subject, body = str(args.get("to") or "").strip(), str(args.get("subject") or "").strip(), str(args.get("body") or "")
    if not to or not subject or not body.strip():
        raise ValueError("to, subject and body are required")
    return api.send(inbox_id=_inbox(args, api), to=to, subject=subject, body=body, cc=args.get("cc") or None,
                    attachments=args.get("attachments") or None)


# thread_id -> time the agent answered it with openmail_reply. The adapter drops the turn's final text for a
# thread on this list, so "reply via tool, then say 'Sent.'" does not email the sender twice.
recent_tool_replies: Dict[str, float] = {}


def openmail_reply(args: Dict[str, Any], api: OpenMailApi) -> Any:
    thread_id, body = str(args.get("thread_id") or "").strip(), str(args.get("body") or "")
    if not thread_id or not body.strip():
        raise ValueError("thread_id and body are required")
    to = str(args.get("to") or "").strip()
    inbox_id = str(args.get("inbox_id") or "").strip()
    if not to or not inbox_id:
        # Fill in from the thread: answer the last inbound sender, from the inbox that received it.
        for message in reversed(api.thread_messages(thread_id)):
            if message.get("direction") == "inbound":
                to = to or str(message.get("fromAddr") or "")
                inbox_id = inbox_id or str(message.get("inboxId") or "")
                break
        if not to:
            raise ValueError("cannot tell whom to answer; pass `to`")
    result = api.send(inbox_id=inbox_id or _inbox(args, api), to=to, body=body, thread_id=thread_id,
                      cc=args.get("cc") or None, attachments=args.get("attachments") or None,
                      include_quote=False if args.get("quote") is False else None)
    recent_tool_replies[thread_id] = time.time()
    return result


def openmail_list_threads(args: Dict[str, Any], api: OpenMailApi) -> Any:
    return api.list_threads(_inbox(args, api), limit=args.get("limit") or 20, offset=args.get("offset"),
                            is_read=args.get("is_read"))


def openmail_read_thread(args: Dict[str, Any], api: OpenMailApi) -> Any:
    thread_id = str(args.get("thread_id") or "").strip()
    if not thread_id:
        raise ValueError("thread_id is required")
    messages = api.thread_messages(thread_id)
    if args.get("mark_read", True):
        try:
            api.mark_thread(thread_id, is_read=True)
        except OpenMailApiError as exc:
            logger.debug("[OpenMail] mark read failed: %s", exc)
    return {"thread_id": thread_id, "messages": messages}


def openmail_list_messages(args: Dict[str, Any], api: OpenMailApi) -> Any:
    return api.list_messages(_inbox(args, api), direction=args.get("direction"), limit=args.get("limit") or 20,
                             offset=args.get("offset"))


def openmail_attachment_text(args: Dict[str, Any], api: OpenMailApi) -> Any:
    message_id, filename = str(args.get("message_id") or "").strip(), str(args.get("filename") or "").strip()
    if not message_id or not filename:
        raise ValueError("message_id and filename are required")
    return api.attachment_text(message_id, filename)


def openmail_list_inboxes(args: Dict[str, Any], api: OpenMailApi) -> Any:
    return api.list_inboxes()


def openmail_create_inbox(args: Dict[str, Any], api: OpenMailApi) -> Any:
    return api.create_inbox(display_name=args.get("display_name"), mailbox_name=args.get("mailbox_name"),
                            pod_id=args.get("pod_id"))


def openmail_create_inbox_key(args: Dict[str, Any], api: OpenMailApi) -> Any:
    inbox_id = str(args.get("inbox_id") or "").strip()
    if not inbox_id:
        raise ValueError("inbox_id is required")
    return api.create_inbox_key(inbox_id, str(args.get("name") or "hermes"))


# ---- schemas ---------------------------------------------------------------------------------
def _schema(name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


_INBOX = {"type": "string", "description": "Inbox id. Defaults to the agent's own inbox."}
_ATTACH = {"type": "array", "items": {"type": "string"}, "description": "Local file paths to attach."}
_CC = {"type": "array", "items": {"type": "string"}, "description": "Extra recipients."}

TOOLS: List[tuple[str, str, Dict[str, Any], Callable[[Dict[str, Any], OpenMailApi], Any]]] = [
    ("openmail_whoami", "Which OpenMail inboxes this agent can use and which one is its default.",
     _schema("openmail_whoami", "Which OpenMail inboxes this agent can use and which one is its default.", {}),
     openmail_whoami),
    ("openmail_send", "Start a new email thread from the agent's inbox.",
     _schema("openmail_send", "Start a new email thread. For answering mail you received, use openmail_reply instead.",
             {"to": {"type": "string", "description": "One recipient address."}, "subject": {"type": "string"},
              "body": {"type": "string", "description": "Plain-text body, sent verbatim."}, "cc": _CC,
              "attachments": _ATTACH, "inbox_id": _INBOX}, ["to", "subject", "body"]),
     openmail_send),
    ("openmail_reply", "Reply inside an existing email thread.",
     _schema("openmail_reply", "Reply in an existing thread. The subject and recipient come from the thread.",
             {"thread_id": {"type": "string"}, "body": {"type": "string", "description": "Plain-text body, sent verbatim."},
              "to": {"type": "string", "description": "Override the recipient (default: last inbound sender)."},
              "cc": _CC, "attachments": _ATTACH, "inbox_id": _INBOX,
              "quote": {"type": "boolean", "description": "false to omit the quoted previous message."}},
             ["thread_id", "body"]),
     openmail_reply),
    ("openmail_list_threads", "List email threads in an inbox, newest first.",
     _schema("openmail_list_threads", "List threads in an inbox, newest first.",
             {"inbox_id": _INBOX, "limit": {"type": "integer"}, "offset": {"type": "integer"},
              "is_read": {"type": "boolean", "description": "Filter by read state."}}),
     openmail_list_threads),
    ("openmail_read_thread", "Read every message in a thread.",
     _schema("openmail_read_thread", "Read every message in a thread, oldest first, and mark it read.",
             {"thread_id": {"type": "string"}, "mark_read": {"type": "boolean", "description": "Default true."}},
             ["thread_id"]),
     openmail_read_thread),
    ("openmail_list_messages", "List individual messages in an inbox.",
     _schema("openmail_list_messages", "List messages in an inbox, newest first.",
             {"inbox_id": _INBOX, "direction": {"type": "string", "enum": ["inbound", "outbound"]},
              "limit": {"type": "integer"}, "offset": {"type": "integer"}}),
     openmail_list_messages),
    ("openmail_attachment_text", "Extract text from an email attachment.",
     _schema("openmail_attachment_text", "Plain text extracted server-side from an attachment (PDF, DOCX, XLSX, images via OCR).",
             {"message_id": {"type": "string"}, "filename": {"type": "string"}}, ["message_id", "filename"]),
     openmail_attachment_text),
    ("openmail_list_inboxes", "List inboxes the key can see.",
     _schema("openmail_list_inboxes", "List inboxes visible to this key.", {}), openmail_list_inboxes),
    ("openmail_create_inbox", "Create a new inbox.",
     _schema("openmail_create_inbox", "Create a new inbox (needs a pod- or account-scoped key). Use for a subagent or a new sender identity.",
             {"display_name": {"type": "string"}, "mailbox_name": {"type": "string", "description": "Local part, e.g. `sales` for sales@omail.sh."},
              "pod_id": {"type": "string"}}),
     openmail_create_inbox),
    ("openmail_create_inbox_key", "Mint an inbox-scoped API key.",
     _schema("openmail_create_inbox_key", "Mint an API key that can only use one inbox. Hand it to a subagent; never log it.",
             {"inbox_id": {"type": "string"}, "name": {"type": "string"}}, ["inbox_id"]),
     openmail_create_inbox_key),
]


def register_tools(ctx: Any) -> None:
    for name, description, schema, fn in TOOLS:
        ctx.register_tool(name, TOOLSET, schema, _tool(fn), check_fn=configured,
                          requires_env=["OPENMAIL_API_KEY"], description=description, emoji="📬")
