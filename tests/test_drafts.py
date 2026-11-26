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
        if request.url.path.endswith("/send") or request.method in ("PATCH", "DELETE"):
            body = json.loads(request.content)
            if body["updatedAt"] != version:
                return httpx.Response(409, json={"error": "draft_changed", "message": "This draft changed. Refresh before continuing."})
            if body.get("inboxId", "i/a") != "i/a":
                return httpx.Response(400, json={"error": "draft_inbox_locked", "message": "A draft must use its original sending inbox."})
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": "draft-1", **body, "updatedAt": "2026-11-18T10:00:00.000Z"})

            if request.method == "DELETE":
                return httpx.Response(200, json={"id": "draft-1", "deleted": True})
            if "sendAt" in body:
                return httpx.Response(202, json={"id": "scheduled-1", "status": "scheduled", "sendAt": body["sendAt"]})
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


def test_delete_uses_exact_version_and_encoded_id_without_fetch_or_send(draft_api):
    api, requests, version = draft_api
    result = tools.openmail_delete_draft({"draft_id": "d/a", "updated_at": version, "inbox_id": "i/a"}, api)
    assert result == {"id": "draft-1", "deleted": True}
    request, = requests
    assert request.method == "DELETE"
    assert request.url.raw_path == b"/v1/drafts/d%2Fa"
    assert json.loads(request.content) == {"updatedAt": version, "inboxId": "i/a"}


def test_delete_does_not_substitute_default_inbox_or_stale_version(draft_api):
    _, requests, _ = draft_api
    result = json.loads(tools._tool(tools.openmail_delete_draft)({"draft_id": "d", "updated_at": "2020-01-01T00:00:00Z"}))
    assert (result["status"], result["code"]) == (409, "draft_changed")
    assert "retry deletion" in result["error"]
    assert "approval before sending" not in result["error"]
    request, = requests
    assert json.loads(request.content) == {"updatedAt": "2020-01-01T00:00:00Z"}


@pytest.mark.parametrize("status, code", [(403, "forbidden"), (404, "not_found"), (400, "draft_inbox_locked"), (409, "draft_sending")])
def test_delete_preserves_failures_and_never_retries(monkeypatch, status, code):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": code, "message": code})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(tools, "_api", OpenMailApi("https://example.invalid", "om_test", client=client))
        result = json.loads(tools._tool(tools.openmail_delete_draft)({"draft_id": "d", "updated_at": "2026-11-18T10:00:00.000Z"}))
    assert (result["status"], result["code"]) == (status, code)
    assert len(requests) == 1 and requests[0].method == "DELETE"
    if code == "draft_sending":
        assert "does not cancel a send" in result["error"]


@pytest.mark.parametrize("args", [{}, {"draft_id": "d"}, {"draft_id": "d", "updated_at": ""},
    {"draft_id": "d", "updated_at": "v", "body_html": "Edit"}, {"draft_id": "d", "updated_at": "v", "inbox_id": ""}])
def test_invalid_deletion_never_touches_api(draft_api, args):
    api, requests, _ = draft_api
    with pytest.raises(ValueError):
        tools.openmail_delete_draft(args, api)
    assert requests == []


