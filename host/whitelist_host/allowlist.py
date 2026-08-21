"""Allowlist mutation backends (§7), preference order:

  Tier 1  Management Protocol — structured add/remove/list, both platforms,
          preferred once the §0 probe passes END-TO-END.
  Tier 2  RCON commands — `whitelist add <raw>` (Java) /
          `fwhitelist add <uuid>` (Bedrock; verify exact syntax on your
          build). RCON output is not a success signal.
  Tier 3  Direct whitelist.json editing — documented and legitimate, but
          last resort. Single mutex, logged raw name, ownership/mode
          preserved, temp file → fsync → rename → fsync directory,
          idempotent on same-UUID, CONFLICT (never overwrite) on same name /
          different UUID, followed by `whitelist reload`.

All tiers are idempotent, because the outbox reconciliation re-runs them.
Across every tier, READ-BACK IS THE SUCCESS SIGNAL: the outbox confirms the
UUID landed (or left) by re-reading the allowlist, not by trusting the
mutation call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class AllowlistError(Exception):
    pass


class AllowlistConflict(AllowlistError):
    """Same name already present with a DIFFERENT UUID. Never silently
    overwritten — a human decides (§7)."""


def canon_uuid(u: str) -> str:
    return u.replace("-", "").lower()


class AllowlistBackend(Protocol):
    def entries(self) -> list[dict]: ...  # [{"name": ..., "uuid": ...}]
    def add(self, name: str, uuid: str, platform: str) -> None: ...
    def remove(self, name: str, uuid: str, platform: str) -> None: ...


def uuid_present(entries: list[dict], uuid: str) -> bool:
    target = canon_uuid(uuid)
    return any(canon_uuid(str(e.get("uuid", ""))) == target for e in entries)


def name_conflict(entries: list[dict], name: str, uuid: str) -> dict | None:
    """An entry with the same name but a different UUID, if any."""
    target = canon_uuid(uuid)
    for e in entries:
        if str(e.get("name", "")).lower() == name.lower() and canon_uuid(
            str(e.get("uuid", ""))
        ) != target:
            return e
    return None


# ------------------------------------------------------------------- tier 1


class ManagementBackend:
    def __init__(self, client):
        self.client = client  # mgmt.ManagementClient

    def entries(self) -> list[dict]:
        return [
            {"name": e.get("name", ""), "uuid": e.get("id") or e.get("uuid", "")}
            for e in self.client.allowlist()
        ]

    def add(self, name: str, uuid: str, platform: str) -> None:
        current = self.entries()
        if uuid_present(current, uuid):
            return  # idempotent
        conflict = name_conflict(current, name, uuid)
        if conflict:
            raise AllowlistConflict(
                f"allowlist already has {conflict['name']!r} with UUID "
                f"{conflict['uuid']} (wanted {uuid})"
            )
        self.client.allowlist_add(name, uuid)

    def remove(self, name: str, uuid: str, platform: str) -> None:
        if not uuid_present(self.entries(), uuid):
            return  # idempotent
        self.client.allowlist_remove(name, uuid)


# ------------------------------------------------------------------- tier 2


class RconBackend:
    """Commands over RCON; reads come from whitelist.json because RCON's
    text output cannot confirm anything (§7)."""

    def __init__(self, rcon_factory, whitelist_json: str | Path):
        self.rcon_factory = rcon_factory  # () -> context-manager Rcon
        self.file_reader = FileBackend(whitelist_json, reload_cmd=None)

    def entries(self) -> list[dict]:
        return self.file_reader.entries()

    def add(self, name: str, uuid: str, platform: str) -> None:
        current = self.entries()
        if uuid_present(current, uuid):
            return
        conflict = name_conflict(current, name, uuid)
        if conflict:
            raise AllowlistConflict(
                f"allowlist already has {conflict['name']!r} with UUID "
                f"{conflict['uuid']} (wanted {uuid})"
            )
        with self.rcon_factory() as rcon:
            if platform == "bedrock":
                # Floodgate's own allowlist command, keyed by the captured
                # Floodgate UUID. Verify exact syntax on your build (§7).
                out = rcon.command(f"fwhitelist add {uuid}")
            else:
                out = rcon.command(f"whitelist add {name}")
        logger.info("rcon add output (informational only): %r", out)

    def remove(self, name: str, uuid: str, platform: str) -> None:
        if not uuid_present(self.entries(), uuid):
            return
        with self.rcon_factory() as rcon:
            if platform == "bedrock":
                out = rcon.command(f"fwhitelist remove {uuid}")
            else:
                out = rcon.command(f"whitelist remove {name}")
        logger.info("rcon remove output (informational only): %r", out)


# ------------------------------------------------------------------- tier 3


class FileBackend:
    """Direct whitelist.json editing with the full §7 ritual."""

    def __init__(self, path: str | Path, reload_cmd=None):
        self.path = Path(path)
        # Called after every successful write, e.g. an RCON/management
        # `whitelist reload`. None → the operator reloads manually.
        self.reload_cmd = reload_cmd
        self._mutex = threading.Lock()

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text() or "[]")
        if not isinstance(data, list):
            raise AllowlistError(f"{self.path} is not a JSON array")
        return [
            {"name": e.get("name", ""), "uuid": e.get("uuid", "")}
            for e in data
            if isinstance(e, dict)
        ]

    def add(self, name: str, uuid: str, platform: str) -> None:
        with self._mutex:
            current = self._read_raw()
            if uuid_present(current, uuid):
                return  # idempotent success
            conflict = name_conflict(current, name, uuid)
            if conflict:
                raise AllowlistConflict(
                    f"whitelist.json already has {conflict['name']!r} with UUID "
                    f"{conflict['uuid']} (wanted {uuid}); refusing to overwrite"
                )
            current.append({"uuid": uuid, "name": name})
            self._write_atomic(current)
        self._reload()

    def remove(self, name: str, uuid: str, platform: str) -> None:
        with self._mutex:
            current = self._read_raw()
            target = canon_uuid(uuid)
            kept = [
                e for e in current if canon_uuid(str(e.get("uuid", ""))) != target
            ]
            if len(kept) == len(current):
                return  # idempotent
            self._write_atomic(kept)
        self._reload()

    def _read_raw(self) -> list[dict]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text() or "[]")
        if not isinstance(data, list):
            raise AllowlistError(f"{self.path} is not a JSON array")
        return data

    def _write_atomic(self, entries: list[dict]) -> None:
        directory = self.path.parent
        tmp = directory / f".{self.path.name}.tmp.{os.getpid()}"
        payload = json.dumps(entries, indent=2) + "\n"
        prior_stat = self.path.stat() if self.path.exists() else None
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        if prior_stat is not None:
            os.chmod(tmp, prior_stat.st_mode & 0o7777)
            try:
                os.chown(tmp, prior_stat.st_uid, prior_stat.st_gid)
            except PermissionError:
                if (prior_stat.st_uid, prior_stat.st_gid) != (os.getuid(), os.getgid()):
                    logger.warning(
                        "cannot preserve ownership of %s (need same user or "
                        "CAP_CHOWN); server may fail to rewrite it later",
                        self.path,
                    )
        os.rename(tmp, self.path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)  # the rename itself must survive a crash
        finally:
            os.close(dir_fd)

    def _reload(self) -> None:
        if self.reload_cmd is not None:
            self.reload_cmd()
        else:
            logger.info(
                "whitelist.json updated; no reload command configured — the "
                "server picks it up on `whitelist reload` or restart"
            )
