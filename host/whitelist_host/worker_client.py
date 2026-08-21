"""Client for the Worker API — the host's ONLY channel to Cloudflare (§2).

No D1 token exists on this machine; transition rules stay enforced in the
Worker, and this client just speaks the four endpoints with the right token
for each audience (§9): daemon token for pending/verify, admin token for
review/decision.
"""

from __future__ import annotations

from .httpjson import request_json


class WorkerError(Exception):
    def __init__(self, status: int, data: dict):
        super().__init__(f"worker returned {status}: {data}")
        self.status = status
        self.data = data


class WorkerClient:
    def __init__(
        self,
        base_url: str,
        *,
        daemon_token: str | None = None,
        admin_token: str | None = None,
        transport=request_json,
    ):
        self.base_url = base_url.rstrip("/")
        self.daemon_token = daemon_token
        self.admin_token = admin_token
        self.transport = transport

    # --- daemon surface ---

    def get_pending(self) -> dict:
        status, data = self.transport(
            "GET", f"{self.base_url}/api/pending", token=self.daemon_token
        )
        if status != 200:
            raise WorkerError(status, data)
        return data

    def post_verify(self, payload: dict) -> tuple[int, dict]:
        """Returns (status, data) — 200/400/409 are all meaningful to the
        matcher, so no raising on 4xx here."""
        return self.transport(
            "POST", f"{self.base_url}/api/verify", token=self.daemon_token, body=payload
        )

    # --- admin surface ---

    def get_review(self) -> dict:
        status, data = self.transport(
            "GET", f"{self.base_url}/api/review", token=self.admin_token
        )
        if status != 200:
            raise WorkerError(status, data)
        return data

    def post_decision(self, payload: dict) -> tuple[int, dict]:
        return self.transport(
            "POST", f"{self.base_url}/api/decision", token=self.admin_token, body=payload
        )
