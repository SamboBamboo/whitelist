"""§0 capability probe: decides which §7 implementation path is safe.

Run BEFORE building on any tier. The Management Protocol test is END-TO-END,
not schema-level — Floodgate ships its own `fwhitelist` precisely because
Bedrock whitelist handling has had friction, and Floodgate may intercept the
admission check rather than deferring to the vanilla allowlist. A write that
succeeds and reads back but does not change admission behavior is a failure
that looks like success. Step 3 (a real account actually being admitted) is
the one that matters, and only a human can observe it.

Usage:
    whitelist-probe checklist
    whitelist-probe discover  --url ws://127.0.0.1:25585 --secret-file /path
    whitelist-probe add-test  --url ... --secret-file ... \
        --name .Cave_Johnson --uuid 00000000-0000-0000-0009-01f64f6dd58e \
        [--keep]
    whitelist-probe classify  <captured.log ...>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CHECKLIST = """\
§0 preconditions — verify ALL of these before trusting the gateway
==================================================================

Security invariants (the ownership proof rests on the first two):

 [ ] online-mode=true in server.properties.
     If false, a Java login attempt proves nothing — anyone can connect
     under any username and the whole ownership proof is worthless.

 [ ] Geyser auth-type: floodgate.
     Same reasoning for Bedrock: this is what makes the connecting identity
     Xbox-authenticated.

 [ ] Management/RCON interfaces are NOT reachable from any non-loopback
     interface. Do not assume a config knob exists — verify empirically:
         ss -ltnp                       # on the Minecraft host
     and attempt a connection from ANOTHER LAN machine. If anything listens
     on a wildcard or LAN address, enforce loopback-only reachability with
     the host firewall.

 [ ] mine.sambonius.net is DNS-only (grey cloud) at Cloudflare. Minecraft is
     not HTTP; proxying does not carry it, and Spectrum covers Java but not
     Bedrock. Game-traffic protection belongs at the UniFi/firewall layer.

Capability probe (decides §7's implementation tier):

 [ ] server.properties has:
         management-server-enabled=true
         management-server-host=localhost
         management-server-port=25585
     …then run `whitelist-probe discover` and `whitelist-probe add-test`.

 [ ] Confirm the protocol is present on YOUR Paper build — third-party
     plugins backport it, so native availability is not uniform.

Also capture up front (§10.2):

 [ ] Real log lines for a rejected Java login, a rejected Bedrock login,
     AND a successful join of each → host/captured/, then run:
         python -m whitelist_host.logparse check host/captured/*.log
 [ ] Floodgate's actual username-prefix and replace-spaces settings.
"""


def cmd_checklist(_args) -> int:
    print(CHECKLIST)
    return 0


def _client(args):
    from .mgmt import ManagementClient

    secret = Path(args.secret_file).read_text().strip()
    return ManagementClient(args.url, secret)


def cmd_discover(args) -> int:
    client = _client(args)
    schema = client.discover()
    methods = []
    if isinstance(schema, dict):
        methods = [m.get("name", "?") for m in schema.get("methods", [])]
    print(f"rpc.discover returned {len(methods)} methods")
    for name in sorted(methods):
        marker = "  <-- allowlist" if "allowlist" in name else ""
        print(f"  {name}{marker}")
    wanted = [m for m in methods if "allowlist" in m]
    if wanted:
        print("\nAllowlist methods present. Confirm the player-object field names")
        print("({name, id} is assumed by this code) in the schema before add-test:")
        if isinstance(schema, dict):
            for m in schema.get("methods", []):
                if "allowlist" in m.get("name", ""):
                    print(json.dumps(m, indent=2)[:2000])
    else:
        print("\nNo allowlist methods — fall back to Tier 2 (RCON) or Tier 3 (file).")
    return 0


def cmd_add_test(args) -> int:
    """§0 end-to-end steps 1–2, then instructions for the human-only step 3."""
    client = _client(args)

    print(f"1) minecraft:allowlist/add with the captured profile "
          f"{{name: {args.name!r}, uuid: {args.uuid!r}}} …")
    client.allowlist_add(args.name, args.uuid)
    print("   add call did not error (and involved no Mojang name resolution).")

    print("2) reading back via minecraft:allowlist/ …")
    entries = client.allowlist()
    from .allowlist import uuid_present

    normalized = [
        {"name": e.get("name", ""), "uuid": e.get("id") or e.get("uuid", "")}
        for e in entries
    ]
    if uuid_present(normalized, args.uuid):
        print("   entry reads back. Steps 1–2 PASS.")
    else:
        print("   ENTRY DID NOT READ BACK — Tier 1 FAILS on this build. "
              "Fall back to Tier 2/3.")
        return 1

    print("""
3) THE STEP THAT MATTERS — a human must observe it:
   Have the real account connect now and confirm it is ACTUALLY ADMITTED.
   Floodgate may intercept the admission check rather than deferring to the
   vanilla allowlist; a write that reads back but does not change admission
   behavior is a failure that looks like success.

   Repeat this whole test for a Java profile and for a Bedrock (Floodgate)
   profile before setting allowlist backend = "management" in config.toml.
""")
    if not args.keep:
        client.allowlist_remove(args.name, args.uuid)
        still = [
            {"name": e.get("name", ""), "uuid": e.get("id") or e.get("uuid", "")}
            for e in client.allowlist()
        ]
        if uuid_present(still, args.uuid):
            print("cleanup: REMOVE DID NOT STICK — investigate before trusting Tier 1.")
            return 1
        print("cleanup: test entry removed (read-back confirmed).")
    else:
        print("--keep: test entry left in place for the step-3 join test. "
              "Remove it afterwards via the admin app.")
    return 0


def cmd_classify(args) -> int:
    from .logparse import main as logparse_main

    return logparse_main(["check", *args.files])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whitelist-probe", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("checklist", help="print the §0 manual checklist")

    for name, fn in (("discover", cmd_discover), ("add-test", cmd_add_test)):
        p = sub.add_parser(name)
        p.add_argument("--url", default="ws://127.0.0.1:25585")
        p.add_argument("--secret-file", required=True,
                       help="file containing management-server-secret")
        if name == "add-test":
            p.add_argument("--name", required=True,
                           help="raw username exactly as logged (Bedrock: with prefix)")
            p.add_argument("--uuid", required=True, help="captured UUID from the log")
            p.add_argument("--keep", action="store_true",
                           help="leave the entry in place for the step-3 join test")
        p.set_defaults(fn=fn)

    p = sub.add_parser("classify", help="run captured log lines through the parser")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_classify)

    args = parser.parse_args(argv)
    if args.cmd == "checklist":
        return cmd_checklist(args)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
