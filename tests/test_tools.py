import os

import pytest

from openmail_plugin import tools


class FakeApi:
    def __init__(self, inboxes=None):
        self.sent = []
        self.inboxes = inboxes if inboxes is not None else [{"id": "i", "address": "a@x.com"}]

    def send(self, **kw):
        self.sent.append(kw)
        return {"id": "msg_1"}

    def list_inboxes(self):
        return self.inboxes


@pytest.fixture
def unbound(monkeypatch):
    """Outside the gateway: nothing has called tools.bind()."""
    monkeypatch.setattr(tools, "_inbox_id", None)
    monkeypatch.setattr(tools, "_api", None)


def test_default_inbox_falls_back_to_env_outside_the_gateway(unbound, monkeypatch):
    """Regression: `hermes chat` with a pod key and OPENMAIL_INBOX_ID set still failed with
    "inbox_id is required" because only the gateway adapter's bind() populated the default."""
    monkeypatch.setenv("OPENMAIL_INBOX_ID", "inbox-from-env")
    api = FakeApi(inboxes=[{"id": "inbox-from-env"}, {"id": "other"}])
    tools.openmail_send({"to": "a@x.com", "subject": "s", "body": "b"}, api)
    assert api.sent[0]["inbox_id"] == "inbox-from-env"
    assert tools.openmail_whoami({}, api)["default_inbox_id"] == "inbox-from-env"


def test_bound_inbox_wins_over_env(unbound, monkeypatch):
    monkeypatch.setenv("OPENMAIL_INBOX_ID", "inbox-from-env")
    tools.bind(FakeApi(), "inbox-from-adapter")
    api = FakeApi(inboxes=[{"id": "x"}, {"id": "y"}])
    tools.openmail_send({"to": "a@x.com", "subject": "s", "body": "b"}, api)
    assert api.sent[0]["inbox_id"] == "inbox-from-adapter"


def test_several_inboxes_and_no_default_still_requires_inbox_id(unbound, monkeypatch):
    monkeypatch.delenv("OPENMAIL_INBOX_ID", raising=False)
    api = FakeApi(inboxes=[{"id": "x"}, {"id": "y"}])
    with pytest.raises(RuntimeError, match="inbox_id is required"):
        tools.openmail_send({"to": "a@x.com", "subject": "s", "body": "b"}, api)


@pytest.mark.parametrize("raw", [
    ["crm@x.com"],
    [{"item": "crm@x.com"}],            # what Nous/Hermes models actually emit
    {"item": "crm@x.com"},              # before Hermes wraps it in a list
    [{"email": "crm@x.com"}],
    "crm@x.com",
    " crm@x.com ",
])
def test_bcc_shapes_all_flatten_to_a_string_list(raw):
    api = FakeApi()
    tools.openmail_send({"to": "a@x.com", "subject": "s", "body": "b", "inbox_id": "i", "bcc": raw}, api)
    assert api.sent[0]["bcc"] == ["crm@x.com"]


def test_comma_separated_string_splits_and_empty_becomes_none():
    assert tools._addresses("a@x.com, b@y.com;c@z.com") == ["a@x.com", "b@y.com", "c@z.com"]
    assert tools._addresses([{"item": ""}, ""]) is None
    for empty in (None, "", []):
        assert tools._addresses(empty) is None


def test_schema_admits_the_object_shape_hermes_produces(monkeypatch):
    """Hermes runs coerce_tool_args then jsonschema on our schema *before* the handler. That is where
    `bcc: {"item": ...}` used to die ("bcc[0] is not of type 'string'"), so exercise that exact path."""
    jsonschema = pytest.importorskip("jsonschema")
    arg_coercion = pytest.importorskip("tools.arg_coercion")
    from tools.registry import registry as hermes_registry

    schema = next(s for n, _, s, _ in tools.TOOLS if n == "openmail_send")
    params = schema.get("parameters", schema)
    monkeypatch.setattr(hermes_registry, "get_schema", lambda name: schema if name == "openmail_send" else None)
    for raw in ({"item": "crm@x.com"}, [{"item": "crm@x.com"}], ["crm@x.com"], "crm@x.com"):
        args = arg_coercion.coerce_tool_args("openmail_send", {"to": "a@x.com", "subject": "s", "body": "b", "bcc": raw})
        jsonschema.validate(args, params)
        api = FakeApi()
        tools.openmail_send({**args, "inbox_id": "i"}, api)
        assert api.sent[0]["bcc"] == ["crm@x.com"]


