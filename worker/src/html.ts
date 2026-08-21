// Server-rendered pages. No build step, no client framework; the only
// external resource is the Turnstile widget script on the form page.

export function escapeHtml(s: string): string {
  return s
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

const CSS = `
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 1.5rem 1rem 4rem;
  background: #f4f2ec; color: #232019;
  display: flex; justify-content: center;
}
@media (prefers-color-scheme: dark) {
  body { background: #191714; color: #ece7dc; }
  .card { background: #211e1a !important; border-color: #3a352d !important; }
  .step, .callout { background: #2a2620 !important; border-color: #3a352d !important; }
  input, select { background: #191714; color: #ece7dc; border-color: #4a443a !important; }
  a { color: #8fc97a; }
}
main { max-width: 42rem; width: 100%; }
h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
h1 .mc { color: #3e8e2f; }
h2 { font-size: 1.1rem; margin: 1.5rem 0 0.5rem; }
.sub { margin: 0 0 1.25rem; opacity: 0.75; }
.card {
  background: #fffdf8; border: 1px solid #ddd6c8; border-radius: 10px;
  padding: 1.25rem; margin: 1rem 0;
}
.callout {
  border: 2px solid #c98a2b; border-radius: 10px; background: #fbf3e3;
  padding: 1rem 1.25rem; margin: 1rem 0;
}
.callout strong { color: #a36514; }
label { display: block; font-weight: 600; margin: 0.9rem 0 0.25rem; }
label small { font-weight: 400; opacity: 0.7; }
input[type=text], input[type=email], select {
  width: 100%; padding: 0.55rem 0.7rem; border: 1px solid #bdb5a4;
  border-radius: 7px; font-size: 1rem;
}
fieldset { border: none; padding: 0; margin: 0.9rem 0 0; }
fieldset legend { font-weight: 600; padding: 0; margin-bottom: 0.25rem; }
.radio-row { display: flex; gap: 1.25rem; }
.radio-row label { font-weight: 400; margin: 0; display: flex; gap: 0.4rem; align-items: center; }
button {
  margin-top: 1.25rem; padding: 0.65rem 1.4rem; font-size: 1.05rem;
  background: #3e8e2f; color: #fff; border: none; border-radius: 8px; cursor: pointer;
}
button:hover { background: #347526; }
.error { color: #b3261e; font-weight: 600; }
.step { border: 1px solid #ddd6c8; border-radius: 10px; padding: 0.9rem 1.1rem; margin: 0.6rem 0; background: #fffdf8; }
.how { margin: 0.45rem 0; }
.small { font-size: 0.9rem; opacity: 0.8; }
details { margin: 0.6rem 0 0.2rem; }
details summary { cursor: pointer; font-weight: 600; color: #a36514; }
.step b.n, .how b.n { display: inline-block; background: #3e8e2f; color: #fff; border-radius: 50%;
  width: 1.5rem; height: 1.5rem; text-align: center; line-height: 1.5rem; margin-right: 0.5rem; flex: none; }
code, .mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
.addr { font-size: 1.15rem; font-weight: 700; }
.statusline { display: flex; gap: 0.6rem; align-items: baseline; margin: 0.4rem 0; }
.dot { width: 0.7rem; height: 0.7rem; border-radius: 50%; flex: none; position: relative; top: 0.05rem; }
.dot.done { background: #3e8e2f; }
.dot.todo { background: #bdb5a4; }
.dot.bad { background: #b3261e; }
.biglink { font-size: 1.05rem; word-break: break-all; }
footer { margin-top: 2rem; font-size: 0.85rem; opacity: 0.6; }
`;

export function page(title: string, body: string): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${CSS}</style>
</head>
<body>
<main>
${body}
<footer>Applications are reviewed by a human. Personal details are deleted 90 days after a request is decided or expires.</footer>
</main>
</body>
</html>`;
}

export function htmlResponse(
  body: string,
  status = 200,
  extraHeaders: Record<string, string> = {},
): Response {
  return new Response(body, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'referrer-policy': 'no-referrer',
      'x-content-type-options': 'nosniff',
      ...extraHeaders,
    },
  });
}

export interface ConnectCopy {
  serverAddress: string;
  bedrockPort: string;
}

// Short per-platform "how to try joining" cards. Shown where they are
// actionable: after submitting (only the chosen platform) and on a pending
// status page. On the form itself they sit folded inside <details>.
export function joinHelp(c: ConnectCopy, platform?: string): string {
  const addr = escapeHtml(c.serverAddress);
  const java = `
<div class="step"><b class="n">J</b><strong>Java:</strong>
<em>Multiplayer → Add Server</em> → <span class="mono addr">${addr}</span> → try to join.</div>`;
  const bedrock = `
<div class="step"><b class="n">B</b><strong>Bedrock (phone / tablet / Windows):</strong>
<em>Play → Servers → Add Server</em> → address <span class="mono addr">${addr}</span>,
port <span class="mono">${escapeHtml(c.bedrockPort)}</span> → try to join.</div>`;
  const chosen =
    platform === 'java' ? java : platform === 'bedrock' ? bedrock : java + bedrock;
  return `${chosen}
<p class="small">Getting told you're not whitelisted is the goal — that's the moment we're
waiting for. Then close the game; you're done.</p>`;
}

// The §5 requirement, minus the essay: the form states prominently that the
// applicant must try to connect, WILL be refused, and that the refusal is
// required — in three short lines a kid will actually read. Console policy
// stays stated plainly before any fields.
export function howItWorks(c: ConnectCopy): string {
  return `
<div class="callout">
<div class="how"><b class="n">1</b> Send this form.</div>
<div class="how"><b class="n">2</b> Open Minecraft and try to join
<span class="mono addr">${escapeHtml(c.serverAddress)}</span>.
<strong>You'll get rejected — on purpose.</strong> That rejection is how we know the
account is really yours.</div>
<div class="how"><b class="n">3</b> A human says yes, and you're in.</div>
<details><summary>Show me how to try joining</summary>${joinHelp(c)}</details>
<p class="small">On Xbox, PlayStation, or Switch only? Consoles can't add this server, so
this form won't work for you — message the admin instead.</p>
</div>`;
}
