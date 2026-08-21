"""Log line → structured events.

IMPORTANT (§0 / §10.2): the regexes here are BEST-EFFORT DEFAULTS for a
recent Paper + Geyser/Floodgate stack. Exact log wording varies by version.
Before trusting the matcher, capture real lines — a rejected Java login, a
rejected Bedrock login, and a successful join of each — drop them under
host/captured/, and run:

    python -m whitelist_host.logparse check host/captured/*.log

which reports, line by line, what parsed as what and what did not parse.
Adjust the patterns until the four captured scenarios classify correctly.

The classifier (classify.py) is deliberately line-format-agnostic: it
consumes the events emitted here, so pattern fixes never touch the state
machine.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

# Terminal-disconnect reason → coarse class. Only 'whitelist' can qualify (§3).
_REASON_CLASSES: list[tuple[str, str]] = [
    ("white-listed", "whitelist"),
    ("whitelisted", "whitelist"),
    ("whitelist", "whitelist"),
    ("banned", "ban"),
    ("full", "full"),
    ("outdated", "version"),
    ("incompatible", "version"),
]


def classify_reason(reason: str) -> str:
    lowered = reason.lower()
    for needle, cls in _REASON_CLASSES:
        if needle in lowered:
            return cls
    return "other"


@dataclass(frozen=True)
class LogEvent:
    kind: str  # 'auth' | 'join' | 'disconnect'
    raw_username: str
    time_str: str
    platform: str | None = None  # auth only: 'java' | 'bedrock'
    uuid: str | None = None
    xuid: str | None = None
    reason_class: str | None = None  # disconnect only


# Strip the log header. Two shapes are accepted:
#   [12:34:56 INFO]: message                       (Paper)
#   [12:34:56] [Server thread/INFO]: message       (log4j classic)
_HEADERS = [
    re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)? [A-Z]+\]:? (?P<msg>.*)$"),
    re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)?\] \[[^\]]+\]:? (?P<msg>.*)$"),
]

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Java authenticated identity: printed for every online-mode login before the
# allowlist check runs. This is the authenticated half of a Java session.
_JAVA_AUTH = re.compile(rf"^UUID of player (?P<name>\S+) is (?P<uuid>{_UUID})$")

# Floodgate authenticated identity (Bedrock). The name is the server-visible
# prefixed form (e.g. .Cave_Johnson). Wording varies across Floodgate
# versions — validate against captured lines.
_FLOODGATE_AUTH = [
    re.compile(
        rf"^\[floodgate\] Floodgate player logged in as (?P<name>.+?) joined"
        rf"(?: \(UUID: (?P<uuid>{_UUID})(?:, XUID: (?P<xuid>\d+))?\))?$"
    ),
    re.compile(
        rf"^\[floodgate\] Floodgate player (?P<name>.+?) \(UUID: (?P<uuid>{_UUID})"
        rf"(?:, XUID: (?P<xuid>\d+))?\) has connected$"
    ),
]

# Successful join (both platforms; Bedrock joins via Geyser produce it too).
_JOIN = re.compile(r"^(?P<name>.+?)\[/[^\]]*\] logged in with entity id \d+")

# Pre-join disconnect carrying the full GameProfile — a single line that
# proves authentication AND carries the rejection reason.
_PROFILE_LOST = re.compile(
    rf"^com\.mojang\.authlib\.GameProfile@\S*?\[id=(?P<uuid>{_UUID}),"
    rf"name=(?P<name>[^,\]]+),.*?\] \(/[^)]*\) lost connection: (?P<reason>.*)$"
)

# Ordinary terminal events, paired with an earlier auth event by username.
_NAME_LOST = re.compile(r"^(?P<name>.+?) \(/[^)]*\) lost connection: (?P<reason>.*)$")
_DISCONNECTING = re.compile(r"^Disconnecting (?P<who>.+?): (?P<reason>.*)$")
_WHO_WITH_ADDR = re.compile(r"^(?P<name>.+?) \(/[^)]*\)$")
_WHO_PROFILE = re.compile(rf"^com\.mojang\.authlib\.GameProfile@\S*?\[id=(?P<uuid>{_UUID}),name=(?P<name>[^,\]]+),.*$")

# A line matching one of these but no full pattern is "unparseable but
# relevant" — the classifier WARNs on it (§3). Everything else is ordinary
# server chatter and is ignored silently.
_RELEVANT_HINTS = re.compile(
    r"UUID of player|lost connection|Disconnecting |\[floodgate\]|logged in with entity id",
    re.IGNORECASE,
)


def parse_line(line: str) -> tuple[list[LogEvent], bool]:
    """Parse one raw log line.

    Returns (events, relevant_but_unparsed). The second element is True only
    when the line looks like login/disconnect traffic but no pattern fully
    matched — the classifier turns that into a WARN instead of a silent miss.
    """
    line = line.rstrip("\r\n")
    msg = None
    time_str = ""
    for header in _HEADERS:
        m = header.match(line)
        if m:
            time_str = m.group("time")
            msg = m.group("msg")
            break
    if msg is None:
        return [], bool(_RELEVANT_HINTS.search(line))

    m = _JAVA_AUTH.match(msg)
    if m:
        return [
            LogEvent(
                kind="auth",
                platform="java",
                raw_username=m.group("name"),
                uuid=m.group("uuid").lower(),
                time_str=time_str,
            )
        ], False

    for pat in _FLOODGATE_AUTH:
        m = pat.match(msg)
        if m:
            uuid = m.group("uuid")
            return [
                LogEvent(
                    kind="auth",
                    platform="bedrock",
                    raw_username=m.group("name"),
                    uuid=uuid.lower() if uuid else None,
                    xuid=m.groupdict().get("xuid"),
                    time_str=time_str,
                )
            ], False

    m = _PROFILE_LOST.match(msg)
    if m:
        # One line carrying both halves: authenticated identity + rejection.
        name = m.group("name")
        uuid = m.group("uuid").lower()
        reason = classify_reason(m.group("reason"))
        return [
            LogEvent(kind="auth", platform="java", raw_username=name, uuid=uuid, time_str=time_str),
            LogEvent(kind="disconnect", raw_username=name, reason_class=reason, time_str=time_str),
        ], False

    m = _JOIN.match(msg)
    if m:
        return [LogEvent(kind="join", raw_username=m.group("name"), time_str=time_str)], False

    m = _DISCONNECTING.match(msg)
    if m:
        who = m.group("who")
        reason = classify_reason(m.group("reason"))
        pm = _WHO_PROFILE.match(who)
        if pm:
            # Full GameProfile in the kick line: authenticated identity and
            # rejection in one line — a self-contained session (Java §3).
            name = pm.group("name")
            uuid = pm.group("uuid").lower()
            return [
                LogEvent(kind="auth", platform="java", raw_username=name, uuid=uuid, time_str=time_str),
                LogEvent(kind="disconnect", raw_username=name, reason_class=reason, time_str=time_str),
            ], False
        am = _WHO_WITH_ADDR.match(who)
        name = am.group("name") if am else who
        return [
            LogEvent(kind="disconnect", raw_username=name, reason_class=reason, time_str=time_str)
        ], False

    m = _NAME_LOST.match(msg)
    if m:
        return [
            LogEvent(
                kind="disconnect",
                raw_username=m.group("name"),
                reason_class=classify_reason(m.group("reason")),
                time_str=time_str,
            )
        ], False

    return [], bool(_RELEVANT_HINTS.search(msg))


def _check_files(paths: list[str]) -> int:
    """CLI: report per-line parse results for captured log files (§10.2)."""
    any_relevant_missed = False
    for path in paths:
        print(f"== {path}")
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                events, missed = parse_line(line)
                for ev in events:
                    extra = "".join(
                        f" {k}={v}"
                        for k, v in (
                            ("platform", ev.platform),
                            ("uuid", ev.uuid),
                            ("xuid", ev.xuid),
                            ("reason", ev.reason_class),
                        )
                        if v
                    )
                    print(f"  {n:>5} {ev.kind:<10} {ev.raw_username!r}{extra}")
                if missed:
                    any_relevant_missed = True
                    print(f"  {n:>5} !UNPARSED  {line.rstrip()!r}")
    if any_relevant_missed:
        print(
            "\nSome login/disconnect-looking lines did not parse. Adjust the "
            "patterns in whitelist_host/logparse.py until captured scenarios "
            "classify correctly."
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "check":
        return _check_files(argv[1:])
    print("usage: python -m whitelist_host.logparse check <captured.log> [...]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
