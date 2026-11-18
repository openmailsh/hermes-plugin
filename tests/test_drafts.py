import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from openmail_plugin import tools
from openmail_plugin.api import OpenMailApi, OpenMailApiError


@pytest.fixture
def draft_api(monkeypatch):
    requests = []
    version = "2026-11-06T10:00:00.123Z"

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/send"):
            body = json.loads(request.content)
            if body["updatedAt"] != version:
                return httpx.Response(409, json={"error": "draft_changed", "message": "This draft changed. Refresh before continuing."})
            if body.get("inboxId", "i/a") != "i/a":
                return httpx.Response(400, json={"error": "draft_inbox_locked", "message": "A draft must use its original sending inbox."})
            return httpx.Response(200, json={"messageId": "msg-1", "threadId": "thread-1", "status": "sent"})
        if request.url.params:
            return httpx.Response(200, json={"data": [], "total": 3})
        if request.method == "GET":
            return httpx.Response(200, json={
                "id": "draft-1", "updatedAt": version, "inboxId": "i/a", "threadId": "thread-1",
                "sourceThreadId": "thread-1", "attachments": [{"filename": "report.pdf", "contentType": "application/pdf", "sizeBytes": 4,
                                 "url": "https://storage.example.invalid/signed/report.pdf"}],
            })
        return httpx.Response(201, json={"id": "draft-1", "updatedAt": version})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    api = OpenMailApi("https://example.invalid", "om_test", client=client)
    monkeypatch.setattr(tools, "_api", api)
    monkeypatch.setattr(tools, "_inbox_id", "default-inbox")
    yield api, requests, version
    client.close()


def test_create_preserves_content_and_never_sends(draft_api):
    api, requests, _ = draft_api
    tools.openmail_create_draft({"inbox_id": "chosen", "to": "a@example.com", "body": " Plain ", "body_html": "<p>HTML</p>",
                                 "cc": ["c@example.com"], "bcc": [{"email": "b@example.com"}]}, api)
    tools.openmail_create_draft({"inbox_id": "other"}, api)
    assert [r.method for r in requests] == ["POST", "POST"]
    assert all(r.url.path == "/v1/drafts" for r in requests)
    assert json.loads(requests[0].content) == {"inboxId": "chosen", "to": "a@example.com", "body": " Plain ", "bodyHtml": "<p>HTML</p>",
                                               "cc": ["c@example.com"], "bcc": ["b@example.com"]}
    assert json.loads(requests[1].content) == {"inboxId": "other"}


def test_search_pagination_and_combined_filters(draft_api):
    api, requests, _ = draft_api
    result = tools.openmail_list_drafts({"inbox_id": "other", "pod_id": "pod", "source_thread_id": "thr/1",
                                       "query": "a & b", "limit": 5, "offset": 2}, api)
    assert result["total"] == 3
    assert requests[0].url.path == "/v1/drafts"
    assert dict(requests[0].url.params) == {"q": "a & b", "limit": "5", "offset": "2", "inboxId": "other",
                                           "podId": "pod", "sourceThreadId": "thr/1"}


def test_get_and_send_use_exact_version_and_encoded_ids(draft_api):
    api, requests, version = draft_api
    args = {"inbox_id": "i/a", "draft_id": "d/b"}
    draft = tools.openmail_get_draft(args, api)
    assert draft["updatedAt"] == version
    assert draft["threadId"] == draft["sourceThreadId"] == "thread-1"
    assert draft["attachments"] == [{"filename": "report.pdf", "contentType": "application/pdf", "sizeBytes": 4,
                                    "url": "https://storage.example.invalid/signed/report.pdf"}]
    result = tools.openmail_send_draft({**args, "updated_at": version}, api)
    assert result["status"] == "sent"
    assert requests[0].url.raw_path == b"/v1/drafts/d%2Fb"
    assert requests[1].url.raw_path == b"/v1/drafts/d%2Fb/send"
    assert json.loads(requests[1].content) == {"updatedAt": version, "inboxId": "i/a"}
    assert requests[1].headers["authorization"] == "Bearer om_test"


def test_stale_send_surfaces_error_without_fetching_or_retrying(draft_api):
    _, requests, _ = draft_api
    result = json.loads(tools._tool(tools.openmail_send_draft)({"draft_id": "d", "updated_at": "2020-01-01T00:00:00Z"}))
    assert result["status"] == 409
    assert result["code"] == "draft_changed"
    assert "Do not send automatically" in result["error"]
    assert "obtain approval" in result["error"]
    assert "Refresh before continuing" not in result["error"]
    assert len(requests) == 1


