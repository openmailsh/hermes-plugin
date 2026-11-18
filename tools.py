"""Native Hermes tools over the OpenMail REST API. The key stays in the plugin; the model never sees it."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
        if isinstance(exc.body, dict):
            code = exc.body.get("error") or exc.body.get("code")
            if isinstance(code, str) and code:
                payload["code"] = code
                draft_messages = {
                    "draft_changed": "The saved draft version no longer matches the version you reviewed. Do not send automatically. Call openmail_get_draft, show the current To/Cc/Bcc, subject and full body to the user, and obtain approval before sending with its updatedAt as updated_at. A failed send can also advance updatedAt without changing the content.",
                    "send_failed": "The send attempt failed and the draft is still saved. Its updatedAt may have advanced even though its content did not change. Do not retry automatically. Call openmail_get_draft, show the current To/Cc/Bcc, subject and full body to the user, and obtain approval to retry with its current updatedAt. Do not copy the draft into openmail_send.",
                    "draft_sending": "A send of this draft is still in progress or has already been accepted. Do not retry now, change recipients, or copy it into openmail_send. Wait and check the inbox for the sent message and openmail_get_draft for the saved draft before considering another send; a missing draft may already have been sent.",
                }
                payload["error"] = draft_messages.get(code, str(exc))
    return _out(payload)


def _default_inbox_id() -> Optional[str]:
    """The inbox tools act on when the model names none.

    The gateway adapter sets ``_inbox_id`` via ``bind()`` once it knows which
    inbox it runs as. Outside the gateway (``hermes chat``, cron, one-shot
    ``-q``) nothing calls ``bind()``, so fall back to ``OPENMAIL_INBOX_ID`` —
    the same value ``hermes openmail doctor`` reports as the inbox. Without
    this, a pod key that sees several inboxes made every send fail with
    "inbox_id is required" even though the user had configured one.
    """
    return _inbox_id or read_config().inbox_id


def _inbox(args: Dict[str, Any], api: OpenMailApi) -> str:
    inbox_id = str(args.get("inbox_id") or _default_inbox_id() or "").strip()
    if inbox_id:
        return inbox_id
    inboxes = api.list_inboxes()
    if len(inboxes) == 1:
        return str(inboxes[0]["id"])
    raise RuntimeError("inbox_id is required: the key can see several inboxes" if inboxes else "no inbox found")


_ATTACHMENT_DIRS = ("media", "output")  # under HERMES_HOME; the only places a model-supplied path may point


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _attachment_path(raw: str) -> str:
    """Confine model-supplied files to the Hermes media and output directories, resolving symlinks."""
    roots = [os.path.realpath(_hermes_home() / name) for name in _ATTACHMENT_DIRS]
    real = os.path.realpath(os.path.expanduser(raw))
    if not any(os.path.commonpath([real, root]) == root for root in roots):
        raise ValueError(f"attachment {raw!r} refused: attachments must live under " + " or ".join(roots))
    return real


def _attachments(paths: Any) -> Optional[List[str]]:
    return [_attachment_path(str(raw)) for raw in paths] if paths else None


def _addresses(raw: Any) -> Optional[List[str]]:
    """Flatten a model-supplied recipient list to ``["a@x.com", ...]``.

    The schema asks for an array of strings, but models (Nous/Hermes in
    particular) routinely emit ``{"item": "a@x.com"}``, ``[{"email": ...}]`` or
    ``"a@x.com, b@y.com"``. Hermes validates arguments against our schema before
    calling us, so ``_ADDRESS_ITEM`` admits those shapes and this collapses them.
    A rejected send here means the mail silently did not go out, so be lenient.
    """
    if raw is None or raw == "" or raw == []:
        return None
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: List[str] = []
    for item in items:
        if isinstance(item, dict):
            item = next((v for v in item.values() if isinstance(v, str) and v.strip()), "")
        for addr in str(item or "").replace(";", ",").split(","):
            addr = addr.strip()
            if addr:
                out.append(addr)
    return out or None


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
    return {"default_inbox_id": _default_inbox_id(), "inboxes": [
        {"id": i.get("id"), "address": i.get("address"), "podId": i.get("podId")} for i in inboxes]}


def openmail_send(args: Dict[str, Any], api: OpenMailApi) -> Any:
    to, subject, body = str(args.get("to") or "").strip(), str(args.get("subject") or "").strip(), str(args.get("body") or "")
    if not to or not subject or not body.strip():
        raise ValueError("to, subject and body are required")
    return api.send(inbox_id=_inbox(args, api), to=to, subject=subject, body=body, cc=_addresses(args.get("cc")),
                    bcc=_addresses(args.get("bcc")), attachments=_attachments(args.get("attachments")))


def openmail_create_draft(args: Dict[str, Any], api: OpenMailApi) -> Any:
    inbox_id = args.get("inbox_id")
    if not isinstance(inbox_id, str) or not inbox_id.strip():
        raise ValueError("inbox_id is required to create a draft")
    body = {key: args[key] for key in ("to", "subject", "body") if key in args}
    for name, field in (("body_html", "bodyHtml"), ("thread_id", "threadId"), ("source_thread_id", "sourceThreadId")):
        if name in args:
            body[field] = args[name]
    for key in ("cc", "bcc"):
        if key in args:
            body[key] = _addresses(args[key]) or []
    inline_images = [(image["content_id"], _attachment_path(image["path"])) for image in args.get("inline_images", [])]
    return api.create_draft(body, inbox_id, attachments=_attachments(args.get("attachments")), inline_images=inline_images)


def openmail_list_drafts(args: Dict[str, Any], api: OpenMailApi) -> Any:
    limit, offset = args.get("limit", 20), args.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError("limit must be 1–100 and offset must be nonnegative")
    return api.list_drafts(args.get("inbox_id"), pod_id=args.get("pod_id"), source_thread_id=args.get("source_thread_id"),
                           limit=limit, offset=offset, query=args.get("query"))


def _draft_id(args: Dict[str, Any]) -> str:
    draft_id = str(args.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id is required")
    return draft_id


def openmail_get_draft(args: Dict[str, Any], api: OpenMailApi) -> Any:
    draft_id = _draft_id(args)
    return api.get_draft(draft_id)


def openmail_send_draft(args: Dict[str, Any], api: OpenMailApi) -> Any:
    draft_id = _draft_id(args)
    updated_at = args.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ValueError("updated_at from the reviewed draft is required; read it with openmail_get_draft first")
    return api.send_draft(draft_id, updated_at, args.get("inbox_id"))


# (OpenMail thread id, nonce) of the gateway turn currently answering an inbound message.
# Set around handle_message; the processing task copies it, so tool calls and the final send agree.
_inbound_turn: contextvars.ContextVar[Optional[Tuple[str, int]]] = contextvars.ContextVar(
    "openmail_inbound_turn", default=None)
# thread id -> nonce of the turn that already answered it with openmail_reply. The adapter drops
# that turn's final text, so "reply via tool, then say 'Sent.'" does not email the sender twice.
# A reply on any other thread is not recorded: the next inbound there is a different message.
recent_tool_replies: Dict[str, int] = {}


def begin_inbound_turn(thread_id: str):
    """Bind this context to the gateway turn answering ``thread_id``. Reset with the returned token."""
    return _inbound_turn.set((thread_id, time.time_ns()))


def end_inbound_turn(token) -> None:
    _inbound_turn.reset(token)


def consume_tool_reply(thread_id: str) -> bool:
    """True when this turn already sent ``openmail_reply`` on ``thread_id``.

    A nonce from an earlier turn, or a marker for a thread this turn is not answering, is not a hit.
    Sends outside a turn (cron, a bare address) do not consume a marker that still belongs to one.
    """
    turn = _inbound_turn.get()
    nonce = recent_tool_replies.get(thread_id)
    if not turn or turn[0] != thread_id or nonce is None:
        return False
    recent_tool_replies.pop(thread_id, None)
    return nonce == turn[1]


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
                      cc=_addresses(args.get("cc")), bcc=_addresses(args.get("bcc")),
                      attachments=_attachments(args.get("attachments")),
                      include_quote=False if args.get("quote") is False else None)
    turn = _inbound_turn.get()
    if turn and turn[0] == thread_id:
        recent_tool_replies[thread_id] = turn[1]
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


# There is deliberately no key-minting tool. A `delegate_task` child runs in this process with this key and
# addresses its own inbox via `inbox_id`; a separate process (a Bot profile, another machine) gets its own
# inbox-scoped key from an operator running `hermes -p <bot> openmail setup --inbox <address>`. Either way a
# live token never has to pass through model context.


# ---- schemas ---------------------------------------------------------------------------------
def _schema(name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


_INBOX = {"type": "string", "description": "Inbox id. Defaults to the agent's own inbox."}
_ATTACH = {"type": "array", "items": {"type": "string"},
           "description": "Local file paths to attach; must be under ~/.hermes/media or ~/.hermes/output."}
_INLINE_IMAGES = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["path", "content_id"],
        "properties": {
            "path": {"type": "string", "description": "PNG, JPEG, GIF or WebP under the Hermes media or output directory."},
            "content_id": {"type": "string", "description": "inline-<lowercase UUID>@openmail, referenced as cid: in body_html."},
        },
    },
}
# Hermes validates tool arguments against this schema before our handler runs. Models often send
# `{"item": "a@x.com"}` for an array-of-strings, which Hermes wraps to `[{"item": ...}]` and then
# rejects — the mail never goes out. Admit that shape here; `_addresses()` flattens it.
_ADDRESS_ITEM = {"anyOf": [{"type": "string"}, {"type": "object", "additionalProperties": {"type": "string"}}]}
_CC = {"type": "array", "items": _ADDRESS_ITEM,
       "description": 'Extra recipients, a plain list of addresses: ["a@x.com", "b@y.com"].'}
_BCC = {"type": "array", "items": _ADDRESS_ITEM,
        "description": 'Blind copies, hidden from To and Cc, a plain list of addresses: ["crm@x.com"]. '
                       "For CRM logging addresses (HubSpot, Salesforce)."}

TOOLS: List[tuple[str, str, Dict[str, Any], Callable[[Dict[str, Any], OpenMailApi], Any]]] = [
    ("openmail_whoami", "Which OpenMail inboxes this agent can use and which one is its default.",
     _schema("openmail_whoami", "Which OpenMail inboxes this agent can use and which one is its default.", {}),
     openmail_whoami),
    ("openmail_send", "Start a new email thread from the agent's inbox.",
     _schema("openmail_send", "Start a new email thread. For answering mail you received, use openmail_reply instead.",
             {"to": {"type": "string", "description": "One recipient address."}, "subject": {"type": "string"},
              "body": {"type": "string", "description": "Plain-text body, sent verbatim."}, "cc": _CC, "bcc": _BCC,
              "attachments": _ATTACH, "inbox_id": _INBOX}, ["to", "subject", "body"]),
     openmail_send),
    ("openmail_create_draft", "Save an unsent email draft in its sending inbox.",
     _schema("openmail_create_draft", "Choose the inbox that will send this draft. All content is optional. "
             "Files and inline images share a 25 MB limit.",
             {"inbox_id": {"type": "string", "minLength": 1, "description": "ID of the inbox that owns and sends the draft."},
              "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
              "body_html": {"type": "string", "description": "Optional HTML body. Supports embedded and public raster images."},
              "cc": _CC, "bcc": _BCC, "attachments": _ATTACH,
              "inline_images": _INLINE_IMAGES,
              "thread_id": {"type": "string", "description": "Reply in this thread. The draft inherits its subject and source thread."},
              "source_thread_id": {"type": "string", "description": "Original conversation in this inbox. Use alone for a forward. Must match thread_id if both are supplied."}},
             ["inbox_id"]),
     openmail_create_draft),
    ("openmail_list_drafts", "Find saved, unsent email drafts.",
     _schema("openmail_list_drafts", "List drafts across accessible inboxes. Filters can be combined.",
             {"inbox_id": {"type": "string", "description": "Filter by the inbox that owns the draft."},
              "pod_id": {"type": "string", "description": "Filter by the sending inbox's pod."},
              "source_thread_id": {"type": "string", "description": "Filter by the conversation the draft was composed from."},
              "query": {"type": "string", "description": "Search subject, recipient and plain-text body."},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100},
              "offset": {"type": "integer", "minimum": 0}}),
     openmail_list_drafts),
    ("openmail_get_draft", "Read a saved draft and its exact updatedAt version.",
     _schema("openmail_get_draft", "Read the sending inbox, recipients, subject, body, attachments, thread context and updatedAt.",
             {"draft_id": {"type": "string", "minLength": 1}}, ["draft_id"]),
     openmail_get_draft),
    ("openmail_send_draft", "Send a reviewed draft from its owning inbox.",
     _schema("openmail_send_draft", "Send when instructed using the reviewed draft's exact updatedAt. "
             "A stale version must be reviewed again. Successful sending removes the draft.",
             {"draft_id": {"type": "string", "minLength": 1},
              "updated_at": {"type": "string", "format": "date-time", "description": "Exact updatedAt returned by openmail_get_draft."},
              "inbox_id": {"type": "string", "minLength": 1, "description": "Optional check. If supplied, must match the draft's owning inbox."}},
             ["draft_id", "updated_at"]),
     openmail_send_draft),
    ("openmail_reply", "Reply inside an existing email thread.",
     _schema("openmail_reply", "Reply in an existing thread. The subject and recipient come from the thread.",
             {"thread_id": {"type": "string"}, "body": {"type": "string", "description": "Plain-text body, sent verbatim."},
              "to": {"type": "string", "description": "Override the recipient (default: last inbound sender)."},
              "cc": _CC, "bcc": _BCC, "attachments": _ATTACH, "inbox_id": _INBOX,
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
     _schema("openmail_create_inbox", "Create a new inbox (needs a pod- or account-scoped key). Use for a new sender identity; "
             "a subagent in this process then targets it with `inbox_id` on send/reply/list, no separate key needed.",
             {"display_name": {"type": "string"}, "mailbox_name": {"type": "string", "description": "Local part, e.g. `sales` for sales@omail.sh."},
              "pod_id": {"type": "string"}}),
     openmail_create_inbox),
]


def register_tools(ctx: Any) -> None:
    for name, description, schema, fn in TOOLS:
        ctx.register_tool(name, TOOLSET, schema, _tool(fn), check_fn=configured,
                          requires_env=["OPENMAIL_API_KEY"], description=description, emoji="📬")
