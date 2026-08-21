"""Host configuration: a TOML file plus secrets from systemd credentials.

Secrets are NEVER in the config file or the repo (§9). They resolve, in
order, from:
  1. $CREDENTIALS_DIRECTORY/<name>   (systemd LoadCredential=)
  2. [secret_files] <name> = "/path" (a 0600 file owned by the service user)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "/etc/whitelist-gateway/config.toml"


@dataclass
class Config:
    # [worker]
    worker_base_url: str = "https://whitelist.sambonius.net"

    # [paths]
    db_path: str = "/var/lib/whitelist-gateway/state.sqlite"
    server_log: str = "/srv/minecraft/logs/latest.log"
    floodgate_config: str = "/srv/minecraft/plugins/floodgate/config.yml"
    whitelist_json: str = "/srv/minecraft/whitelist.json"

    # [matcher]
    poll_interval_s: int = 60
    session_window_s: int = 45
    tail_interval_s: float = 1.0

    # [telegram]
    telegram_enabled: bool = True
    telegram_chat_id: str = ""

    # [admin]
    admin_bind_host: str = "127.0.0.1"
    admin_bind_port: int = 8080
    admin_allowed_origins: list[str] = field(default_factory=list)
    reviewer: str = "admin"

    # [allowlist]
    allowlist_backend: str = "file"  # management | rcon | file
    # For the file tier: how `whitelist reload` is issued after edits.
    # "rcon" | "management" | "none" (operator reloads manually).
    reload_via: str = "none"

    # [management]
    management_url: str = "ws://127.0.0.1:25585"

    # [rcon]
    rcon_host: str = "127.0.0.1"
    rcon_port: int = 25575

    secret_files: dict[str, str] = field(default_factory=dict)

    def secret(self, name: str) -> str | None:
        cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        if cred_dir:
            p = Path(cred_dir) / name
            if p.is_file():
                return p.read_text().strip()
        path = self.secret_files.get(name)
        if path and Path(path).is_file():
            return Path(path).read_text().strip()
        return None


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("WHITELIST_CONFIG", DEFAULT_CONFIG_PATH))
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = Config()
    worker = raw.get("worker", {})
    cfg.worker_base_url = worker.get("base_url", cfg.worker_base_url).rstrip("/")

    paths = raw.get("paths", {})
    cfg.db_path = paths.get("db", cfg.db_path)
    cfg.server_log = paths.get("server_log", cfg.server_log)
    cfg.floodgate_config = paths.get("floodgate_config", cfg.floodgate_config)
    cfg.whitelist_json = paths.get("whitelist_json", cfg.whitelist_json)

    matcher = raw.get("matcher", {})
    cfg.poll_interval_s = int(matcher.get("poll_interval_s", cfg.poll_interval_s))
    cfg.session_window_s = int(matcher.get("session_window_s", cfg.session_window_s))
    cfg.tail_interval_s = float(matcher.get("tail_interval_s", cfg.tail_interval_s))

    tg = raw.get("telegram", {})
    cfg.telegram_enabled = bool(tg.get("enabled", cfg.telegram_enabled))
    cfg.telegram_chat_id = str(tg.get("chat_id", cfg.telegram_chat_id))

    admin = raw.get("admin", {})
    cfg.admin_bind_host = admin.get("bind_host", cfg.admin_bind_host)
    cfg.admin_bind_port = int(admin.get("bind_port", cfg.admin_bind_port))
    cfg.admin_allowed_origins = [
        o.rstrip("/") for o in admin.get("allowed_origins", [])
    ]
    cfg.reviewer = admin.get("reviewer", cfg.reviewer)

    allowlist = raw.get("allowlist", {})
    cfg.allowlist_backend = allowlist.get("backend", cfg.allowlist_backend)
    cfg.reload_via = allowlist.get("reload_via", cfg.reload_via)

    mgmt = raw.get("management", {})
    cfg.management_url = mgmt.get("url", cfg.management_url)

    rcon = raw.get("rcon", {})
    cfg.rcon_host = rcon.get("host", cfg.rcon_host)
    cfg.rcon_port = int(rcon.get("port", cfg.rcon_port))

    cfg.secret_files = {k: str(v) for k, v in raw.get("secret_files", {}).items()}
    return cfg
