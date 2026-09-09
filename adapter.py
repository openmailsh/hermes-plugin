"""OpenMail platform adapter: inbound mail over a websocket wakes the agent; ``send`` answers in-thread."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (BasePlatformAdapter, MessageEvent, MessageType, SendResult,
                                    cache_document_from_bytes, cache_image_from_bytes)

from .api import AttachmentTooLargeError, OpenMailApi, OpenMailApiError
from .config import OpenMailConfig, allow_all_senders, allowed_senders, read_config
from .inbound import StagedMedia, build_agent_text, classify, parse_address
from .stream import Cursor, FatalStreamError, run_stream, ws_url
from . import tools as _tools

logger = logging.getLogger(__name__)

PLATFORM_NAME = "openmail"
DEFAULT_SUBJECT = "Message from your agent"
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
# OpenMail rejects mail over 25 MB, so nothing legitimate exceeds this; it only stops a misbehaving
# server from streaming unbounded bytes into memory.
_DOWNLOAD_CEILING = 25 * 1024 * 1024
# Hermes sends these to the chat on first contact. On other platforms that is the operator; here it is
# whoever emailed the agent.
_HERMES_NOTICE_PREFIXES = ("📬 No home channel is set",)
_TOOL_REPLY_WINDOW = 15 * 60  # seconds within which a tool reply pre-empts the turn's final text
_FIND_RETRIES = (0.5, 1.5, 3.0)  # the API row can lag the websocket frame by a moment


def state_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()) / "openmail"
    except Exception:  # noqa: BLE001 — outside Hermes
        return Path.home() / ".hermes" / "openmail"


@dataclass
class ThreadContext:
    """What ``send`` needs to answer a conversation: the inbox it arrived at, whom to answer, which thread."""

    inbox_id: str
    to: str
    thread_id: str
    subject: Optional[str] = None
    mode: str = "channel"
    updated_at: float = 0.0


class ThreadStore:
    """chat key -> ThreadContext, on disk so replies still thread after a gateway restart."""

    def __init__(self, path: Path, max_entries: int = 2000):
        self.path = path
        self.max_entries = max_entries
        self._items: Dict[str, ThreadContext] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        fields = ThreadContext.__dataclass_fields__
        for key, value in raw.items():
            if isinstance(value, dict) and value.get("inbox_id") and value.get("to") and value.get("thread_id"):
                self._items[key] = ThreadContext(**{k: v for k, v in value.items() if k in fields})

    def _flush(self) -> None:
        if len(self._items) > self.max_entries:
            excess = len(self._items) - self.max_entries
            for key in sorted(self._items, key=lambda k: self._items[k].updated_at)[:excess]:
                del self._items[key]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: asdict(v) for k, v in self._items.items()}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("[OpenMail] thread store save failed: %s", exc)

    def get(self, key: str) -> Optional[ThreadContext]:
        return self._items.get(key)

    def put(self, key: str, ctx: ThreadContext) -> None:
        ctx.updated_at = time.time()
        self._items[key] = ctx
        self._flush()


def chat_key(chat_id: str, thread_id: Optional[str]) -> str:
    return f"{chat_id}|{thread_id or ''}"


class OpenMailAdapter(BasePlatformAdapter):
    """One adapter = one OpenMail API key: an inbox, or every inbox in a pod."""

    def __init__(self, config: PlatformConfig, *, api: Optional[OpenMailApi] = None,
                 threads: Optional[ThreadStore] = None):
        super().__init__(config, Platform(PLATFORM_NAME))
        self.settings: OpenMailConfig = read_config(config.extra or {})
        self.api = api or OpenMailApi(self.settings.base_url, self.settings.api_key)
        self.scope: str = "inbox"  # or "pod", decided at connect
        self.inbox_id: Optional[str] = self.settings.inbox_id
        self.inbox_address: Optional[str] = None
        self.pod_id: Optional[str] = self.settings.pod_id
        self._inbox_addresses: Dict[str, str] = {}
        self._stream_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._warned_default_deny = False
        self.threads = threads or ThreadStore(state_dir() / "threads.json")

    # ---- lifecycle ---------------------------------------------------------------------------
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.settings.configured:
            message = ("OPENMAIL_API_KEY is not set. Get a key at https://console.openmail.sh and add it to "
                       "~/.hermes/.env")
            logger.error("[OpenMail] %s", message)
            self._set_fatal_error("openmail_missing_configuration", message, retryable=False)
            return False
        try:
            await asyncio.to_thread(self._resolve_scope)
        except OpenMailApiError as exc:
            retryable = exc.status not in (401, 403)
            logger.error("[OpenMail] cannot start: %s", exc)
            self._set_fatal_error("openmail_connect_error" if retryable else "openmail_auth_error", str(exc),
                                  retryable=retryable)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("[OpenMail] cannot start: %s", exc)
            self._set_fatal_error("openmail_connect_error", str(exc), retryable=True)
            return False

        self._running = True
        _tools.bind(self.api, self.inbox_id if self.scope == "inbox" else None)
        if self.settings.mode == "tool" and self.scope == "inbox":
            logger.info("[OpenMail] %s in tool mode: inbound mail is not streamed", self.inbox_address)
        else:
            self._stop = asyncio.Event()
            self._stream_task = asyncio.create_task(self._stream())
        label = f"pod {self.pod_id}" if self.scope == "pod" else self.inbox_address or self.inbox_id
        print(f"[OpenMail] Connected as {label} ({self.settings.mode} mode)")
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._stop.set()
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stream_task = None
        logger.info("[OpenMail] Disconnected.")

    def _resolve_scope(self) -> None:
        """Inbox or pod scope, from config or from what the key can see. Creates an inbox when the key
        sees none (an account key on a fresh account)."""
        if self.pod_id:
            pod = self.api.get_pod(self.pod_id)
            self.pod_id = str(pod.get("id") or self.pod_id)
            self.scope = "pod"
            return
        if self.inbox_id:
            inbox = self.api.get_inbox(self.inbox_id)
            self.inbox_address = inbox.get("address")
            self.scope = "inbox"
            return
        inboxes = self.api.list_inboxes()
        if len(inboxes) == 1:
            inbox = inboxes[0]
        elif not inboxes:
            inbox = self.api.create_inbox(display_name="Hermes")
            logger.info("[OpenMail] created inbox %s", inbox.get("address"))
        else:
            pods = self._pods_or_empty()
            if len(pods) == 1:
                self.pod_id = str(pods[0]["id"])
                self.scope = "pod"
                logger.info("[OpenMail] key sees %d inboxes in one pod; running pod-scoped", len(inboxes))
                return
            addresses = ", ".join(str(i.get("address")) for i in inboxes[:5])
            raise RuntimeError(f"the key sees {len(inboxes)} inboxes ({addresses}). Set OPENMAIL_INBOX_ID to "
                               "pick one, or OPENMAIL_POD_ID to run them all.")
        self.inbox_id = str(inbox["id"])
        self.inbox_address = inbox.get("address")
        self.scope = "inbox"

    def _pods_or_empty(self) -> List[Dict[str, Any]]:
        try:
            return self.api.list_pods()
        except OpenMailApiError:
            return []

    # ---- inbound -----------------------------------------------------------------------------
    async def _stream(self) -> None:
        cursor = Cursor(state_dir() / f"{self.pod_id or self.inbox_id}.cursor")
        cursor.load()
        try:
            await run_stream(url=ws_url(self.settings.base_url), api_key=self.settings.api_key,
                             inbox_id=None if self.scope == "pod" else self.inbox_id, cursor=cursor,
                             on_event=self._on_event, stop=self._stop)
        except FatalStreamError as exc:
            logger.error("[OpenMail] server refused the stream: %s", exc)
            self._set_fatal_error("openmail_auth_error",
                                  f"OpenMail refused the websocket: {exc}. Check OPENMAIL_API_KEY.", retryable=False)
            await self._notify_fatal_error()
        except asyncio.CancelledError:
            pass

    async def _find_message(self, thread_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        for delay in (*_FIND_RETRIES, None):
            message = await asyncio.to_thread(self.api.find_message, thread_id, message_id)
            if message is not None or delay is None:
                return message
            await asyncio.sleep(delay)
        return None

    async def _address_of(self, inbox_id: str) -> Optional[str]:
        if inbox_id not in self._inbox_addresses:
            try:
                inbox = await asyncio.to_thread(self.api.get_inbox, inbox_id)
                self._inbox_addresses[inbox_id] = str(inbox.get("address") or "")
            except OpenMailApiError:
                return None
        return self._inbox_addresses.get(inbox_id) or None

    def _sender_allowed(self, address: str) -> bool:
        """The gateway's default-deny, applied before a MessageEvent exists so an unknown sender never
        gets a pairing reply by email. OpenMail's server-side allow/block rules run before this."""
        if allow_all_senders():
            return True
        allowed = allowed_senders()
        if not allowed:
            if not self._warned_default_deny:
                self._warned_default_deny = True
                logger.warning("[OpenMail] dropping mail from %s: OPENMAIL_ALLOWED_USERS is empty and "
                               "OPENMAIL_ALLOW_ALL_USERS is not true. Set one of them.", address)
            return False
        return "*" in allowed or address in allowed

    async def _on_event(self, payload: Mapping[str, Any]) -> None:
        event_message = payload["message"]
        thread_id = str(payload["thread_id"])
        message_id = str(event_message["id"])
        event_inbox = str(payload.get("inbox_id") or "")
        if self.scope == "inbox" and event_inbox and event_inbox != self.inbox_id:
            return

        # The frame is a hint; the API record is the truth (sender, body, classification, attachment text).
        message = await self._find_message(thread_id, message_id)
        if message is None:
            logger.info("[OpenMail] dropping %s: not found in thread %s", message_id, thread_id)
            return
        if message.get("direction") == "outbound":
            return
        inbox_id = self.inbox_id if self.scope == "inbox" else str(message.get("inboxId") or event_inbox)
        if not inbox_id:
            return
        sender_name, sender = parse_address(str(message.get("fromAddr") or ""))
        if not sender:
            return
        _, claimed = parse_address(str(event_message.get("from") or ""))
        if claimed and claimed != sender:
            logger.warning("[OpenMail] frame claimed From %s but the API says %s; using the API", claimed, sender)

        inbox_address = await self._address_of(inbox_id) if self.scope == "pod" else self.inbox_address
        mode = self.settings.inbox_mode(inbox_id, inbox_address)
        if mode == "tool" or not self._sender_allowed(sender):
            return
        classification = classify(message, event_message)
        if classification.rejected:
            logger.info("[OpenMail] dropping %s mail from %s", classification.verdict, sender)
            return
        effective_mode = "channel" if mode == "channel" and classification.wants_reply else "notify"
        if mode == "channel" and effective_mode == "notify":
            logger.info("[OpenMail] %s mail from %s handed to the agent without a reply turn",
                        classification.category or "non-replyable", sender)

        attachments = message.get("attachments") or []
        staged = await self._stage_attachments(message_id, attachments) if attachments else StagedMedia()
        subject = message.get("subject") or event_message.get("subject")
        body = str(message.get("bodyText") or event_message.get("body_text") or "")
        text = build_agent_text(
            sender=str(message.get("fromAddr")), to=message.get("toAddr") or event_message.get("to"),
            subject=subject, thread_id=thread_id, message_id=message_id, body=body, attachments=attachments,
            staged=staged, mode=effective_mode, inbox_id=inbox_id if self.scope == "pod" else None,
            category=classification.category,
        )

        # One conversation per correspondent; in pod scope, per (inbox, correspondent).
        source_thread = inbox_id if self.scope == "pod" else None
        self.threads.put(chat_key(sender, source_thread), ThreadContext(
            inbox_id=inbox_id, to=sender, thread_id=thread_id, subject=subject, mode=effective_mode))
        kinds = set(staged.types)
        message_type = (MessageType.DOCUMENT if any(t not in _IMAGE_TYPES for t in kinds)
                        else MessageType.PHOTO if kinds else MessageType.TEXT)
        event = MessageEvent(
            text=text, message_id=message_id, message_type=message_type,
            source=self.build_source(chat_id=sender, chat_name=sender_name or sender, chat_type="dm",
                                     user_id=sender, user_name=sender_name or sender, thread_id=source_thread,
                                     chat_topic=subject or None),
            media_urls=list(staged.paths), media_types=list(staged.types), allow_gateway_control=False,
            metadata={"openmail_inbox_id": inbox_id, "openmail_thread_id": thread_id,
                      "openmail_message_id": message_id, "openmail_mode": effective_mode,
                      "openmail_category": classification.category},
        )
        logger.info("[OpenMail] mail from %s: %s", sender, subject or "(no subject)")
        await self.handle_message(event)

    async def _stage_attachments(self, message_id: str, attachments: List[Mapping[str, Any]]) -> StagedMedia:
        staged = StagedMedia()
        for att in attachments:
            filename = str(att.get("filename") or "")
            if not filename or att.get("parsedText"):
                continue  # text already inlined; the binary adds nothing the model can read
            try:
                data, content_type = await asyncio.to_thread(
                    self.api.download_attachment, message_id, filename, _DOWNLOAD_CEILING)
                content_type = content_type or str(att.get("contentType") or "application/octet-stream")
                if content_type in _IMAGE_TYPES:
                    path = cache_image_from_bytes(data, _IMAGE_EXT[content_type])
                else:
                    path = cache_document_from_bytes(data, filename)
                staged.paths.append(path)
                staged.types.append(content_type)
            except AttachmentTooLargeError:
                staged.skipped.append(filename)
            except Exception as exc:  # noqa: BLE001 — attachments are a convenience; the mail still gets answered
                logger.warning("[OpenMail] could not stage %s: %s", filename, exc)
        return staged

    # ---- outbound ----------------------------------------------------------------------------
    def _context_for(self, chat_id: str, metadata: Optional[Mapping[str, Any]]) -> Optional[ThreadContext]:
        thread_id = str((metadata or {}).get("thread_id") or "") or None
        return self.threads.get(chat_key(chat_id, thread_id)) or self.threads.get(chat_key(chat_id, None))

    def _default_inbox(self) -> Optional[str]:
        if self.inbox_id:
            return self.inbox_id
        try:
            inboxes = self.api.list_inboxes()
        except OpenMailApiError:
            return None
        return str(inboxes[0]["id"]) if inboxes else None

    async def _deliver_notice(self, text: str) -> bool:
        """Notify mode: the agent's words go to the user's home channels, never back to the sender."""
        runner = self.gateway_runner
        transports = getattr(runner, "_home_channel_transports", None)
        sender = getattr(runner, "_send_home_channel_message", None)
        if not callable(transports) or not callable(sender):
            logger.warning("[OpenMail] notify: no home channel available; message: %s", text[:200])
            return False
        delivered = False
        for platform, _cfg, home, transport in transports():
            if platform == self.platform:
                continue
            if await sender(platform, home, transport, text, "[OpenMail] notify to %s:%s failed: %s"):
                delivered = True
        if not delivered:
            logger.warning("[OpenMail] notify: no home channel accepted the message; set one with "
                           "`hermes gateway setup`. Message: %s", text[:160].replace("\n", " "))
        return delivered

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self._send(chat_id, content, metadata=metadata)

    async def _send(self, chat_id: str, content: str, *, metadata: Optional[Mapping[str, Any]] = None,
                    attachments: Optional[List[str]] = None) -> SendResult:
        text = (content or "").strip()
        if not text and not attachments:
            return SendResult(success=True)
        if text.startswith(_HERMES_NOTICE_PREFIXES):
            # Gateway housekeeping meant for the operator, not for whoever emailed the agent.
            logger.info("[OpenMail] dropped gateway notice to %s: %s", chat_id, text[:80].replace("\n", " "))
            return SendResult(success=True)
        ctx = self._context_for(chat_id, metadata)
        if ctx and _tools.recent_tool_replies.pop(ctx.thread_id, 0) > time.time() - _TOOL_REPLY_WINDOW:
            logger.info("[OpenMail] agent already answered thread %s with openmail_reply; dropping final text: %s",
                        ctx.thread_id, text[:80].replace("\n", " "))
            return SendResult(success=True)
        if ctx and ctx.mode == "notify":
            ok = await self._deliver_notice(text)
            return SendResult(success=ok, error=None if ok else "no home channel accepted the notification")
        try:
            if ctx:
                result = await asyncio.to_thread(lambda: self.api.send(
                    inbox_id=ctx.inbox_id, to=ctx.to, body=text, thread_id=ctx.thread_id,
                    attachments=attachments))
            else:
                # No conversation on record: chat_id is a bare address (cron delivery, send_message tool).
                inbox_id = await asyncio.to_thread(self._default_inbox)
                if not inbox_id:
                    return SendResult(success=False, error="no inbox to send from")
                subject = str((metadata or {}).get("subject") or DEFAULT_SUBJECT)
                result = await asyncio.to_thread(lambda: self.api.send(
                    inbox_id=inbox_id, to=chat_id, subject=subject, body=text, attachments=attachments))
            return SendResult(success=True, message_id=str(result.get("messageId") or result.get("id") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.error("[OpenMail] send to %s failed: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            **kwargs: Any) -> SendResult:
        return await self._send(chat_id, caption or "", metadata=kwargs.get("metadata"), attachments=[file_path])

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        path = image_url[7:] if image_url.startswith("file://") else image_url
        if Path(path).exists():
            return await self._send(chat_id, caption or "", metadata=metadata, attachments=[path])
        return await self._send(chat_id, f"{caption or ''}\n\n{image_url}".strip(), metadata=metadata)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        ctx = self.threads.get(chat_key(chat_id, None))
        return {"name": chat_id, "type": "dm", "chat_id": chat_id, "subject": ctx.subject if ctx else ""}
