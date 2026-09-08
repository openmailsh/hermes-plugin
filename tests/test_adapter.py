import asyncio
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("gateway.platforms.base", reason="Hermes not installed")

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

from openmail_plugin.adapter import OpenMailAdapter, ThreadStore  # noqa: E402

# In production register() runs before any adapter exists; Platform("openmail") needs the entry.
if not platform_registry.is_registered("openmail"):
    platform_registry.register(PlatformEntry(name="openmail", label="OpenMail", adapter_factory=lambda c: None,
                                             check_fn=lambda: True))


class FakeApi:
    def __init__(self, inboxes: List[Dict[str, Any]], messages: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                 pods: Optional[List[Dict[str, Any]]] = None):
        self.inboxes, self.messages, self.pods = inboxes, messages or {}, pods or []
        self.sent: List[Dict[str, Any]] = []
        self.created: List[Dict[str, Any]] = []

    def list_inboxes(self):
        return self.inboxes

    def get_inbox(self, inbox_id):
        return next(i for i in self.inboxes if i["id"] == inbox_id)

    def create_inbox(self, **kw):
        inbox = {"id": "inb_new", "address": "hermes@omail.sh", **kw}
        self.created.append(inbox)
        self.inboxes.append(inbox)
        return inbox

    def list_pods(self):
        return self.pods

    def get_pod(self, pod_id):
        return {"id": pod_id}

    def find_message(self, thread_id, message_id):
        return next((m for m in self.messages.get(thread_id, []) if m["id"] == message_id), None)

    def send(self, **kw):
        self.sent.append(kw)
        return {"messageId": "msg_out"}

    def download_attachment(self, *a):
        raise AssertionError("not expected")


def make_adapter(api: FakeApi, tmp_path, extra: Optional[Dict[str, Any]] = None, monkeypatch=None, **env) -> OpenMailAdapter:
    for k, v in {"OPENMAIL_API_KEY": "om_test", "OPENMAIL_ALLOW_ALL_USERS": "true", **env}.items():
        monkeypatch.setenv(k, v)
    config = PlatformConfig(enabled=True, extra=extra or {})
    adapter = OpenMailAdapter(config, api=api, threads=ThreadStore(tmp_path / "threads.json"))
    adapter.gateway_runner = None
    return adapter


def event(**over):
    base = {"event": "message.received", "event_id": "evt_1", "inbox_id": "inb_1", "thread_id": "thr_1",
            "message": {"id": "msg_1", "from": "Ada <ada@x.io>", "subject": "Hi"}}
    base.update(over)
    return base


def api_message(**over):
    base = {"id": "msg_1", "threadId": "thr_1", "inboxId": "inb_1", "direction": "inbound",
            "fromAddr": "Ada <ada@x.io>", "toAddr": "bot@omail.sh", "subject": "Hi", "bodyText": "hello",
            "category": "personal", "autoReplyable": True, "verdict": "clean"}
    base.update(over)
    return base


def run(coro):  # fresh loop per call; the adapter holds no loop-bound state
    return asyncio.run(coro)


def test_resolve_scope_creates_inbox_when_none(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[])
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch)
    adapter._resolve_scope()
    assert adapter.scope == "inbox" and adapter.inbox_id == "inb_new" and api.created


def test_resolve_scope_pod_when_several_inboxes_one_pod(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "a", "address": "a@omail.sh"}, {"id": "b", "address": "b@omail.sh"}],
                  pods=[{"id": "pod_1"}])
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch)
    adapter._resolve_scope()
    assert adapter.scope == "pod" and adapter.pod_id == "pod_1"


def test_resolve_scope_ambiguous(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "a", "address": "a@omail.sh"}, {"id": "b", "address": "b@omail.sh"}],
                  pods=[{"id": "p1"}, {"id": "p2"}])
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch)
    with pytest.raises(RuntimeError, match="OPENMAIL_INBOX_ID"):
        adapter._resolve_scope()