def test_create_preserves_content_and_never_sends(draft_api):
    api, requests, _ = draft_api
    tools.openmail_create_draft({"inbox_id": "chosen", "to": "a@example.com", "body_html": "<p>HTML</p>",
                                 "cc": ["c@example.com"], "bcc": [{"email": "b@example.com"}]}, api)
    tools.openmail_create_draft({"inbox_id": "other"}, api)
    assert [r.method for r in requests] == ["POST", "POST"]
    assert all(r.url.path == "/v1/drafts" for r in requests)
    assert json.loads(requests[0].content) == {"inboxId": "chosen", "to": "a@example.com", "bodyHtml": "<p>HTML</p>",
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


def test_schedule_reviewed_draft(draft_api):
    api, requests, version = draft_api
    args = {"draft_id": "d/b", "updated_at": version}
    for invalid in ("", None, "tomorrow", "2026-12-01T10:00:00"):
        with pytest.raises(ValueError, match="send_at"):
            tools.openmail_send_draft({**args, "send_at": invalid}, api)
    assert not requests
    send_at = "2026-12-01T10:00:00Z"
    result = tools.openmail_send_draft({**args, "send_at": send_at}, api)
    assert result == {"id": "scheduled-1", "status": "scheduled", "sendAt": send_at}
    assert json.loads(requests[0].content) == {"updatedAt": version, "sendAt": send_at}


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
    recipients = ["sam@example.com", "alex@example.com"]
    tools.openmail_create_draft({
        "inbox_id": "owner", "source_thread_id": "thread", "body_html": html, "to": recipients,
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
    assert [v for key, v in fields if key == "to"] == recipients
    assert sorted((k, v) for k, v in fields if k != "to") == sorted([("inboxId", "owner"), ("bodyHtml", html), ("sourceThreadId", "thread"),
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


@pytest.mark.parametrize("handler", [tools.openmail_create_draft, tools.openmail_edit_draft])
@pytest.mark.parametrize("fields", [{"body": "Plain"}, {"body_text": "Plain"}, {"bodyText": "Plain"}, {"body_html": None}])
def test_create_and_edit_reject_non_html_body_inputs(draft_api, handler, fields):
    api, requests, version = draft_api
    with pytest.raises(ValueError, match="body_html"):
        handler({"inbox_id": "i/a", "draft_id": "d", "updated_at": version, **fields}, api)
    assert requests == []


def test_edit_preserves_exact_version_html_and_explicit_clears_without_sending(draft_api):
    api, requests, version = draft_api
    result = tools.openmail_edit_draft({"draft_id": "d/b", "updated_at": version, "inbox_id": "i/a",
        "to": None, "cc": [], "bcc": [{"email": "private@example.com"}], "subject": "", "body_html": "<p>Revised</p>"}, api)
    request, = requests
    assert request.method == "PATCH"
    assert request.url.raw_path == b"/v1/drafts/d%2Fb"
    assert json.loads(request.content) == {"updatedAt": version, "inboxId": "i/a", "to": None,
        "cc": [], "bcc": ["private@example.com"], "subject": "", "bodyHtml": "<p>Revised</p>"}
    assert result["updatedAt"] == "2026-11-18T10:00:00.000Z"


def test_edit_omits_unchanged_fields_and_can_clear_html(draft_api):
    api, requests, version = draft_api
    tools.openmail_edit_draft({"draft_id": "d", "updated_at": version, "body_html": ""}, api)
    assert json.loads(requests[0].content) == {"updatedAt": version, "bodyHtml": ""}


def test_draft_recipients_can_be_created_replaced_and_cleared(draft_api):
    api, requests, version = draft_api
    recipients = ["sam@example.com", "alex@example.com"]
    tools.openmail_create_draft({"inbox_id": "owner", "to": recipients}, api)
    for addresses in (recipients[::-1], []):
        tools.openmail_edit_draft({"draft_id": "d", "updated_at": version, "to": addresses}, api)
    assert [json.loads(r.content)["to"] for r in requests] == [recipients, recipients[::-1], []]


def test_stale_edit_requires_reconciliation_without_fetching_or_retrying(draft_api):
    _, requests, _ = draft_api
    result = json.loads(tools._tool(tools.openmail_edit_draft)({"draft_id": "d", "updated_at": "2020-01-01T00:00:00Z", "body_html": "<p>Edit</p>"}))
    assert (result["status"], result["code"]) == (409, "draft_changed")
    assert "reconcile the requested changes" in result["error"]
    assert "before sending" not in result["error"]
    assert len(requests) == 1 and requests[0].method == "PATCH"


@pytest.mark.parametrize("status, code", [(403, "forbidden"), (409, "draft_sending"), (404, "not_found")])
def test_edit_preserves_access_and_send_errors_without_retrying(monkeypatch, status, code):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": code, "message": code})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(tools, "_api", OpenMailApi("https://example.invalid", "om_test", client=client))
        result = json.loads(tools._tool(tools.openmail_edit_draft)({"draft_id": "d", "updated_at": "2026-11-06T10:00:00.123Z", "subject": "Edit"}))
    assert (result["status"], result["code"]) == (status, code)
    assert len(requests) == 1 and requests[0].method == "PATCH"


@pytest.mark.parametrize("args", [{}, {"draft_id": "d"}, {"draft_id": "d", "updated_at": "v"},
    {"draft_id": "d", "updated_at": "v", "subject": "Edit", "thread_id": "other"},
    {"draft_id": "d", "updated_at": "v", "subject": "Edit", "inbox_id": ""}])
def test_invalid_edits_never_touch_api(draft_api, args):
    api, requests, _ = draft_api
    with pytest.raises(ValueError):
        tools.openmail_edit_draft(args, api)
    assert requests == []


def test_edit_ownership_check_is_forwarded_without_default_sender_substitution(draft_api):
    _, requests, version = draft_api
    result = json.loads(tools._tool(tools.openmail_edit_draft)({"draft_id": "d", "updated_at": version, "inbox_id": "other", "subject": "Edit"}))
    assert (result["status"], result["code"]) == (400, "draft_inbox_locked")
    assert len(requests) == 1
    assert json.loads(requests[0].content)["inboxId"] == "other"


@pytest.mark.parametrize("name", ["openmail_create_draft", "openmail_edit_draft"])
def test_draft_schemas_expose_html_only(name):
    schema = next(s for n, _, s, _ in tools.TOOLS if n == name)["parameters"]
    assert "body_html" in schema["properties"]
    assert "body" not in schema["properties"]
    if name == "openmail_edit_draft":
        assert schema["required"] == ["draft_id", "updated_at"]
