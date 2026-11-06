import json

import httpx
import pytest

from openmail_plugin import tools
from openmail_plugin.api import OpenMailApi


@pytest.fixture
def draft_api(monkeypatch):
    requests = []
    version = "2026-11-06T10:00:00.123Z"

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/send"):
            body = json.loads(request.content)
            if body["updatedAt"] != version:
                return httpx.Response(409, json={"error": "draft_changed", "message": "Review the changed draft"})
            return httpx.Response(200, json={"messageId": "msg-1", "threadId": "thread-1", "status": "sent"})
        if request.url.params:
            return httpx.Response(200, json={"data": [], "total": 3})
        return httpx.Response(200, json={"id": "draft-1", "updatedAt": version})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    api = OpenMailApi("https://example.invalid", "om_test", client=client)
    monkeypatch.setattr(tools, "_api", api)
    monkeypatch.setattr(tools, "_inbox_id", "default-inbox")
    yield api, requests, version
    client.close()


def test_create_preserves_content_and_never_sends(draft_api):
    api, requests, _ = draft_api
    tools.openmail_create_draft({"to": "a@example.com", "body": " Plain ", "body_html": "<p>HTML</p>",
                                 "cc": ["c@example.com"], "bcc": [{"email": "b@example.com"}]}, api)
    tools.openmail_create_draft({}, api)
    assert [r.method for r in requests] == ["POST", "POST"]
    assert all(r.url.path == "/v1/inboxes/default-inbox/drafts" for r in requests)
    assert json.loads(requests[0].content) == {"to": "a@example.com", "body": " Plain ", "bodyHtml": "<p>HTML</p>",
                                               "cc": ["c@example.com"], "bcc": ["b@example.com"]}
    assert json.loads(requests[1].content) == {}


def test_search_pagination_and_explicit_inbox(draft_api):
    api, requests, _ = draft_api
    result = tools.openmail_list_drafts({"inbox_id": "other", "query": "a & b", "limit": 5, "offset": 2}, api)
    assert result["total"] == 3
    assert requests[0].url.path == "/v1/inboxes/other/drafts"
    assert dict(requests[0].url.params) == {"q": "a & b", "limit": "5", "offset": "2"}


def test_get_and_send_use_exact_version_and_encoded_ids(draft_api):
    api, requests, version = draft_api
    args = {"inbox_id": "i/a", "draft_id": "d/b"}
    assert tools.openmail_get_draft(args, api)["updatedAt"] == version
    result = tools.openmail_send_draft({**args, "updated_at": version}, api)
    assert result["status"] == "sent"
    assert requests[0].url.raw_path == b"/v1/inboxes/i%2Fa/drafts/d%2Fb"
    assert requests[1].url.raw_path == b"/v1/inboxes/i%2Fa/drafts/d%2Fb/send"
    assert json.loads(requests[1].content) == {"updatedAt": version}
    assert requests[1].headers["authorization"] == "Bearer om_test"


def test_stale_send_surfaces_error_without_fetching_or_retrying(draft_api):
    _, requests, _ = draft_api
    result = json.loads(tools._tool(tools.openmail_send_draft)({"draft_id": "d", "updated_at": "2020-01-01T00:00:00Z"}))
    assert result["status"] == 409
    assert "Review" in result["error"]
    assert len(requests) == 1


def test_missing_version_and_invalid_pagination_never_touch_api(draft_api):
    api, requests, _ = draft_api
    with pytest.raises(ValueError, match="updated_at"):
        tools.openmail_send_draft({"draft_id": "d"}, api)
    with pytest.raises(ValueError, match="draft_id"):
        tools.openmail_get_draft({}, api)
    for args in [{"limit": 0}, {"limit": 101}, {"offset": -1}]:
        with pytest.raises(ValueError):
            tools.openmail_list_drafts(args, api)
    assert requests == []


def test_registered_send_requires_reviewed_version():
    schema = next(s for n, _, s, _ in tools.TOOLS if n == "openmail_send_draft")
    assert schema["parameters"]["required"] == ["draft_id", "updated_at"]
