"""Tailer (§6): rotation drain, replay-safe positions, restart resume."""

import logging
import os

from whitelist_host.tailer import Position, Tailer, load_position, save_position


def write(path, text, mode="a"):
    with open(path, mode) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def test_incremental_reads_with_offsets(tmp_path):
    log = tmp_path / "latest.log"
    write(log, "one\ntwo\n", "w")
    t = Tailer(log, None)
    lines = t.poll()
    assert [l.text for l in lines] == ["one", "two"]
    assert lines[0].offset == 0
    assert lines[1].offset == 4
    assert t.poll() == []

    write(log, "three\n")
    lines = t.poll()
    assert [l.text for l in lines] == ["three"]
    assert lines[0].offset == 8


def test_partial_line_not_committed(tmp_path):
    log = tmp_path / "latest.log"
    write(log, "complete\nparti", "w")
    t = Tailer(log, None)
    lines = t.poll()
    assert [l.text for l in lines] == ["complete"]
    end = t.current_end()
    assert end.offset == len("complete\n")  # unfinished tail stays unread
    write(log, "al\n")
    assert [l.text for l in t.poll()] == ["partial"]


def test_rotation_drains_old_file_first(tmp_path):
    log = tmp_path / "latest.log"
    write(log, "old1\n", "w")
    t = Tailer(log, None)
    assert [l.text for l in t.poll()] == ["old1"]
    old_inode = os.stat(log).st_ino

    # Rotate: final line lands in the old file just before the switch.
    os.rename(log, tmp_path / "old.log")
    write(tmp_path / "old.log", "old2-final\n")
    write(log, "new1\n", "w")

    lines = t.poll()
    assert [l.text for l in lines] == ["old2-final", "new1"]
    assert lines[0].inode == old_inode
    assert lines[1].inode == os.stat(log).st_ino
    assert lines[1].inode != old_inode
    assert lines[1].offset == 0


def test_restart_resumes_from_saved_position(tmp_path, conn):
    log = tmp_path / "latest.log"
    write(log, "a\nb\n", "w")
    t = Tailer(log, None)
    t.poll()
    save_position(conn, str(log), t.current_end())
    conn.commit()
    t.close()

    write(log, "c\n")
    t2 = Tailer(log, load_position(conn, str(log)))
    assert [l.text for l in t2.poll()] == ["c"]  # no replay of a/b


def test_rotation_while_down_warns_and_starts_fresh(tmp_path, caplog):
    log = tmp_path / "latest.log"
    write(log, "gone1\ngone2\n", "w")
    stored = Position(inode=os.stat(log).st_ino, offset=6)
    # Create the replacement while the old file still exists so it is
    # guaranteed a different inode (deleting first lets tmpfs reuse it).
    write(tmp_path / "next.log", "fresh\n", "w")
    os.rename(tmp_path / "next.log", log)

    t = Tailer(log, stored)
    with caplog.at_level(logging.WARNING):
        lines = t.poll()
    assert [l.text for l in lines] == ["fresh"]
    assert any("rotated while the daemon was down" in r.message for r in caplog.records)


def test_truncation_in_place_rereads(tmp_path, caplog):
    log = tmp_path / "latest.log"
    write(log, "long line one\nlong line two\n", "w")
    stored = Position(inode=os.stat(log).st_ino, offset=28)
    write(log, "tiny\n", "w")  # same inode, shorter

    t = Tailer(log, stored)
    with caplog.at_level(logging.WARNING):
        lines = t.poll()
    assert [l.text for l in lines] == ["tiny"]
