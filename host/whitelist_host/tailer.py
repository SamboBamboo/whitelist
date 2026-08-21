"""Log tailer with rotation handling and replay-safe positions (§6).

Rules implemented here:
  - On rotation (inode change), the OLD descriptor is drained to EOF before
    the new file is opened. Rejection lines land milliseconds before a
    rotation boundary often enough to matter.
  - The caller decides what position to commit (it may hold the offset back
    to the start of the oldest unresolved session); this class just reports
    precise (inode, line_start_offset) for every line it yields.
  - If the daemon was down across a rotation, the old file's tail is gone;
    that is warned about loudly and the new file starts at 0. Re-processing
    overlap is harmless — attempt event ids are deterministic and UNIQUE.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TailedLine:
    inode: int
    offset: int  # byte offset of the line start
    text: str


@dataclass(frozen=True)
class Position:
    inode: int
    offset: int


class Tailer:
    def __init__(self, path: str | Path, position: Position | None):
        self.path = Path(path)
        self._fd: int | None = None
        self._inode: int | None = None
        self._offset = 0  # next unread byte in the open file
        self._buffer = b""
        self._buffer_start = 0  # offset of buffer[0] within the file
        self._stored = position

    def _open_current(self) -> bool:
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        st = os.fstat(fd)
        start = 0
        if self._stored is not None:
            if self._stored.inode == st.st_ino:
                if self._stored.offset <= st.st_size:
                    start = self._stored.offset
                else:
                    logger.warning(
                        "log %s shrank below stored offset (%d > %d); assuming "
                        "in-place truncation, re-reading from 0",
                        self.path,
                        self._stored.offset,
                        st.st_size,
                    )
            else:
                logger.warning(
                    "log %s rotated while the daemon was down (stored inode %s, "
                    "current %s); lines written to the old file after offset %d "
                    "were NOT processed",
                    self.path,
                    self._stored.inode,
                    st.st_ino,
                    self._stored.offset,
                )
            self._stored = None
        os.lseek(fd, start, os.SEEK_SET)
        self._fd = fd
        self._inode = st.st_ino
        self._offset = start
        self._buffer = b""
        self._buffer_start = start
        return True

    def _read_available(self) -> list[TailedLine]:
        assert self._fd is not None and self._inode is not None
        lines: list[TailedLine] = []
        while True:
            chunk = os.read(self._fd, 65536)
            if not chunk:
                break
            self._buffer += chunk
            self._offset += len(chunk)
        while True:
            nl = self._buffer.find(b"\n")
            if nl < 0:
                break
            raw = self._buffer[:nl]
            start = self._buffer_start
            self._buffer = self._buffer[nl + 1 :]
            self._buffer_start = start + nl + 1
            lines.append(
                TailedLine(
                    inode=self._inode,
                    offset=start,
                    text=raw.decode("utf-8", "replace"),
                )
            )
        return lines

    def poll(self) -> list[TailedLine]:
        """Return all complete new lines. Handles first open and rotation."""
        if self._fd is None:
            if not self._open_current():
                return []
            return self._read_available()

        lines = self._read_available()

        # Rotation check: has the path been replaced under us?
        try:
            st = os.stat(self.path)
            current_inode = st.st_ino
        except FileNotFoundError:
            current_inode = None

        if current_inode is not None and current_inode != self._inode:
            # Drain the old descriptor to EOF before switching (§6).
            lines.extend(self._read_available())
            if self._buffer:
                # A final unterminated line at EOF of the rotated file is
                # complete by definition — nothing more will be written.
                lines.append(
                    TailedLine(
                        inode=self._inode or 0,
                        offset=self._buffer_start,
                        text=self._buffer.decode("utf-8", "replace"),
                    )
                )
                self._buffer = b""
            os.close(self._fd)
            self._fd = None
            logger.info("log rotated (%s); switching to new file", self.path)
            if self._open_current():
                lines.extend(self._read_available())
        return lines

    def current_end(self) -> Position | None:
        """Position just past the last COMPLETE line consumed (an unfinished
        trailing line stays uncommitted)."""
        if self._inode is None:
            return self._stored
        return Position(inode=self._inode, offset=self._buffer_start)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def load_position(conn, path: str) -> Position | None:
    row = conn.execute(
        "SELECT inode, offset FROM log_position WHERE path = ?", (str(path),)
    ).fetchone()
    if row is None or row["inode"] is None:
        return None
    return Position(inode=row["inode"], offset=row["offset"])


def save_position(conn, path: str, pos: Position) -> None:
    """Caller wraps this in the same transaction as the attempt inserts."""
    conn.execute(
        "INSERT INTO log_position (path, inode, offset) VALUES (?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET inode = excluded.inode, offset = excluded.offset",
        (str(path), pos.inode, pos.offset),
    )