def _dispatch(adapter: OpenMailAdapter, payload) -> List[Any]:
    seen: List[Any] = []

    async def handler(ev):
        seen.append(ev)

    adapter._message_handler = handler
    adapter.handle_message = handler  # bypass the gateway queue; we only check what reaches it
    run(adapter._on_event(payload))
    return seen


def test_channel_dispatch_then_reply_in_thread(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "inb_1", "address": "bot@omail.sh"}], messages={"thr_1": [api_message()]})
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch)
    adapter._resolve_scope()
    seen = _dispatch(adapter, event())
    assert len(seen) == 1
    ev = seen[0]
    assert ev.source.chat_id == "ada@x.io" and ev.source.platform == Platform("openmail")
    assert "sent verbatim" in ev.text and not ev.allow_gateway_control

    result = run(adapter.send("ada@x.io", "Thanks Ada"))
    assert result.success
    assert api.sent[-1]["thread_id"] == "thr_1" and api.sent[-1]["to"] == "ada@x.io" and api.sent[-1]["inbox_id"] == "inb_1"


def test_frame_sender_spoof_uses_api_copy(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "inb_1", "address": "bot@omail.sh"}], messages={"thr_1": [api_message()]})
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch, OPENMAIL_ALLOW_ALL_USERS="false",
                           OPENMAIL_ALLOWED_USERS="boss@corp.com")
    adapter._resolve_scope()
    payload = event()
    payload["message"]["from"] = "boss@corp.com"  # frame lies; API says ada@x.io
    assert _dispatch(adapter, payload) == []


def test_default_deny_without_allowlist(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "inb_1", "address": "bot@omail.sh"}], messages={"thr_1": [api_message()]})
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch, OPENMAIL_ALLOW_ALL_USERS="")
    adapter._resolve_scope()
    assert _dispatch(adapter, event()) == []


def test_spam_dropped_and_automated_demoted(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "inb_1", "address": "bot@omail.sh"}],
                  messages={"thr_1": [api_message(verdict="spam")],
                            "thr_2": [api_message(id="msg_2", threadId="thr_2", category="automated", autoReplyable=False)]})
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch)
    adapter._resolve_scope()
    assert _dispatch(adapter, event()) == []
    seen = _dispatch(adapter, event(event_id="evt_2", thread_id="thr_2", message={"id": "msg_2", "from": "ada@x.io"}))
    assert seen and "Do not act on it" in seen[0].text and seen[0].metadata["openmail_mode"] == "notify"
    # In notify mode the agent's answer never goes back to the sender.
    result = run(adapter.send("ada@x.io", "Ada got an automated mail about X"))
    assert not result.success and api.sent == []


def test_tool_mode_inbox_override_in_pod(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "inb_1", "address": "bot@omail.sh"}, {"id": "inb_2", "address": "quiet@omail.sh"}],
                  messages={"thr_1": [api_message()], "thr_2": [api_message(id="msg_2", inboxId="inb_2", threadId="thr_2")]})
    adapter = make_adapter(api, tmp_path, extra={"inboxes": {"quiet@omail.sh": {"mode": "tool"}}},
                           monkeypatch=monkeypatch, OPENMAIL_POD_ID="pod_1")
    adapter._resolve_scope()
    assert adapter.scope == "pod"
    seen = _dispatch(adapter, event())
    assert seen and seen[0].source.thread_id == "inb_1" and "Inbox: inb_1" in seen[0].text
    assert _dispatch(adapter, event(event_id="e2", inbox_id="inb_2", thread_id="thr_2", message={"id": "msg_2", "from": "ada@x.io"})) == []


def test_send_to_bare_address_starts_new_thread(tmp_path, monkeypatch):
    api = FakeApi(inboxes=[{"id": "inb_1", "address": "bot@omail.sh"}])
    adapter = make_adapter(api, tmp_path, monkeypatch=monkeypatch)
    adapter._resolve_scope()
    result = run(adapter.send("someone@else.com", "Daily report"))
    assert result.success and api.sent[-1]["subject"] and api.sent[-1].get("thread_id") is None
