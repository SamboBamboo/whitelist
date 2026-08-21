"""Minecraft Server Management Protocol client — Tier 1 transport (§7).

JSON-RPC over WebSocket on a dedicated localhost bind (1.21.9+):
Authorization: Bearer <management-server-secret>, subprotocol minecraft-v1.

The §0 probe MUST pass end-to-end before this tier is trusted: schema-level
success is not enough, because Floodgate may intercept the admission check
rather than deferring to the vanilla allowlist — a write that succeeds and
reads back but does not change admission behavior is a failure that looks
like success. Player payload field names ({name, id}) should be confirmed
against this server's own `rpc.discover` output via the probe.
"""

from __future__ import annotations

import json


class ManagementError(Exception):
    pass


class ManagementClient:
    def __init__(self, url: str, secret: str, timeout: float = 10.0, connector=None):
        self.url = url
        self.secret = secret
        self.timeout = timeout
        # Injectable for tests; defaults to websockets.sync.client.connect.
        self._connector = connector
        self._next_id = 1

    def _connect(self):
        if self._connector is not None:
            return self._connector(self.url, self.secret)
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            raise ManagementError(
                "the 'websockets' package is required for the management backend "
                "(pip install 'whitelist-host[management]')"
            ) from e
        return connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.secret}"},
            subprotocols=["minecraft-v1"],
            open_timeout=self.timeout,
            close_timeout=self.timeout,
        )

    def call(self, method: str, params: list | None = None):
        req_id = self._next_id
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params
        with self._connect() as ws:
            ws.send(json.dumps(request))
            # The server may push notifications; read until our id answers.
            for _ in range(50):
                msg = json.loads(ws.recv(timeout=self.timeout))
                if isinstance(msg, dict) and msg.get("id") == req_id:
                    if "error" in msg:
                        raise ManagementError(f"{method}: {msg['error']}")
                    return msg.get("result")
        raise ManagementError(f"{method}: no response with id {req_id}")

    def discover(self):
        return self.call("rpc.discover")

    def allowlist(self) -> list[dict]:
        result = self.call("minecraft:allowlist/")
        return result if isinstance(result, list) else []

    def allowlist_add(self, name: str, uuid: str):
        # Structured add with the CAPTURED profile: name + uuid supplied
        # directly, no Mojang name resolution involved (§0 test 1).
        return self.call("minecraft:allowlist/add", [[{"name": name, "id": uuid}]])

    def allowlist_remove(self, name: str, uuid: str):
        return self.call("minecraft:allowlist/remove", [[{"name": name, "id": uuid}]])
