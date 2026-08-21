"""Shared fakes: an in-memory Worker, allowlist backend, and Telegram, so
the host logic runs against real SQLite and real files with no network."""

from __future__ import annotations

import pytest

from whitelist_host import db as dbmod
from whitelist_host.allowlist import canon_uuid, name_conflict, uuid_present, AllowlistConflict


class FakeWorker:
    """Duck-typed WorkerClient. Program its payloads per test."""

    def __init__(self):
        self.normalization = {"username_prefix": ".", "replace_spaces": True, "version": 1}
        self.pending: list[dict] = []
        self.recently_verified: list[dict] = []
        self.review_submissions: list[dict] = []
        self.recent_terminal: list[dict] = []
        self.verify_calls: list[dict] = []
        self.decision_calls: list[dict] = []
        # (status, data) or callable(payload) -> (status, data)
        self.verify_response = None
        self.decision_response = (200, {"ok": True, "transitioned": True})
        self.now = 1_750_000_000

    def get_pending(self):
        return {
            "now": self.now,
            "normalization": self.normalization,
            "pending": [dict(s) for s in self.pending],
            "recently_verified": [dict(s) for s in self.recently_verified],
        }

    def post_verify(self, payload):
        self.verify_calls.append(dict(payload))
        if callable(self.verify_response):
            return self.verify_response(payload)
        if self.verify_response is not None:
            return self.verify_response
        return 200, {"transitioned": True, "attempt_event_id": payload["attempt_event_id"]}

    def get_review(self):
        return {
            "now": self.now,
            "normalization": self.normalization,
            "submissions": [dict(s) for s in self.review_submissions],
            "recent_terminal": [dict(s) for s in self.recent_terminal],
        }

    def post_decision(self, payload):
        self.decision_calls.append(dict(payload))
        if callable(self.decision_response):
            return self.decision_response(payload)
        return self.decision_response


class FakeBackend:
    """In-memory allowlist honoring the real backend contract, with failure
    injection for mutation/read-back paths."""

    def __init__(self):
        self._entries: list[dict] = []
        self.add_calls = 0
        self.remove_calls = 0
        self.fail_add = False  # raise on add
        self.drop_add = False  # add "succeeds" but changes nothing (read-back must catch)

    def entries(self):
        return [dict(e) for e in self._entries]

    def seed(self, name, uuid):
        self._entries.append({"name": name, "uuid": uuid})

    def add(self, name, uuid, platform):
        self.add_calls += 1
        if self.fail_add:
            raise RuntimeError("injected add failure")
        current = self.entries()
        if uuid_present(current, uuid):
            return
        conflict = name_conflict(current, name, uuid)
        if conflict:
            raise AllowlistConflict(f"{name!r} already present with {conflict['uuid']}")
        if self.drop_add:
            return
        self._entries.append({"name": name, "uuid": uuid})

    def remove(self, name, uuid, platform):
        self.remove_calls += 1
        target = canon_uuid(uuid)
        self._entries = [e for e in self._entries if canon_uuid(e["uuid"]) != target]


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.ok = True

    def send(self, text):
        if self.ok:
            self.sent.append(text)
            return True
        return False


@pytest.fixture
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "state.sqlite")
    yield c
    c.close()


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def telegram():
    return FakeTelegram()