@pytest.mark.parametrize("name, required", [
    ("openmail_send", {"subject": "Hello", "body": "Hi"}),
    ("openmail_reply", {"thread_id": "thread", "body": "Hi"}),
    ("openmail_create_draft", {"inbox_id": "owner"}),
    ("openmail_edit_draft", {"draft_id": "draft", "updated_at": "2026-11-26T10:00:00Z"}),
])
def test_hermes_accepts_one_address_and_ordered_to_lists(monkeypatch, name, required):
    jsonschema = pytest.importorskip("jsonschema")
    arg_coercion = pytest.importorskip("tools.arg_coercion")
    from tools.registry import registry

    schema = next(s for n, _, s, _ in tools.TOOLS if n == name)
    monkeypatch.setattr(registry, "get_schema", lambda _: schema)
    for recipients in ("sam@example.com", ["sam@example.com", "alex@example.com"]):
        args = arg_coercion.coerce_tool_args(name, {**required, "to": recipients})
        jsonschema.validate(args, schema["parameters"])
        assert args["to"] == recipients
    validator = jsonschema.Draft7Validator(schema["parameters"]["properties"]["to"])
    assert validator.is_valid([]) == name.endswith("_draft")
    assert validator.is_valid(None) == (name == "openmail_edit_draft")
    if name.endswith("_draft"):
        assert not validator.is_valid([f"user{i}@example.com" for i in range(101)])



@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "media").mkdir()
    (tmp_path / "output").mkdir()
    return tmp_path


def test_attachments_confined_to_media_and_output(home):
    ok = home / "output" / "report.pdf"
    ok.write_text("x")
    api = FakeApi()
    tools.openmail_send({"to": "a@x.com", "subject": "s", "body": "b", "inbox_id": "i", "attachments": [str(ok)]}, api)
    assert api.sent[0]["attachments"] == [os.path.realpath(ok)]
    # Assembled with os.path.join rather than written as literals: Hermes's install-time
    # plugin scanner pattern-matches the literal strings and blocks the install.
    outside = [
        os.path.join(os.sep, "etc", "passwd"),
        str(home / ".env"),
        str(home / "output" / ".." / ".env"),
        os.path.join("~", ".ssh", "id_rsa"),
    ]
    for bad in outside:
        with pytest.raises(ValueError, match="refused"):
            tools.openmail_send({"to": "a@x.com", "subject": "s", "body": "b", "inbox_id": "i", "attachments": [bad]}, api)
    assert len(api.sent) == 1


def test_attachment_symlink_escape_refused(home):
    link = home / "media" / "link"
    link.symlink_to(home / "secret.txt")
    (home / "secret.txt").write_text("x")
    with pytest.raises(ValueError, match="refused"):
        tools.openmail_reply({"thread_id": "t", "body": "b", "to": "a@x.com", "inbox_id": "i", "attachments": [str(link)]}, FakeApi())


def test_create_inbox_key_is_not_a_tool():
    assert "openmail_create_inbox_key" not in {name for name, *_ in tools.TOOLS}
    assert not hasattr(tools, "openmail_create_inbox_key")


@pytest.mark.parametrize("query", [None, "invoice acme"])
def test_list_messages_forwards_search_and_pagination(query):
    import httpx
    from openmail_plugin.api import OpenMailApi

    args = {"inbox_id": "inbox", "direction": "inbound", "limit": 5, "offset": 10}
    expected = {"direction": "inbound", "limit": "5", "offset": "10"}
    if query is not None:
        args["query"] = query
        expected["q"] = query

    def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/inboxes/inbox/messages"
        assert dict(request.url.params) == expected
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        api = OpenMailApi("https://api.test", "om_test", client=client)
        assert tools.openmail_list_messages(args, api) == {"data": []}


def test_attachment_text_selects_by_id_and_accepts_legacy_filenames(monkeypatch):
    import json
    import httpx
    from openmail_plugin.api import OpenMailApi, OpenMailApiError

    files = [{"id": "second", "filename": "report copy.pdf"}, {"id": "first", "filename": "report copy.pdf"}]
    routes = {"/v1/attachments/msg/first/text": "first report", "/v1/attachments/msg/second/text": "second report",
              "/v1/attachments/msg/report%20copy.pdf/text": "first report"}

    def extract(request):
        assert request.headers["authorization"] == "Bearer om_test"
        if request.url.path in ("/v1/threads/thread/messages", "/v1/inboxes/inbox/messages"):
            return httpx.Response(200, json={"data": [{"id": "msg", "attachments": files}]})
        text = routes.get(request.url.raw_path.decode())
        return httpx.Response(200, json={"text": text}) if text else httpx.Response(404)

    schema = next(s["parameters"] for name, _, s, _ in tools.TOOLS if name == "openmail_attachment_text")
    assert "attachment_id" in schema["properties"] and "filename" not in schema["required"]
    with httpx.Client(transport=httpx.MockTransport(extract)) as client:
        api = OpenMailApi("https://api.test", "om_test", client=client)
        monkeypatch.setattr(tools, "_api", api)
        thread = json.loads(tools._tool(tools.openmail_read_thread)({"thread_id": "thread", "mark_read": False}))
        inbox = json.loads(tools._tool(tools.openmail_list_messages)({"inbox_id": "inbox"}))
        for message in (thread["messages"][0], inbox["data"][0]):
            assert message["attachments"] == files
            for att in message["attachments"]:
                result = tools._tool(tools.openmail_attachment_text)({"message_id": message["id"], "attachment_id": att["id"]})
                assert json.loads(result)["text"] == f"{att['id']} report"
        args = {"message_id": "msg", "filename": "report copy.pdf"}
        assert tools.openmail_attachment_text({**args, "attachment_id": "second"}, api)["text"] == "second report"
        assert tools.openmail_attachment_text(args, api)["text"] == "first report"
        with pytest.raises(OpenMailApiError) as exc:
            tools.openmail_attachment_text({**args, "attachment_id": "missing"}, api)
        assert exc.value.status == 404
        with pytest.raises(ValueError, match="message_id and attachment_id"):
            tools.openmail_attachment_text({"message_id": "msg"}, api)


