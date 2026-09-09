"""Settings for the OpenMail platform. Env first, then ``platforms.openmail`` in config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

DEFAULT_BASE_URL = "https://api.openmail.sh"
MODES = ("channel", "notify", "tool")
_TRUTHY = {"1", "true", "yes", "on"}


def secret(name: str, default: str = "") -> str:
    """Profile-scoped env read when running inside Hermes; plain ``os.environ`` otherwise."""
    try:
        from gateway.platforms._shared import get_scoped_secret  # type: ignore

        value = get_scoped_secret(name, default)
    except Exception:  # noqa: BLE001 — outside Hermes (tests, scripts)
        value = os.getenv(name, default)
    return str(value or "").strip()


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def normalize_mode(value: Any, default: str = "channel") -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in MODES else default


@dataclass
class OpenMailConfig:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    inbox_id: Optional[str] = None
    pod_id: Optional[str] = None
    mode: str = "channel"
    home_address: Optional[str] = None
    # Pod scope only: inbox id or address -> {"mode": ...}
    inboxes: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def inbox_mode(self, inbox_id: str, address: Optional[str] = None) -> str:
        """Mode for one inbox: per-inbox override (by id, then by address) or the account default."""
        for key in (inbox_id, (address or "").lower()):
            override = self.inboxes.get(key) if key else None
            if isinstance(override, Mapping) and override.get("mode"):
                return normalize_mode(override["mode"], self.mode)
        return self.mode


def read_config(extra: Optional[Mapping[str, Any]] = None) -> OpenMailConfig:
    """Merge env (wins) with ``PlatformConfig.extra`` (config.yaml)."""
    extra = dict(extra or {})

    def pick(env: str, key: str, default: str = "") -> str:
        return secret(env) or str(extra.get(key) or "").strip() or default

    inboxes: Dict[str, Dict[str, Any]] = {}
    raw_inboxes = extra.get("inboxes")
    if isinstance(raw_inboxes, Mapping):
        for k, v in raw_inboxes.items():
            if isinstance(v, Mapping):
                inboxes[str(k).strip().lower() if "@" in str(k) else str(k).strip()] = dict(v)

    return OpenMailConfig(
        api_key=pick("OPENMAIL_API_KEY", "api_key"),
        base_url=pick("OPENMAIL_BASE_URL", "base_url", DEFAULT_BASE_URL).rstrip("/"),
        inbox_id=pick("OPENMAIL_INBOX_ID", "inbox_id") or None,
        pod_id=pick("OPENMAIL_POD_ID", "pod_id") or None,
        mode=normalize_mode(pick("OPENMAIL_MODE", "mode")),
        home_address=pick("OPENMAIL_HOME_ADDRESS", "home_address") or None,
        inboxes=inboxes,
    )


def allowed_senders() -> set[str]:
    """Union of OPENMAIL_ALLOWED_USERS and GATEWAY_ALLOWED_USERS, lowercased."""
    out: set[str] = set()
    for name in ("OPENMAIL_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS"):
        for part in secret(name).split(","):
            part = part.strip().lower()
            if part:
                out.add(part)
    return out


def allow_all_senders() -> bool:
    return truthy(secret("OPENMAIL_ALLOW_ALL_USERS")) or truthy(os.getenv("GATEWAY_ALLOW_ALL_USERS", ""))
