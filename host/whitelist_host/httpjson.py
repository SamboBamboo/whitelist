"""Tiny stdlib JSON-over-HTTP transport with explicit timeouts.

Returns (status, parsed_json) for any HTTP status; raises TransportError only
for network-level failures (DNS, refused, timeout). Injectable so tests fake
the Worker and Telegram without sockets.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class TransportError(Exception):
    pass


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict]:
    data = None
    headers = {"accept": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransportError(f"{method} {url}: {e}") from e


def _parse(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_body": parsed}
    except json.JSONDecodeError:
        return {"_raw": raw[:500].decode("utf-8", "replace")}
