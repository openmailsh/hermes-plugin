from typing import Any, Dict, List, Optional

import pytest

from openmail_plugin import cli
from openmail_plugin.api import OpenMailApiError


class FakeApi:
    def __init__(self, inboxes: List[Dict[str, Any]], pods: Optional[List[Dict[str, Any]]] = None,
                 pods_forbidden: bool = False, mint_fails: bool = False):
        self.inboxes, self.pods = inboxes, pods or []
        self.pods_forbidden, self.mint_fails = pods_forbidden, mint_fails
        self.minted: List[str] = []

    def list_inboxes(self):
        return self.inboxes

    def list_pods(self):
        if self.pods_forbidden:
            raise OpenMailApiError("forbidden", 403)
        return self.pods

    def create_inbox(self, **kw):
        inbox = {"id": "inb_new", "address": "hermes@omail.sh"}
        self.inboxes.append(inbox)
        return inbox

    def create_inbox_key(self, inbox_id, name):
        if self.mint_fails:
            raise OpenMailApiError("inbox not in a pod", 409)
        self.minted.append(inbox_id)
        return {"id": "key_1", "token": f"om_inbox_{inbox_id}"}


@pytest.fixture
def env(monkeypatch):
    saved: Dict[str, str] = {}
    monkeypatch.setattr(cli, "_save_env", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(cli, "_ui", lambda: (
        lambda q, default=None, password=False: default or "",
        lambda q, default=True: default,
        lambda t: None, lambda t: None, lambda t: None, lambda t: None,
    ))
    for name in ("OPENMAIL_API_KEY", "OPENMAIL_INBOX_ID", "OPENMAIL_POD_ID", "OPENMAIL_MODE",
                 "OPENMAIL_ALLOWED_USERS", "OPENMAIL_ALLOW_ALL_USERS"):
        monkeypatch.delenv(name, raising=False)
    return saved


def test_probe_scope():
    assert cli.probe_key(FakeApi([{"id": "a"}], pods_forbidden=True)).scope == "inbox"
    assert cli.probe_key(FakeApi([{"id": "a"}], pods=[{"id": "p"}])).scope == "inbox"
    assert cli.probe_key(FakeApi([{"id": "a"}, {"id": "b"}], pods=[{"id": "p"}])).scope == "pod"
    assert cli.probe_key(FakeApi([{"id": "a"}], pods=[{"id": "p", "isDefault": True}, {"id": "q"}])).scope == "account"


def test_inbox_key_stored_as_is(env, monkeypatch):
    api = FakeApi([{"id": "inb_1", "address": "bot@omail.sh"}], pods_forbidden=True)
    api.create_inbox_key = lambda *a: (_ for _ in ()).throw(OpenMailApiError("inbox-scoped", 403))
    monkeypatch.setattr(cli, "OpenMailApi", lambda *a, **k: api)
    assert cli.interactive_setup("om_inbox") is True
    assert env["OPENMAIL_API_KEY"] == "om_inbox"
    assert "OPENMAIL_INBOX_ID" not in env
    assert env["OPENMAIL_ALLOW_ALL_USERS"] == "true"


def test_account_key_creates_inbox_and_narrows(env, monkeypatch):
    api = FakeApi([], pods=[{"id": "p", "isDefault": True}])
    monkeypatch.setattr(cli, "OpenMailApi", lambda *a, **k: api)
    assert cli.interactive_setup("om_account", non_interactive=True) is True
    assert api.minted == ["inb_new"]
    assert env["OPENMAIL_API_KEY"] == "om_inbox_inb_new"
    assert "OPENMAIL_INBOX_ID" not in env  # the minted key sees exactly one inbox


def test_inbox_key_seeing_its_pod_is_not_narrowed(env, monkeypatch):
    api = FakeApi([{"id": "inb_1", "address": "bot@omail.sh"}], pods=[{"id": "p"}], mint_fails=True)
    api.create_inbox_key = lambda *a: (_ for _ in ()).throw(OpenMailApiError("inbox-scoped", 403))
    monkeypatch.setattr(cli, "OpenMailApi", lambda *a, **k: api)
    assert cli.interactive_setup("om_inbox") is True
    assert env["OPENMAIL_API_KEY"] == "om_inbox"
    assert "OPENMAIL_INBOX_ID" not in env


def test_mint_failure_keeps_broad_key(env, monkeypatch):
    api = FakeApi([{"id": "inb_1", "address": "a@omail.sh"}, {"id": "inb_2", "address": "b@omail.sh"}],
                  pods=[{"id": "p", "isDefault": True}, {"id": "q"}], mint_fails=True)
    monkeypatch.setattr(cli, "OpenMailApi", lambda *a, **k: api)
    assert cli.interactive_setup("om_account") is True  # default choice "1"
    assert env["OPENMAIL_API_KEY"] == "om_account"
    assert env["OPENMAIL_INBOX_ID"] == "inb_1"


def test_allowlist_not_overridden(env, monkeypatch):
    monkeypatch.setenv("OPENMAIL_ALLOWED_USERS", "boss@x.com")
    api = FakeApi([{"id": "inb_1", "address": "bot@omail.sh"}], pods_forbidden=True)
    monkeypatch.setattr(cli, "OpenMailApi", lambda *a, **k: api)
    assert cli.interactive_setup("om_inbox") is True
    assert "OPENMAIL_ALLOW_ALL_USERS" not in env


def test_rejected_key(env, monkeypatch):
    class Bad:
        def list_inboxes(self):
            raise OpenMailApiError("unauthorized", 401)
    monkeypatch.setattr(cli, "OpenMailApi", lambda *a, **k: Bad())
    assert cli.interactive_setup("nope") is False
    assert env == {}
