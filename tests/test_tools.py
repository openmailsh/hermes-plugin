import json
import os

import pytest

from openmail_plugin import tools


class FakeApi:
    def __init__(self):
        self.sent = []

    def send(self, **kw):
        self.sent.append(kw)
        return {"id": "msg_1"}

    def create_inbox_key(self, inbox_id, name):
        return {"id": "key_1", "name": name, "token": "om_inbox_secret"}


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
    for bad in ["/etc/passwd", str(home / ".env"), str(home / "output" / ".." / ".env"), "~/.ssh/id_rsa"]:
        with pytest.raises(ValueError, match="refused"):
            tools.openmail_send({"to": "a@x.com", "subject": "s", "body": "b", "inbox_id": "i", "attachments": [bad]}, api)
    assert len(api.sent) == 1


def test_attachment_symlink_escape_refused(home):
    link = home / "media" / "link"
    link.symlink_to(home / "secret.txt")
    (home / "secret.txt").write_text("x")
    with pytest.raises(ValueError, match="refused"):
        tools.openmail_reply({"thread_id": "t", "body": "b", "to": "a@x.com", "inbox_id": "i", "attachments": [str(link)]}, FakeApi())


def test_create_inbox_key_token_stays_out_of_context(home):
    out = tools.openmail_create_inbox_key({"inbox_id": "inb_1"}, FakeApi())
    assert "om_inbox_secret" not in json.dumps(out)
    path = out["env_file"]
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert open(path).read() == "OPENMAIL_API_KEY=om_inbox_secret\nOPENMAIL_INBOX_ID=inb_1\n"