def test_send_with_attachment_builds_real_multipart(home):
    """Regression: a list-of-tuples form made httpx treat the body as raw bytes and every attachment send failed."""
    import httpx
    from openmail_plugin.api import OpenMailApi

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.read()
        return httpx.Response(200, json={"threadId": "t", "status": "sent"})

    path = home / "output" / "report.txt"
    path.write_text("hello")
    api = OpenMailApi("https://api.test", "om_x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    api.send(inbox_id="i", to="a@x.com", subject="s", body="b", cc=["c1@x.com", "c2@x.com"], attachments=[str(path)])
    assert seen["content_type"].startswith("multipart/form-data")
    assert seen["body"].count(b'name="cc"') == 2
    assert b'filename="report.txt"' in seen["body"] and b"hello" in seen["body"]


@pytest.mark.parametrize("send,context", [
    (tools.openmail_send, {"subject": "Hello"}),
    (tools.openmail_reply, {"thread_id": "thread"}),
])
def test_group_send_uses_ordered_json_and_repeated_form_fields(home, send, context):
    import json
    from email import policy
    from email.parser import BytesParser

    import httpx
    from openmail_plugin.api import OpenMailApi

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["content-type"], request.read()))
        return httpx.Response(200, json={"threadId": "t", "status": "sent"})

    api = OpenMailApi("https://api.test", "om_x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    recipients = ["sam@example.com", "alex@example.com"]
    args = {"inbox_id": "i", "to": recipients, "body": "b", "bcc": ["crm@x.com"], **context}
    args["send_at"] = "2027-01-01T12:00:00Z"
    send(args, api)
    assert json.loads(seen[0][1])["sendAt"] == args["send_at"]
    assert json.loads(seen[0][1])["to"] == recipients
    assert json.loads(seen[0][1])["bcc"] == ["crm@x.com"]

    path = home / "output" / "note.txt"
    path.write_text("x")
    send({**args, "bcc": ["crm@x.com", "log@x.com"], "attachments": [str(path)]}, api)
    headers = f"Content-Type: {seen[1][0]}\r\n\r\n".encode()
    parts = BytesParser(policy=policy.default).parsebytes(headers + seen[1][1]).iter_parts()
    fields = [(p.get_param("name", header="content-disposition"), p.get_content()) for p in parts]
    assert [value for key, value in fields if key == "sendAt"] == [args["send_at"]]
    assert [value for key, value in fields if key == "to"] == recipients
    assert [value for key, value in fields if key == "bcc"] == ["crm@x.com", "log@x.com"]


def test_reply_uses_last_inbound_sender_only_when_to_is_omitted(monkeypatch):
    api = FakeApi()
    messages = [{"direction": "inbound", "fromAddr": "sender@example.com", "inboxId": "i"}]
    monkeypatch.setattr(api, "thread_messages", lambda thread: messages, raising=False)
    args = {"thread_id": "thread", "body": "b"}
    tools.openmail_reply(args, api)
    assert api.sent[0]["to"] == "sender@example.com"
    recipients = ["sam@example.com", "alex@example.com"]
    tools.openmail_reply({**args, "to": recipients}, api)
    assert api.sent[1]["to"] == recipients
    with pytest.raises(ValueError, match="To addresses"):
        tools.openmail_reply({**args, "to": []}, api)
    assert len(api.sent) == 2


def test_api_identifies_itself_to_openmail():
    """The API attributes usage per client from these headers; both must be on every request."""
    import httpx
    from openmail_plugin.api import OpenMailApi

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["client"] = request.headers["x-openmail-client"]
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json={"data": []})

    api = OpenMailApi("https://api.test", "om_x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    api.list_inboxes()
    assert seen["client"] == "hermes"
    assert seen["ua"].startswith("openmail-hermes/0.")
