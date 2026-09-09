"""``hermes openmail setup`` and ``hermes openmail doctor``.

Setup does what a person would otherwise do by hand: probe the key, pick or create the inbox, mint an
inbox-scoped key so a broad one never lands in ``.env``, then write ``OPENMAIL_API_KEY`` and
``OPENMAIL_ALLOW_ALL_USERS=true`` (Hermes's own sender gate; reachability lives in OpenMail's policy).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .api import OpenMailApi, OpenMailApiError
from .config import DEFAULT_BASE_URL, MODES, allow_all_senders, allowed_senders, read_config, secret

CONSOLE_URL = "https://console.openmail.sh"
DOCS_URL = "https://docs.openmail.sh/integrations/hermes"


# ---- Hermes CLI helpers, with plain fallbacks so the module imports outside Hermes ---------------
def _ui():
    try:
        from hermes_cli.setup import prompt, prompt_yes_no, print_info, print_success, print_warning, print_error
    except Exception:  # noqa: BLE001
        def prompt(q: str, default: Optional[str] = None, password: bool = False) -> str:
            import getpass
            text = f"{q}" + (f" [{default}]" if default else "") + ": "
            value = getpass.getpass(text) if password else input(text)
            return value.strip() or (default or "")

        def prompt_yes_no(q: str, default: bool = True) -> bool:
            value = input(f"{q} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            return default if not value else value in ("y", "yes")

        def _p(prefix: str) -> Callable[[str], None]:
            return lambda text: print(f"{prefix}{text}")

        print_info, print_success, print_warning, print_error = _p(""), _p("✓ "), _p("! "), _p("✗ ")
    return prompt, prompt_yes_no, print_info, print_success, print_warning, print_error


def _save_env(key: str, value: str) -> None:
    try:
        from hermes_cli.config import save_env_value
        save_env_value(key, value)
    except Exception:  # noqa: BLE001 — outside Hermes: append to ~/.hermes/.env
        from pathlib import Path
        path = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"
        lines = [l for l in path.read_text().splitlines() if not l.startswith(f"{key}=")] if path.exists() else []
        lines.append(f"{key}={value}")
        path.write_text("\n".join(lines) + "\n")


# ---- key probing --------------------------------------------------------------------------------
@dataclass
class KeyProbe:
    scope: str  # "inbox" | "pod" | "account"
    inboxes: List[Dict[str, Any]]
    pods: List[Dict[str, Any]]


def probe_key(api: OpenMailApi) -> KeyProbe:
    """Scope is inferred: an inbox key sees one inbox and no pods; a pod key sees one pod; an account key sees all."""
    inboxes = api.list_inboxes()
    try:
        pods = api.list_pods()
    except OpenMailApiError as exc:
        if exc.status != 403:
            raise
        pods = []
    if not pods:
        scope = "inbox" if len(inboxes) == 1 else "account"
    elif len(pods) == 1 and not pods[0].get("isDefault"):
        # An inbox key also sees its own pod, so one inbox + one pod is indistinguishable from a
        # pod key over a single-inbox pod. Call it inbox; setup still tries to narrow and reads the 403.
        scope = "inbox" if len(inboxes) == 1 else "pod"
    else:
        scope = "account"
    return KeyProbe(scope=scope, inboxes=inboxes, pods=pods)


def _narrow(api: OpenMailApi, inbox: Dict[str, Any], key: str, ok, warn) -> str:
    """Trade an account key for a pod key over the chosen inbox's pod. A pod key still lets the agent create
    inboxes, mint inbox keys and later run the whole pod (one env change, no new key), but cannot reach other
    pods, webhooks or account-wide policy. Inbox and pod keys get a 403 here: already narrow, keep as-is."""
    pod_id = inbox.get("podId")
    if not pod_id:
        return key
    try:
        minted = api.create_pod_key(str(pod_id), "hermes")
    except OpenMailApiError as exc:
        if exc.status == 403:
            return key
        warn(f"Could not mint a pod key ({exc}); storing the given key as-is")
        return key
    token = minted.get("token")
    if not token:
        return key
    ok("Minted a pod-scoped key; the account key is not stored")
    return str(token)


def interactive_setup(api_key: Optional[str] = None, *, non_interactive: bool = False) -> bool:
    prompt, prompt_yes_no, info, ok, warn, err = _ui()
    base_url = secret("OPENMAIL_BASE_URL") or DEFAULT_BASE_URL

    key = (api_key or "").strip()
    if not key:
        existing = read_config().api_key
        if existing and not non_interactive and prompt_yes_no("OPENMAIL_API_KEY is already set. Keep it?", True):
            key = existing
        elif non_interactive:
            key = existing
        else:
            info(f"Create a key at {CONSOLE_URL} (any scope).")
            key = prompt("OpenMail API key", password=True)
    if not key:
        err("No key given.")
        return False

    api = OpenMailApi(base_url, key)
    try:
        probe = probe_key(api)
    except OpenMailApiError as exc:
        err(f"Key rejected: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        err(f"Cannot reach {base_url}: {exc}")
        return False

    stored_key = key
    inbox: Optional[Dict[str, Any]] = None
    pod_id: Optional[str] = None

    if probe.scope == "inbox":
        inbox = probe.inboxes[0]
        ok(f"Inbox: {inbox.get('address')}")
    else:
        inboxes = probe.inboxes
        if probe.scope == "pod" and len(inboxes) > 1 and not non_interactive and prompt_yes_no(
                f"This pod key sees {len(inboxes)} inboxes. Run them all as one agent?", False):
            pod_id = str(probe.pods[0]["id"])
        elif not inboxes:
            name = "" if non_interactive else prompt("Mailbox name (blank for a random one)", default="")
            inbox = api.create_inbox(display_name="Hermes", mailbox_name=name or None)
            ok(f"Created {inbox.get('address')}")
        elif len(inboxes) == 1 or non_interactive:
            inbox = inboxes[0]
        else:
            for i, box in enumerate(inboxes, 1):
                info(f"  {i}. {box.get('address')}")
            info(f"  {len(inboxes) + 1}. create a new inbox")
            choice = prompt("Which inbox should the agent use?", default="1")
            try:
                index = int(choice)
            except ValueError:
                index = 1
            if index == len(inboxes) + 1:
                name = prompt("Mailbox name (blank for a random one)", default="")
                inbox = api.create_inbox(display_name="Hermes", mailbox_name=name or None)
                ok(f"Created {inbox.get('address')}")
            else:
                inbox = inboxes[max(1, min(index, len(inboxes))) - 1]

    if inbox is not None:
        stored_key = _narrow(api, inbox, key, ok, warn)

    _save_env("OPENMAIL_API_KEY", stored_key)
    for name in ("OPENMAIL_INBOX_ID", "OPENMAIL_POD_ID"):
        if secret(name):
            _save_env(name, "")
    if pod_id:
        _save_env("OPENMAIL_POD_ID", pod_id)
    elif inbox is not None and probe.scope != "inbox":
        # The stored key can see the whole pod; pin the inbox the agent runs as. Widening later is just
        # replacing this with OPENMAIL_POD_ID.
        _save_env("OPENMAIL_INBOX_ID", str(inbox["id"]))

    if not secret("OPENMAIL_MODE") and not non_interactive:
        mode = prompt("Mode: channel (reply by email), notify (tell you, no auto-reply), tool (no inbound)",
                      default="channel").strip().lower()
        if mode in MODES and mode != "channel":
            _save_env("OPENMAIL_MODE", mode)

    # Hermes denies unknown senders by default. Reachability is OpenMail's job (console / `openmail policy`).
    if not secret("OPENMAIL_ALLOWED_USERS"):
        _save_env("OPENMAIL_ALLOW_ALL_USERS", "true")

    address = inbox.get("address") if inbox else f"every inbox in pod {pod_id}"
    ok(f"OpenMail configured: {address}")
    info(f"Who may write to it is set in OpenMail policy: {CONSOLE_URL} or `openmail policy`.")
    info("Restart the gateway: hermes gateway restart")
    return True


# ---- doctor ------------------------------------------------------------------------------------
def doctor() -> bool:
    _, _, info, ok, warn, err = _ui()
    cfg = read_config()
    healthy = True
    if not cfg.configured:
        err("OPENMAIL_API_KEY is not set. Run: hermes openmail setup")
        return False
    api = OpenMailApi(cfg.base_url, cfg.api_key)
    try:
        probe = probe_key(api)
    except OpenMailApiError as exc:
        err(f"Key rejected by {cfg.base_url}: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        err(f"Cannot reach {cfg.base_url}: {exc}")
        return False
    ok(f"Key valid ({probe.scope}-scoped), {len(probe.inboxes)} inbox(es) visible")
    if cfg.pod_id:
        try:
            pod = api.get_pod(cfg.pod_id)
            info(f"Scope: pod {pod.get('id') or cfg.pod_id}")
        except OpenMailApiError:
            err(f"OPENMAIL_POD_ID={cfg.pod_id} is not visible to this key")
            healthy = False
    elif cfg.inbox_id:
        match = next((i for i in probe.inboxes if i.get("id") == cfg.inbox_id), None)
        if match:
            info(f"Inbox: {match.get('address')}")
        else:
            err(f"OPENMAIL_INBOX_ID={cfg.inbox_id} is not visible to this key")
            healthy = False
    elif len(probe.inboxes) == 1:
        info(f"Inbox: {probe.inboxes[0].get('address')}")
    elif len(probe.inboxes) > 1 and len(probe.pods) != 1:
        err(f"Key sees {len(probe.inboxes)} inboxes; set OPENMAIL_INBOX_ID or OPENMAIL_POD_ID")
        healthy = False
    info(f"Mode: {cfg.mode}")
    if allow_all_senders():
        info("Hermes sender gate: open (OpenMail policy decides who gets through)")
    elif allowed := allowed_senders():
        info(f"Hermes sender allowlist: {', '.join(sorted(allowed))}")
    else:
        warn("Hermes will drop every sender: set OPENMAIL_ALLOW_ALL_USERS=true or OPENMAIL_ALLOWED_USERS")
        healthy = False
    if probe.scope == "account":
        warn("An account key is stored in .env; `hermes openmail setup` can swap it for a pod-scoped one")
    if healthy:
        ok("Ready. Docs: " + DOCS_URL)
    return healthy


# ---- argparse glue -----------------------------------------------------------------------------
def setup_argparse(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="openmail_command")
    setup = subs.add_parser("setup", help="Configure the OpenMail platform (key, inbox, sender gate)")
    setup.add_argument("--api-key", help="Use this key instead of prompting")
    setup.add_argument("--api-key-stdin", action="store_true", help="Read the key from stdin")
    setup.add_argument("--yes", "-y", action="store_true", help="No prompts; take defaults")
    subs.add_parser("doctor", help="Check the OpenMail configuration")


def handle_cli(args: Any) -> None:
    command = getattr(args, "openmail_command", None)
    if command == "setup":
        key = sys.stdin.read().strip() if getattr(args, "api_key_stdin", False) else getattr(args, "api_key", None)
        sys.exit(0 if interactive_setup(key, non_interactive=bool(getattr(args, "yes", False))) else 1)
    if command == "doctor":
        sys.exit(0 if doctor() else 1)
    print("Usage: hermes openmail setup | doctor")
    sys.exit(2)