@pytest.mark.parametrize("status, code, message, guidance", [
    (409, "draft_changed", "This draft changed. Refresh before continuing.", "A failed send can also advance updatedAt"),
    (502, "send_failed", "Sending failed. Your draft is saved. Refresh and try again.", "obtain approval to retry"),
    (409, "draft_sending", "Sending is still being processed. Refresh and try again shortly.", "Wait and check the inbox"),
])
def test_send_errors_explain_next_steps_without_retrying(monkeypatch, status, code, message, guidance):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": code, "message": message})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = OpenMailApi("https://example.invalid", "om_test", client=client)
        monkeypatch.setattr(tools, "_api", api)
        version = "2026-11-06T10:00:00.123Z"
        result = json.loads(tools._tool(tools.openmail_send_draft)({"inbox_id": "i", "draft_id": "d", "updated_at": version}))
    assert result["status"] == status
    assert result["code"] == code
    assert guidance in result["error"]
    assert "Refresh" not in result["error"]
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/drafts/d/send"
    assert json.loads(requests[0].content) == {"updatedAt": version, "inboxId": "i"}


@pytest.mark.parametrize("body, code", [
    ({"error": "recipient_not_permitted", "code": "legacy"}, "recipient_not_permitted"),
    ({"code": "legacy"}, "legacy"),
])
def test_error_codes_preserve_api_field_and_legacy_fallback(body, code):
    result = json.loads(tools._err(OpenMailApiError("Original error", 403, body)))
    assert result == {"error": "Original error", "status": 403, "code": code}


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


def test_lists_without_implicitly_selecting_default_inbox(draft_api):
    api, requests, _ = draft_api
    tools.openmail_list_drafts({}, api)
    assert requests[0].url.path == "/v1/drafts"
    assert dict(requests[0].url.params) == {"limit": "20", "offset": "0"}


@pytest.mark.parametrize("args", [{}, {"inbox_id": ""}, {"inbox_id": "   "}])
def test_create_requires_explicit_owner_even_with_default_inbox(draft_api, args):
    api, requests, _ = draft_api
    schema = next(s for n, _, s, _ in tools.TOOLS if n == "openmail_create_draft")
    assert schema["parameters"]["required"] == ["inbox_id"]
    with pytest.raises(ValueError, match="inbox_id"):
        tools.openmail_create_draft(args, api)
    assert requests == []


@pytest.mark.parametrize("context, payload", [
    ({"thread_id": "thread"}, {"threadId": "thread"}),
    ({"source_thread_id": "thread"}, {"sourceThreadId": "thread"}),
])
def test_create_keeps_reply_and_forward_context_distinct(draft_api, context, payload):
    api, requests, _ = draft_api
    tools.openmail_create_draft({"inbox_id": "owner", **context}, api)
    assert json.loads(requests[0].content) == {"inboxId": "owner", **payload}


def test_draft_send_uses_stored_inbox_and_rejects_mismatched_assertion(draft_api):
    api, requests, version = draft_api
    args = {"draft_id": "draft", "updated_at": version}
    tools.openmail_send_draft(args, api)
    assert json.loads(requests[0].content) == {"updatedAt": version}
    mismatch = json.loads(tools._tool(tools.openmail_send_draft)({**args, "inbox_id": "other"}))
    assert (mismatch["status"], mismatch["code"]) == (400, "draft_inbox_locked")
    assert len(requests) == 2


def test_create_uploads_files_and_inline_images(draft_api, monkeypatch, tmp_path):
    api, requests, _ = draft_api
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    report = tmp_path / "output" / "report.pdf"
    image = tmp_path / "media" / "plot.png"
    for path, content in [(report, b"%PDF-report"), (image, b"image bytes")]:
        path.parent.mkdir()
        path.write_bytes(content)
    cid = "inline-66ae30db-b7a8-4048-a103-af5d7990be89@openmail"
    html = f'<p>Report</p><img src="cid:{cid}">'
    tools.openmail_create_draft({
        "inbox_id": "owner", "source_thread_id": "thread", "body_html": html,
        "cc": ["one@example.com", "two@example.com"], "attachments": [str(report)],
        "inline_images": [{"path": str(image), "content_id": cid}],
    }, api)

    request, = requests
    assert (request.method, request.url.path) == ("POST", "/v1/drafts")
    headers = f'Content-Type: {request.headers["content-type"]}\r\n\r\n'.encode()
    message = BytesParser(policy=policy.default).parsebytes(headers + request.content)
    fields = []
    files = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_filename() is None:
            fields.append((name, part.get_content()))
        else:
            files.append((name, part.get_filename(), part.get_content_type(), part.get_payload(decode=True)))
    assert sorted(fields) == sorted([("inboxId", "owner"), ("bodyHtml", html), ("sourceThreadId", "thread"),
                                     ("cc", "one@example.com"), ("cc", "two@example.com")])
    assert files == [("attachments", "report.pdf", "application/pdf", report.read_bytes()),
                     ("inlineImages", cid, "image/png", image.read_bytes())]


@pytest.mark.parametrize("inline", [False, True])
def test_draft_uploads_respect_hermes_attachment_directories(draft_api, monkeypatch, tmp_path, inline):
    api, requests, _ = draft_api
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = str(tmp_path / "secrets")
    uploads = {"inline_images": [{"path": path, "content_id": "example"}]} if inline else {"attachments": [path]}
    with pytest.raises(ValueError, match="attachments must live under"):
        tools.openmail_create_draft({"inbox_id": "owner", **uploads}, api)
    assert requests == []
