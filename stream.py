"""OpenMail websocket: subscribe to ``message.received``, replay from a cursor, reconnect with backoff."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from .inbound import valid_event

logger = logging.getLogger(__name__)

FATAL_CLOSE_CODES = {4001, 4003, 4008}  # unauthorized, forbidden, policy: reconnecting won't help
PING_INTERVAL_S = 30
RESUBSCRIBE_INTERVAL_S = 60  # pod scope: pick up inboxes created after we subscribed
MAX_BACKOFF_S = 30


def ws_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/v1/ws"


class Cursor:
    """Last acknowledged ``event_id``, on disk, so a restart replays what was missed rather than losing it."""

    def __init__(self, path: Path):
        self.path = path
        self._value: Optional[str] = None

    def load(self) -> Optional[str]:
        try:
            self._value = self.path.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            self._value = None
        except OSError as exc:
            logger.warning("[OpenMail] cursor read failed: %s", exc)
        return self._value

    @property
    def value(self) -> Optional[str]:
        return self._value

    def save(self, event_id: str) -> None:
        self._value = event_id
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(event_id, encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:  # losing the cursor only costs a replay after restart
            logger.warning("[OpenMail] cursor save failed: %s", exc)


class FatalStreamError(Exception):
    """Server rejected the connection for a reason a retry cannot fix (bad key, revoked, forbidden)."""


async def run_stream(*, url: str, api_key: str, inbox_id: Optional[str], cursor: Cursor,
                     on_event: Callable[[Mapping[str, Any]], Awaitable[None]], stop: asyncio.Event,
                     on_connected: Optional[Callable[[], None]] = None) -> None:
    """Keep one subscription alive until ``stop`` is set. Raises FatalStreamError on 4001/4003/4008."""
    import websockets

    backoff = 1.0
    while not stop.is_set():
        started = asyncio.get_running_loop().time()
        try:
            await _connect_once(websockets, url=url, api_key=api_key, inbox_id=inbox_id, cursor=cursor,
                                on_event=on_event, stop=stop, on_connected=on_connected)
        except FatalStreamError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — network trouble: back off and retry
            logger.warning("[OpenMail] websocket error: %s", exc)
        if stop.is_set():
            return
        stable = asyncio.get_running_loop().time() - started > 60
        backoff = 1.0 if stable else min(MAX_BACKOFF_S, backoff * 2)
        delay = backoff * (0.5 + random.random())
        logger.info("[OpenMail] reconnecting in %.1fs", delay)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def _connect_once(websockets: Any, *, url: str, api_key: str, inbox_id: Optional[str], cursor: Cursor,
                        on_event: Callable[[Mapping[str, Any]], Awaitable[None]], stop: asyncio.Event,
                        on_connected: Optional[Callable[[], None]]) -> None:
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        connect = websockets.connect(url, additional_headers=headers, ping_interval=None, max_size=8 * 1024 * 1024)
    except TypeError:  # websockets < 13 spelled it extra_headers
        connect = websockets.connect(url, extra_headers=headers, ping_interval=None, max_size=8 * 1024 * 1024)

    async with connect as ws:
        subscribed_count = -1

        async def subscribe(with_cursor: bool) -> None:
            frame: Dict[str, Any] = {"type": "subscribe", "event_types": ["message.received"]}
            if inbox_id:
                frame["inbox_ids"] = [inbox_id]
            if with_cursor and cursor.value:
                frame["last_event_id"] = cursor.value
            await ws.send(json.dumps(frame))

        await subscribe(True)
        if on_connected:
            on_connected()
        logger.info("[OpenMail] websocket connected")

        async def keepalive() -> None:
            tick = 0
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                tick += PING_INTERVAL_S
                await ws.send(json.dumps({"type": "ping"}))
                if not inbox_id and tick % RESUBSCRIBE_INTERVAL_S == 0:
                    await subscribe(False)

        async def stopper() -> None:
            await stop.wait()
            await ws.close()

        tasks = [asyncio.create_task(keepalive()), asyncio.create_task(stopper())]
        try:
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                if kind == "error":
                    logger.warning("[OpenMail] server error: %s", payload.get("message"))
                    continue
                if kind == "subscribed":
                    ids = payload.get("inbox_ids")
                    count = len(ids) if isinstance(ids, list) else 0
                    if not inbox_id and count != subscribed_count:
                        if subscribed_count >= 0:
                            logger.info("[OpenMail] pod now streams %d inbox(es)", count)
                        subscribed_count = count
                    continue
                if not valid_event(payload):
                    continue
                # Admit in receive order so the cursor never skips ahead of an event.
                try:
                    await on_event(payload)
                except Exception as exc:  # noqa: BLE001 — one bad message must not drop the stream
                    logger.error("[OpenMail] event handling failed for %s: %s", payload.get("event_id"), exc)
                cursor.save(str(payload["event_id"]))
        finally:
            for task in tasks:
                task.cancel()
            code = getattr(ws, "close_code", None)
            reason = getattr(ws, "close_reason", "") or ""
            if code in FATAL_CLOSE_CODES:
                raise FatalStreamError(f"{code} {reason}".strip())
            if code is not None and not stop.is_set():
                logger.info("[OpenMail] websocket closed (%s) %s", code, reason)
