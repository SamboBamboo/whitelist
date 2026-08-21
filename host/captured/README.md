# Captured log lines (§0 / §10.2)

The matcher's log patterns are best-effort defaults for a recent
Paper + Geyser/Floodgate stack. **Do not trust them until they are validated
against this server's real output.** Log wording changes across versions, and
it breaks silently — as a non-match, not as an error.

## What to capture

With the whitelist ON, produce and save each of these from
`logs/latest.log`:

1. a **rejected Java** login (unwhitelisted Java account tries to join)
2. a **rejected Bedrock** login (unwhitelisted Xbox account via Geyser)
3. a **successful Java** join (whitelisted account)
4. a **successful Bedrock** join (whitelisted account)

Copy the relevant line ranges (a few seconds around each event) into files
in this directory, e.g. `java-rejected.log`, `bedrock-rejected.log`,
`java-joined.log`, `bedrock-joined.log`.

## How to validate

```
python -m whitelist_host.logparse check host/captured/*.log
```

Every login/disconnect-looking line must parse, and the four scenarios must
classify as `auth`+`disconnect` (rejections) and `auth`+`join` (joins), with
UUIDs captured. Anything printed as `!UNPARSED` means the patterns in
`whitelist_host/logparse.py` need adjusting — the session state machine in
`classify.py` does not change.

Also note here, for the record, the Floodgate settings the capture was made
under (they feed the §4 drift guard):

- `username-prefix`: 
- `replace-spaces`: 
- Paper version / Floodgate version:

Files in this directory are gitignored except this README — captured lines
contain player IPs and identities.
