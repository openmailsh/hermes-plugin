"""Minimal OpenMail REST client. Sync httpx; the adapter runs it off the event loop."""

from __future__ import annotations

import mimetypes
import uuid
from urllib.parse import quote
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
USER_AGENT = "openmail-hermes/0.1.0"


class OpenMailApiError(Exception):
    def __init__(self, message: str, status: int, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class AttachmentTooLargeError(Exception):
    def __init__(self, filename: str, size: int, max_bytes: int):
        super().__init__(f"attachment {filename} ({size} bytes) exceeds the {max_bytes} byte cap")
        self.filename, self.size, self.max_bytes = filename, size, max_bytes


def _pick_list(data: Any, *keys: str) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


class OpenMailApi:
    def __init__(self, base_url: str, api_key: str, *, client: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=TIMEOUT)

    # ---- transport -------------------------------------------------------------------------
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}", "User-Agent": USER_AGENT}
        headers.update(extra or {})
        return headers

    def request(self, method: str, path: str, *, json: Any = None, params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None, data: Any = None, files: Any = None) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        response = self._client.request(
            method, f"{self.base_url}{path}", json=json, params=clean_params or None,
            headers=self._headers(headers), data=data, files=files,
        )
        try:
            parsed: Any = response.json() if response.content else None
        except ValueError:
            parsed = response.text
        if response.is_error:
            detail = parsed.get("message") if isinstance(parsed, dict) and parsed.get("message") else str(parsed)[:200]
            raise OpenMailApiError(f"OpenMail API {response.status_code}: {detail}", response.status_code, parsed)
        return parsed

    # ---- inboxes / pods ---------------------------------------------------------------------
    def list_inboxes(self) -> List[Dict[str, Any]]:
        return _pick_list(self.request("GET", "/v1/inboxes", params={"limit": 50}), "data", "inboxes")

    def get_inbox(self, inbox_id: str) -> Dict[str, Any]:
        data = self.request("GET", f"/v1/inboxes/{inbox_id}")
        return data.get("inbox", data) if isinstance(data, dict) else data

    def create_inbox(self, *, display_name: Optional[str] = None, mailbox_name: Optional[str] = None,
                     pod_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if display_name:
            body["displayName"] = display_name
        if mailbox_name:
            body["mailboxName"] = mailbox_name
        if pod_id:
            body["podId"] = pod_id
        return self.request("POST", "/v1/inboxes", json=body)

    def create_inbox_key(self, inbox_id: str, name: str) -> Dict[str, Any]:
        return self.request("POST", f"/v1/inboxes/{inbox_id}/api-keys", json={"name": name})

    def create_pod_key(self, pod_id: str, name: str) -> Dict[str, Any]:
        return self.request("POST", f"/v1/pods/{pod_id}/api-keys", json={"name": name})

    def list_pods(self) -> List[Dict[str, Any]]:
        return _pick_list(self.request("GET", "/v1/pods", params={"limit": 100}), "data", "pods")

    def get_pod(self, pod_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/v1/pods/{pod_id}")

    # ---- mail -------------------------------------------------------------------------------
    def list_threads(self, inbox_id: str, *, limit: Optional[int] = None, offset: Optional[int] = None,
                     is_read: Optional[bool] = None) -> Any:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if is_read is not None:
            params["is_read"] = "true" if is_read else "false"
        return self.request("GET", f"/v1/inboxes/{inbox_id}/threads", params=params)

    def list_messages(self, inbox_id: str, *, direction: Optional[str] = None, limit: Optional[int] = None,
                      offset: Optional[int] = None) -> Any:
        return self.request("GET", f"/v1/inboxes/{inbox_id}/messages",
                            params={"direction": direction, "limit": limit, "offset": offset})

    def thread_messages(self, thread_id: str) -> List[Dict[str, Any]]:
        return _pick_list(self.request("GET", f"/v1/threads/{thread_id}/messages"), "data", "messages")

    def mark_thread(self, thread_id: str, *, is_read: bool) -> Any:
        return self.request("PATCH", f"/v1/threads/{thread_id}", json={"is_read": is_read})

    def find_message(self, thread_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        """The API's copy of one message, or None when the (thread, message) pair isn't visible to this key."""
        try:
            for message in self.thread_messages(thread_id):
                if message.get("id") == message_id:
                    return message
            return None
        except OpenMailApiError as exc:
            if exc.status in (403, 404):
                return None
            raise

    def attachment_text(self, message_id: str, filename: str) -> Any:
        return self.request("GET", f"/v1/attachments/{message_id}/{quote(filename, safe='')}/text")

    def download_attachment(self, message_id: str, filename: str, max_bytes: int) -> Tuple[bytes, Optional[str]]:
        url = f"{self.base_url}/v1/attachments/{message_id}/{quote(filename, safe='')}"
        with self._client.stream("GET", url, headers=self._headers()) as response:
            if response.is_error:
                response.read()
                raise OpenMailApiError(f"OpenMail API {response.status_code}: attachment download failed",
                                       response.status_code, response.text)
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise AttachmentTooLargeError(filename, declared, max_bytes)
            chunks: List[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise AttachmentTooLargeError(filename, total, max_bytes)
                chunks.append(chunk)
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip() or None
            return b"".join(chunks), content_type

    def send(self, *, inbox_id: str, to: str, body: str, subject: Optional[str] = None,
             thread_id: Optional[str] = None, cc: Optional[Sequence[str]] = None,
             reply_to: Optional[str] = None, include_quote: Optional[bool] = None,
             attachments: Optional[Iterable[str]] = None, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """New thread (subject required) or in-thread reply (thread_id; the API derives "Re: ...")."""
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        path = f"/v1/inboxes/{inbox_id}/send"
        paths = [Path(p) for p in (attachments or [])]
        if paths:
            form: List[Tuple[str, str]] = [("to", to), ("body", body)]
            if subject:
                form.append(("subject", subject))
            if thread_id:
                form.append(("threadId", thread_id))
            if include_quote is False:
                form.append(("includeQuote", "false"))
            if reply_to:
                form.append(("replyTo", reply_to))
            form.extend(("cc", address) for address in (cc or []))
            files = []
            for p in paths:
                content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                files.append(("attachments", (p.name, p.read_bytes(), content_type)))
            return self.request("POST", path, data=form, files=files, headers=headers)
        payload: Dict[str, Any] = {"to": to, "body": body}
        if subject:
            payload["subject"] = subject
        if thread_id:
            payload["threadId"] = thread_id
        if cc:
            payload["cc"] = list(cc)
        if reply_to:
            payload["replyTo"] = reply_to
        if include_quote is False:
            payload["includeQuote"] = False
        return self.request("POST", path, json=payload, headers=headers)

    def close(self) -> None:
        self._client.close()
