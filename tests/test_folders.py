import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from openmail_plugin import tools
from openmail_plugin.api import OpenMailApi


@pytest.fixture
def folder_tools(monkeypatch):
    folders = [{"id": 3, "name": "Drafts", "role": "drafts"},
               {"id": 9, "name": "Finance/Receipts", "role": None}]
    items = [{"id": "m1", "kind": "message", "threadId": "t1", "subject": "Hello",
              "fromAddr": "a@example.com", "toAddr": "agent@example.com",
              "toRecipients": ["agent@example.com"], "createdAt": "2026-11-25T12:00:00Z"},
             {"id": "d1", "kind": "draft", "threadId": None, "subject": None,
              "fromAddr": "agent@example.com", "toAddr": None,
              "toRecipients": [], "createdAt": "2026-11-25T10:00:00Z"}]
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer test-key"
        path = request.url.raw_path.split(b"?")[0]
        if path == b"/v1/inboxes/team%2Fa/folders":
            return httpx.Response(200, json={"data": folders})
        if path == b"/v1/inboxes/team%2Fa/folders/3/messages":
            start, size = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json={"data": items[start:start + size], "total": len(items)})
        if path == b"/v1/inboxes/team%2Fa/folders/9/messages":
            return httpx.Response(200, json={"data": [], "total": 0})
        if path.startswith(b"/v1/inboxes/other/"):
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(404, json={"error": "not_found"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(tools, "_api", OpenMailApi("https://example.test", "test-key", client=client))
        monkeypatch.setattr(tools, "_inbox_id", "team/a")
        ctx = SimpleNamespace(register_tool=Mock())
        tools.register_tools(ctx)
        handlers = {call.args[0]: call.args[3] for call in ctx.register_tool.call_args_list}
        yield handlers, requests, folders, items


def test_discover_folders_and_browse_items(folder_tools):
    handlers, requests, folders, items = folder_tools
    found = json.loads(handlers["openmail_list_folders"]({}))
    assert found == {"data": folders}
    browse = handlers["openmail_list_folder_messages"]
    drafts, empty = (folder["id"] for folder in found["data"])
    assert json.loads(browse({"folder_id": drafts})) == {"data": items, "total": 2}
    assert dict(requests[-1].url.params) == {"limit": "50", "offset": "0"}
    assert json.loads(browse({"folder_id": drafts, "limit": 1, "offset": 1})) == {"data": items[1:], "total": 2}
    assert json.loads(browse({"folder_id": drafts, "offset": 2})) == {"data": [], "total": 2}
    assert json.loads(browse({"folder_id": empty})) == {"data": [], "total": 0}


def test_folder_tools_propagate_access_and_missing_folder_errors(folder_tools):
    handlers, requests, _, _ = folder_tools
    assert json.loads(handlers["openmail_list_folders"]({"inbox_id": "other"}))["status"] == 403
    assert json.loads(handlers["openmail_list_folder_messages"]({"inbox_id": "other", "folder_id": 3}))["status"] == 403
    assert json.loads(handlers["openmail_list_folder_messages"]({"folder_id": 100}))["status"] == 404
    assert len(requests) == 3


@pytest.mark.parametrize("args", [
    {}, {"folder_id": "Drafts"}, {"folder_id": True}, {"folder_id": 0}, {"folder_id": 2_147_483_648},
    {"folder_id": 3, "limit": 0}, {"folder_id": 3, "limit": 101},
    {"folder_id": 3, "offset": -1}, {"folder_id": 3, "offset": 2_147_483_648},
])
def test_folder_parameters_match_the_api_bounds(folder_tools, args):
    jsonschema = pytest.importorskip("jsonschema")
    handlers, requests, _, _ = folder_tools
    schema = next(s for name, _, s, _ in tools.TOOLS if name == "openmail_list_folder_messages")
    assert not jsonschema.Draft7Validator(schema["parameters"]).is_valid(args)
    assert "error" in json.loads(handlers["openmail_list_folder_messages"](args))
    assert not requests


@pytest.mark.parametrize("name,args,method,suffix,payload,status,result", [
    ("create_folder", {"name":"Work"}, "POST", "/folders", {"name":"Work"}, 201, {"id":13,"name":"Work"}),
    ("rename_folder", {"folder_id":13,"name":"Work/Done"}, "PATCH", "/folders/13", {"name":"Work/Done"}, 200, {"id":13,"name":"Work/Done"}),
    ("move_message", {"folder_id":13,"message_id":"m/1"}, "PUT", "/messages/m%2F1/folder", {"folderId":13}, 204, None),
    ("restore_message", {"message_id":"m/1"}, "POST", "/messages/m%2F1/restore", None, 204, None),
    ("delete_folder", {"folder_id":13}, "DELETE", "/folders/13", None, 204, None),
    ("list_scheduled_sends", {"statuses":["scheduled","failed"]}, "GET", "/scheduled-sends?status=scheduled%2Cfailed", None, 200, {"data":[{"id":"s","status":"failed"}]}),
    ("reschedule_scheduled_send", {"send_id":"s","send_at":"2027-01-01T12:00:00Z"}, "PATCH", "/scheduled-sends/s", {"sendAt":"2027-01-01T12:00:00Z"}, 200, {"id":"s","status":"scheduled"}),
    ("cancel_scheduled_send", {"send_id":"s"}, "DELETE", "/scheduled-sends/s", None, 200, {"id":"s","status":"cancelled"}),
])
def test_mailbox_operation(monkeypatch, name, args, method, suffix, payload, status, result):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=result) if status != 204 else httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(tools, "_api", OpenMailApi("https://example.test", "key", client=client))
        monkeypatch.setattr(tools, "_inbox_id", "team/a")
        ctx = SimpleNamespace(register_tool=Mock())
        tools.register_tools(ctx)
        handlers = {call.args[0]: call.args[3] for call in ctx.register_tool.call_args_list}
        assert json.loads(handlers["openmail_" + name](args)) == (result if result is not None else {"ok": True})
    request, = requests
    assert request.method == method
    assert request.url.raw_path.decode() == "/v1/inboxes/team%2Fa" + suffix
    assert (json.loads(request.content) if request.content else None) == payload
