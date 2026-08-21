"""Session classification (§3): every authenticated session gets exactly one
outcome, and WARN fires only on unresolved sessions and unparseable-but-
relevant lines — never on normal traffic."""

import logging

from whitelist_host.classify import LineRef, SessionTracker
from whitelist_host.logparse import parse_line

UUID_J = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_B = "00000000-0000-0000-0009-01f64f6dd58e"

AUTH_J = f"[12:00:00 INFO]: UUID of player Foo_Bar is {UUID_J}"
JOIN_J = "[12:00:01 INFO]: Foo_Bar[/192.168.1.9:51000] logged in with entity id 123 at ([world]1.5, 64.0, 8.5)"
REJECT_J = "[12:00:01 INFO]: Foo_Bar (/192.168.1.9:51000) lost connection: You are not white-listed on this server!"
BAN_J = "[12:00:01 INFO]: Foo_Bar (/192.168.1.9:51000) lost connection: You are banned from this server!"
KICK_PROFILE = (
    "[12:00:00 INFO]: Disconnecting com.mojang.authlib.GameProfile@1a2b"
    f"[id={UUID_J},name=Foo_Bar,properties={{}},legacy=false] "
    "(/192.168.1.9:51000): You are not white-listed on this server!"
)
LOST_PROFILE = (
    "[12:00:00 INFO]: com.mojang.authlib.GameProfile@1a2b"
    f"[id={UUID_J},name=Foo_Bar,properties={{}},legacy=false] "
    "(/192.168.1.9:51000) lost connection: You are not white-listed on this server!"
)
AUTH_B = f"[12:00:00 INFO]: [floodgate] Floodgate player logged in as .Cave_Johnson joined (UUID: {UUID_B})"
KICK_B = "[12:00:02 INFO]: Disconnecting .Cave_Johnson (/192.168.1.50:61000): You are not white-listed on this server!"
QUIT_LINE = "[12:10:00 INFO]: Foo_Bar (/192.168.1.9:51000) lost connection: Disconnected"


def feed_lines(tracker, lines, now=1000):
    completed = []
    for i, line in enumerate(lines):
        events, missed = parse_line(line)
        assert not missed, f"line failed to parse: {line!r}"
        for ev in events:
            completed.extend(tracker.feed(ev, LineRef(inode=7, offset=100 * i), now))
    return completed


def test_java_joined_is_normal_and_silent(caplog):
    tracker = SessionTracker(window_s=45)
    with caplog.at_level(logging.WARNING):
        done = feed_lines(tracker, [AUTH_J, JOIN_J])
    assert [c.outcome for c in done] == ["joined"]
    assert done[0].uuid == UUID_J
    assert done[0].platform == "java"
    assert caplog.records == []  # normal traffic never warns


def test_java_whitelist_rejection_pairs():
    tracker = SessionTracker(window_s=45)
    done = feed_lines(tracker, [AUTH_J, REJECT_J])
    assert [c.outcome for c in done] == ["whitelist_rejected"]
    assert done[0].raw_username == "Foo_Bar"


def test_java_ban_is_other_rejected():
    tracker = SessionTracker(window_s=45)
    done = feed_lines(tracker, [AUTH_J, BAN_J])
    assert [c.outcome for c in done] == ["other_rejected"]


def test_single_line_profile_rejections_qualify():
    for line in (KICK_PROFILE, LOST_PROFILE):
        tracker = SessionTracker(window_s=45)
        done = feed_lines(tracker, [line])
        assert [c.outcome for c in done] == ["whitelist_rejected"], line
        assert done[0].uuid == UUID_J
        assert done[0].platform == "java"


def test_bedrock_floodgate_pairing():
    tracker = SessionTracker(window_s=45)
    done = feed_lines(tracker, [AUTH_B, KICK_B])
    assert [c.outcome for c in done] == ["whitelist_rejected"]
    assert done[0].platform == "bedrock"
    assert done[0].raw_username == ".Cave_Johnson"  # exact log token, prefix intact
    assert done[0].uuid == UUID_B


def test_unresolved_times_out_with_warning(caplog):
    tracker = SessionTracker(window_s=45)
    feed_lines(tracker, [AUTH_J], now=1000)
    with caplog.at_level(logging.WARNING):
        done = tracker.expire(now=1050)
    assert [c.outcome for c in done] == ["unresolved"]
    assert any("unresolved" in r.message for r in caplog.records)


def test_terminal_without_session_is_silent(caplog):
    tracker = SessionTracker(window_s=45)
    with caplog.at_level(logging.WARNING):
        done = feed_lines(tracker, [QUIT_LINE, JOIN_J])
    assert done == []
    assert caplog.records == []  # long-joined players quitting is not noteworthy


def test_unparseable_relevant_line_is_flagged():
    events, missed = parse_line("[12:00:00 INFO]: Foo_Bar lost connection unexpectedly weird format")
    assert events == []
    assert missed is True


def test_ordinary_chatter_is_ignored():
    events, missed = parse_line("[12:00:00 INFO]: Preparing spawn area: 95%")
    assert events == [] and missed is False


def test_event_ids_are_deterministic():
    t1 = SessionTracker(window_s=45)
    t2 = SessionTracker(window_s=45)
    a = feed_lines(t1, [AUTH_J, REJECT_J], now=1000)
    b = feed_lines(t2, [AUTH_J, REJECT_J], now=2000)  # replay later
    assert a[0].event_id == b[0].event_id  # same bytes → same id (§6)


def test_reauth_supersedes_as_unresolved(caplog):
    tracker = SessionTracker(window_s=45)
    with caplog.at_level(logging.WARNING):
        done = feed_lines(tracker, [AUTH_J, AUTH_J])
    assert [c.outcome for c in done] == ["unresolved"]
    assert any("superseded" in r.message for r in caplog.records)
