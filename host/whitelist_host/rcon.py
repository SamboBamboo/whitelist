"""Minimal Source-RCON client (stdlib only) — Tier 2 transport (§7).

RCON success is not reliably detectable from command output: it returns
human-readable text, not a status code. Callers must confirm mutations by
re-reading the allowlist — read-back is the success signal. This client just
moves commands; it never claims a mutation "worked".
"""

from __future__ import annotations

import socket
import struct

_TYPE_LOGIN = 3
_TYPE_COMMAND = 2


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_id = 1

    def __enter__(self) -> "Rcon":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as e:
            raise RconError(f"cannot connect to rcon {self.host}:{self.port}: {e}") from e
        resp_id, _, _ = self._roundtrip(_TYPE_LOGIN, self.password)
        if resp_id == -1:
            raise RconAuthError("rcon password rejected")

    def command(self, cmd: str) -> str:
        if self._sock is None:
            raise RconError("not connected")
        _, _, payload = self._roundtrip(_TYPE_COMMAND, cmd)
        return payload

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _roundtrip(self, ptype: int, payload: str) -> tuple[int, int, str]:
        assert self._sock is not None
        pid = self._next_id
        self._next_id += 1
        body = struct.pack("<ii", pid, ptype) + payload.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(body)) + body)
        return self._read_packet()

    def _read_packet(self) -> tuple[int, int, str]:
        assert self._sock is not None
        raw_len = self._recv_exact(4)
        (length,) = struct.unpack("<i", raw_len)
        if length < 10 or length > 4110:
            raise RconError(f"implausible rcon packet length {length}")
        data = self._recv_exact(length)
        pid, ptype = struct.unpack("<ii", data[:8])
        payload = data[8:-2].decode("utf-8", "replace")
        return pid, ptype, payload

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("rcon connection closed mid-packet")
            buf += chunk
        return buf
