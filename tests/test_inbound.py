from openmail_plugin.config import OpenMailConfig, normalize_mode, read_config
from openmail_plugin.inbound import Classification, StagedMedia, build_agent_text, classify, parse_address, valid_event
from openmail_plugin.stream import ws_url


def test_parse_address():
    assert parse_address('"Ada Lovelace" <Ada@Example.com>') == ("Ada Lovelace", "ada@example.com")
    assert parse_address("Ada <ada@example.com>") == ("Ada", "ada@example.com")
    assert parse_address("ADA@example.com") == (None, "ada@example.com")


def test_classify_prefers_api_then_frame():
    c = classify({"category": None, "autoReplyable": None, "verdict": None},
                 {"category": "automated", "auto_replyable": False, "verdict": "clean"})
    assert c == Classification("automated", False, "clean")
    assert not c.rejected and not c.wants_reply
    assert classify({"verdict": "spam"}).rejected
    assert classify({}).wants_reply  # unclassified keeps today's behaviour


def test_agent_text_channel_and_notify():
    staged = StagedMedia(paths=["/tmp/a.pdf"], types=["application/pdf"], skipped=["big.zip"])
    atts = [{"filename": "a.pdf"}, {"filename": "big.zip"}, {"filename": "notes.txt", "parsedText": "hello " * 5}]
    text = build_agent_text(sender="Bob <bob@x.io>", to="me@omail.sh", subject="Hi", thread_id="thr_1",
                            message_id="msg_1", body="body", attachments=atts, staged=staged, mode="channel",
                            category="personal")
    assert "sent verbatim" in text and "From: Bob <bob@x.io>" in text and "Thread: thr_1" in text
    assert "1 attached as files" in text and "skipped, over the size cap: big.zip" in text
    assert "--- notes.txt (extracted text) ---" in text and "Category:" not in text

    notify = build_agent_text(sender="x@y.z", to=None, subject=None, thread_id="thr_2", message_id="m",
                              body="b", attachments=[], staged=StagedMedia(), mode="notify", inbox_id="inb_9",
                              category="marketing")
    assert "Do not act on it" in notify and "thread_id=thr_2, inbox_id=inb_9" in notify
    assert "Inbox: inb_9" in notify and "Category: marketing" in notify


def test_valid_event():
    good = {"event": "message.received", "event_id": "e1", "thread_id": "t", "message": {"id": "m"}}
    assert valid_event(good)
    assert not valid_event({**good, "event": "message.sent"})
    assert not valid_event({**good, "message": {}})


def test_ws_url():
    assert ws_url("https://api.openmail.sh/") == "wss://api.openmail.sh/v1/ws"
    assert ws_url("http://localhost:3000") == "ws://localhost:3000/v1/ws"


def test_config_env_wins_over_extra(monkeypatch):
    monkeypatch.setenv("OPENMAIL_API_KEY", "om_env")
    monkeypatch.setenv("OPENMAIL_MODE", "bogus")
    cfg = read_config({"api_key": "om_yaml", "mode": "notify", "pod_id": "pod_1",
                       "inboxes": {"inb_1": {"mode": "tool"}, "Sales@omail.sh": {"mode": "notify"}}})
    assert cfg.api_key == "om_env" and cfg.mode == "channel" and cfg.pod_id == "pod_1"
    assert cfg.inbox_mode("inb_1") == "tool"
    assert cfg.inbox_mode("inb_2", "sales@omail.sh") == "notify"
    assert cfg.inbox_mode("inb_3", "other@omail.sh") == "channel"
    assert normalize_mode("NOTIFY") == "notify"

