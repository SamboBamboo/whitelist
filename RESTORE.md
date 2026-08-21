# Whitelist Gateway — restore & push instructions

This package contains the complete two-halves build, two ways:

- `whitelist/` — the plain source tree (browse it, use it directly)
- `whitelist-two-halves.bundle` — the same work as real git history
  (2 commits on branch `claude/project-two-halves-l4qqch`)

## Why you're holding a zip instead of seeing a branch on GitHub

Every push from the Claude session was refused by GitHub with 403
("Permission to SamboBamboo/whitelist.git denied to barther"). Adding
yourself as a collaborator creates an **invitation** — it grants nothing
until it's accepted from your own account:

1. Sign in to github.com as **barther**.
2. Open https://github.com/SamboBamboo/whitelist — an invitation banner
   appears (also at https://github.com/notifications, and by email).
3. Accept it. Push access is live immediately after.

## Getting the branch onto GitHub (preserves the commit history)

From any machine with git, using the bundle:

```sh
git clone whitelist-two-halves.bundle -b claude/project-two-halves-l4qqch whitelist-repo
cd whitelist-repo
git remote set-url origin https://github.com/SamboBamboo/whitelist
git push -u origin claude/project-two-halves-l4qqch
```

(Or, if you'd rather it just be the main branch of the repo:
`git push origin claude/project-two-halves-l4qqch:main`.)

Alternatively, once the invitation is accepted, just tell the Claude
session to retry — it still has the commits and will push them itself.

## Verifying the code before trusting it

```sh
cd whitelist/worker && npm install && npm test && npm run typecheck
cd whitelist/host && python3 -m venv .venv && .venv/bin/pip install -e .[dev,management] && .venv/bin/pytest
```

Expected: 18 worker tests, 91 host tests, all green.

Start with `whitelist/README.md` — it maps the spec's build order to the
code and lists the two steps that still need the live Minecraft server
(the §0 capability probe and real log capture) before the gateway should
be trusted.
