"""``hermes openmail setup`` and ``hermes openmail doctor``.

Setup does what a person would otherwise do by hand: probe the key, pick or create the inbox, mint a narrower
key so a broad one never lands in ``.env`` (pod-scoped by default, inbox-scoped with ``--inbox``), then write
``OPENMAIL_API_KEY`` and a sender allowlist (``OPENMAIL_ALLOWED_USERS``). ``OPENMAIL_ALLOW_ALL_USERS=true`` is
only written on an explicit ``--allow-all`` or when the operator picks "sender rules decide" in the menu; ``-y`` alone never opens Hermes's sender gate.

``--inbox`` is also how a Bot (a Hermes profile) gets its own address: ``hermes -p <bot> openmail setup
--api-key-stdin --inbox <address> -y``. The token goes straight into that profile's ``.env`` and never
through a model.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .api import OpenMailApi, OpenMailApiError
from .config import DEFAULT_BASE_URL, MODES, allow_all_senders, allowed_senders, read_config, secret

CONSOLE_URL = "https://console.openmail.sh"
POLICY_URL = f"{CONSOLE_URL}/sender-rules"
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
    """``/v1/me`` says what the key is. Older servers without it fall back to inference: an inbox key sees one inbox
    and no pods; a pod key sees one pod; an account key sees all."""
    inboxes = api.list_inboxes()
    try:
        pods = api.list_pods()
    except OpenMailApiError as exc:
        if exc.status != 403:
            raise
        pods = []
    try:
        declared = api.me().get("apiKeyScope")
    except OpenMailApiError:
        declared = None
    if declared == "account":
        scope = "account"
    elif isinstance(declared, dict):
        scope = "inbox" if declared.get("inboxId") else "pod" if declared.get("podId") else "account"
    elif not pods:
        scope = "inbox" if len(inboxes) == 1 else "account"
    elif len(pods) == 1 and not pods[0].get("isDefault"):
        # An inbox key also sees its own pod, so one inbox + one pod is indistinguishable from a
        # pod key over a single-inbox pod. Call it inbox; setup still tries to narrow and reads the 403.
        scope = "inbox" if len(inboxes) == 1 else "pod"
    else:
        scope = "account"
    return KeyProbe(scope=scope, inboxes=inboxes, pods=pods)


def _narrow(api: OpenMailApi, inbox: Dict[str, Any], key: str, ok, warn, *,
            to_inbox: bool = False, already_inbox: bool = False) -> Optional[str]:
    """Trade a broad key for a narrower one and never store the broad one.

    Default: account key -> pod key over the chosen inbox's pod. A pod key still lets the agent create inboxes and
    later run the whole pod (one env change, no new key), but cannot reach other pods, webhooks or account-wide
    policy. With ``to_inbox`` (``--inbox``): account or pod key -> inbox key, for a profile that should only ever
    be this one address. A key that is already inbox-scoped gets a 403 here and is kept as-is. Returns None when
    ``to_inbox`` was set but an inbox-scoped key could not be obtained (so a broader key is never stored)."""
    try:
        if to_inbox:
            minted = api.create_inbox_key(str(inbox["id"]), "hermes")
        else:
            pod_id = inbox.get("podId")
            if not pod_id:
                return key
            minted = api.create_pod_key(str(pod_id), "hermes")
    except OpenMailApiError as exc:
        if exc.status == 403:
            if to_inbox and not already_inbox:
                return None
            return key
        if to_inbox:
            return None
        warn(f"Could not mint a narrower key ({exc}); storing the given key as-is")
        return key
    token = minted.get("token")
    if not token:
        return None if to_inbox else key
    ok(f"Minted an {'inbox' if to_inbox else 'pod'}-scoped key; the given key is not stored")
    return str(token)


def _find_inbox(inboxes: List[Dict[str, Any]], ref: str) -> Optional[Dict[str, Any]]:
    wanted = ref.strip().lower()
    return next((b for b in inboxes if wanted in (str(b.get("id", "")).lower(), str(b.get("address", "")).lower())), None)


def interactive_setup(api_key: Optional[str] = None, *, non_interactive: bool = False,
                      allow_all: bool = False, inbox_ref: Optional[str] = None) -> bool:
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

    if inbox_ref:
        inbox = _find_inbox(probe.inboxes, inbox_ref)
        if inbox is None:
            err(f"No inbox {inbox_ref!r} is visible to this key.")
            return False
        ok(f"Inbox: {inbox.get('address')}")
    elif probe.scope == "inbox":
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
        stored_key = _narrow(api, inbox, key, ok, warn, to_inbox=bool(inbox_ref),
                             already_inbox=probe.scope == "inbox")
        if stored_key is None:
            err("Could not mint an inbox-scoped key; refusing to store a broader key")
            return False

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

    # Hermes denies unknown senders by default. The allowlist is the default; opening the gate to every sender
    # that OpenMail policy lets through needs an explicit --allow-all or an interactive choice, never `-y` alone.
    if not secret("OPENMAIL_ALLOWED_USERS") and not allow_all_senders():
        if allow_all:
            _save_env("OPENMAIL_ALLOW_ALL_USERS", "true")
        elif non_interactive:
            warn("No sender allowlist. Set OPENMAIL_ALLOWED_USERS=a@x.com,b@y.io, or rerun with --allow-all "
                 "to let OpenMail policy alone decide.")
        else:
            info("Who may email the agent?")
            info("  1. Only addresses I list now")
            info(f"  2. Anyone OpenMail's sender rules let through (manage at {POLICY_URL} or `openmail policy`)")
            info("  3. Decide later (the agent drops every sender until OPENMAIL_ALLOWED_USERS is set)")
            choice = prompt("Choose", default="1").strip()
            if choice == "2":
                _save_env("OPENMAIL_ALLOW_ALL_USERS", "true")
                ok(f"Sender rules decide: {POLICY_URL}")
            elif choice == "3":
                warn("No sender allowlist: Hermes will drop every sender until OPENMAIL_ALLOWED_USERS is set.")
            else:
                senders = ",".join(part.strip() for part in prompt(
                    "Addresses, comma-separated", default="").split(",") if part.strip())
                if senders:
                    _save_env("OPENMAIL_ALLOWED_USERS", senders)
                else:
                    warn("No addresses given: Hermes will drop every sender until OPENMAIL_ALLOWED_USERS is set.")

    address = inbox.get("address") if inbox else f"every inbox in pod {pod_id}"
    ok(f"OpenMail configured: {address}")
    info(f"OpenMail's own sender rules apply on top: {POLICY_URL} or `openmail policy`.")
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
    setup.add_argument("--allow-all", action="store_true",
                       help="Write OPENMAIL_ALLOW_ALL_USERS=true instead of a sender allowlist")
    setup.add_argument("--inbox", metavar="ID_OR_ADDRESS",
                       help="Run as exactly this inbox and store an inbox-scoped key (for a Bot profile: "
                            "hermes -p <bot> openmail setup --inbox <address>)")
    subs.add_parser("doctor", help="Check the OpenMail configuration")


def handle_cli(args: Any) -> None:
    command = getattr(args, "openmail_command", None)
    if command == "setup":
        key = sys.stdin.read().strip() if getattr(args, "api_key_stdin", False) else getattr(args, "api_key", None)
        sys.exit(0 if interactive_setup(key, non_interactive=bool(getattr(args, "yes", False)),
                                        allow_all=bool(getattr(args, "allow_all", False)),
                                        inbox_ref=getattr(args, "inbox", None)) else 1)
    if command == "doctor":
        sys.exit(0 if doctor() else 1)
    print("Usage: hermes openmail setup | doctor")
    sys.exit(2)
