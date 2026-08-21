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
.step b.n { display: inline-block; background: #3e8e2f; color: #fff; border-radius: 50%;
  width: 1.5rem; height: 1.5rem; text-align: center; line-height: 1.5rem; margin-right: 0.5rem; }
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

// The deliberate-rejection instructions (§5): must be prominent, and must
// frame the refused connection as a required step, not an error.
export function connectInstructions(c: ConnectCopy, platform?: string): string {
  const java = `
<div class="step"><b class="n">J</b><strong>Java Edition:</strong>
open <em>Multiplayer → Add Server</em>, enter
<span class="mono addr">${escapeHtml(c.serverAddress)}</span>, then try to join it.</div>`;
  const bedrock = `
<div class="step"><b class="n">B</b><strong>Bedrock (phone / tablet / Windows):</strong>
open <em>Play → Servers → Add Server</em>, enter address
<span class="mono addr">${escapeHtml(c.serverAddress)}</span> and port
<span class="mono">${escapeHtml(c.bedrockPort)}</span>, then try to join it.</div>
<div class="step"><b class="n">!</b><strong>Bedrock on Xbox / PlayStation / Switch: not supported.</strong>
Consoles cannot add custom servers, so a console cannot complete this step — and could
not join the server afterwards either. If you only play on console, please do not fill
in this form; contact the server admin directly instead. If you also play on a phone,
tablet, or computer with the same Microsoft account, apply from that device.</div>`;
  const chosen =
    platform === 'java' ? java : platform === 'bedrock' ? bedrock : java + bedrock;
  return `
<div class="callout">
<strong>Step 2 is deliberately weird — read this.</strong>
<p>After you submit the form, you must <strong>attempt to connect</strong> to the server.
<strong>The connection will be refused.</strong> That refusal is a <em>required step</em>,
not an error: it is how we confirm the account you named is really yours, because only
the account holder can knock on the door as that account.</p>
${chosen}
<p>Once we see your refused connection attempt, your request is marked verified and goes
to a human for approval. You'll be able to watch this on your status page.</p>
</div>`;
}
