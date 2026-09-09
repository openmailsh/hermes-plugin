"""OpenMail plugin for Hermes Agent: an email address for your agent, as a gateway platform plus tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .api import OpenMailApi
from .config import read_config, secret
from .tools import register_tools

logger = logging.getLogger(__name__)

PLATFORM_HINT = (
    "You are reading and writing email through OpenMail. Inbound mail arrives as a message with From, Subject "
    "and Thread headers. In channel mode your final message IS the reply: it is sent verbatim as the email body, "
    "no preamble, no markdown. Do not also call openmail_reply on the thread you are answering. Use "
    "openmail_reply for other threads and openmail_send to start a new one."
)


def check_requirements() -> bool:
    """Passive probe: is the key present? Never installs anything."""
    return bool(secret("OPENMAIL_API_KEY"))


def _validate_config(config: Any) -> bool:
    return read_config(getattr(config, "extra", {}) or {}).configured


def _is_connected(config: Any) -> bool:
    return _validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env so an env-only setup shows in ``hermes gateway status``."""
    cfg = read_config()
    if not cfg.configured:
        return None
    seed: Dict[str, Any] = {"mode": cfg.mode}
    if cfg.inbox_id:
        seed["inbox_id"] = cfg.inbox_id
    if cfg.pod_id:
        seed["pod_id"] = cfg.pod_id
    if cfg.home_address:
        seed["home_channel"] = cfg.home_address
    return seed


def _build_adapter(config: Any):
    from .adapter import OpenMailAdapter

    return OpenMailAdapter(config)


async def _standalone_send(pconfig: Any, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[list] = None, force_document: bool = False) -> dict:
    """Out-of-process delivery (cron running without the gateway): one new email to ``chat_id``."""
    import asyncio

    cfg = read_config(getattr(pconfig, "extra", {}) or {})
    if not cfg.configured:
        return {"error": "OpenMail not configured (OPENMAIL_API_KEY required)"}
    api = OpenMailApi(cfg.base_url, cfg.api_key)

    def run() -> dict:
        # Hermes also uses this path to redeliver a turn's reply after a restart; chat_id is then the
        # original sender. Honour the conversation: in-thread in channel mode, never emailed in notify mode.
        from .adapter import ThreadStore, chat_key, state_dir
        ctx = ThreadStore(state_dir() / "threads.json").get(chat_key(chat_id, thread_id)) \
            or ThreadStore(state_dir() / "threads.json").get(chat_key(chat_id, None))
        if ctx and ctx.mode == "notify":
            logger.warning("[OpenMail] not emailing %s: conversation is in notify mode. Message: %s",
                           chat_id, message[:160].replace("\n", " "))
            return {"success": True, "platform": "openmail", "chat_id": chat_id, "skipped": "notify mode"}
        if ctx:
            api.send(inbox_id=ctx.inbox_id, to=ctx.to, body=message, thread_id=ctx.thread_id,
                     attachments=[str(p) for p in (media_files or [])] or None)
            return {"success": True, "platform": "openmail", "chat_id": chat_id}
        inbox_id = cfg.inbox_id
        if not inbox_id:
            inboxes = api.list_inboxes()
            if not inboxes:
                return {"error": "no OpenMail inbox to send from"}
            inbox_id = str(inboxes[0]["id"])
        api.send(inbox_id=inbox_id, to=chat_id, subject="Message from your agent", body=message,
                 attachments=[str(p) for p in (media_files or [])] or None)
        return {"success": True, "platform": "openmail", "chat_id": chat_id}

    try:
        return await asyncio.to_thread(run)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"OpenMail send failed: {exc}"}
    finally:
        api.close()


def _register_skills(ctx: Any) -> None:
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.exists():
        return
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md, description="OpenMail CLI and API reference for agents")


def register(ctx: Any) -> None:
    """Plugin entry point, called by the Hermes plugin system."""
    ctx.register_platform(
        name="openmail",
        label="OpenMail",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        validate_config=_validate_config,
        is_connected=_is_connected,
        required_env=["OPENMAIL_API_KEY"],
        install_hint="Set OPENMAIL_API_KEY in ~/.hermes/.env. Keys: https://console.openmail.sh",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="OPENMAIL_HOME_ADDRESS",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="OPENMAIL_ALLOWED_USERS",
        allow_all_env="OPENMAIL_ALLOW_ALL_USERS",
        max_message_length=50_000,
        pii_safe=True,
        emoji="📬",
        allow_update_command=False,
        platform_hint=PLATFORM_HINT,
    )
    register_tools(ctx)
    _register_skills(ctx)
    if hasattr(ctx, "register_cli_command"):
        from .cli import handle_cli, setup_argparse
        ctx.register_cli_command(
            name="openmail",
            help="OpenMail: `setup` wires a key and inbox, `doctor` checks the config",
            setup_fn=setup_argparse,
            handler_fn=handle_cli,
        )
    logger.info("OpenMail plugin registered")
