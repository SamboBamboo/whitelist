// Public form (GET /) and submission endpoint (POST /api/submit) — §5.

import type { DBLike, Deps, Env, Platform } from './types.ts';
import { normConfig } from './types.ts';
import { normalizeForm } from './normalize.ts';
import { escapeHtml, howItWorks, htmlResponse, joinHelp, page } from './html.ts';
import { randomToken, sha256Hex } from './crypto.ts';
import { verifyTurnstile } from './turnstile.ts';
import { allowSubmission } from './ratelimit.ts';
import { processEmailQueue, queueEmail, receiptKey } from './email.ts';

interface FormValues {
  real_name: string;
  email: string;
  username: string;
  platform: string;
}

const EMPTY: FormValues = { real_name: '', email: '', username: '', platform: '' };

// The whole page carries the "you will be refused, on purpose" framing —
// prominently, before the fields, per §5.
export function renderForm(env: Env, values: FormValues, errors: string[]): string {
  const err = errors.length
    ? `<div class="card"><p class="error">${errors.map(escapeHtml).join('<br>')}</p></div>`
    : '';
  const v = (s: string) => escapeHtml(s);
  const checked = (p: string) => (values.platform === p ? ' checked' : '');
  return page(
    'Whitelist request — mine.sambonius.net',
    `
<h1><span class="mc">mine.sambonius.net</span> — whitelist request</h1>
<p class="sub">Want in? Takes about two minutes.</p>
${howItWorks({ serverAddress: env.SERVER_ADDRESS, bedrockPort: env.BEDROCK_PORT })}
${err}
<form class="card" method="post" action="/api/submit">
  <label for="real_name">Your name <small>— so the admin knows who's asking</small></label>
  <input type="text" id="real_name" name="real_name" required maxlength="80" value="${v(values.real_name)}">

  <label for="email">Email <small>— for a receipt and the decision; nothing else</small></label>
  <input type="email" id="email" name="email" required maxlength="254" value="${v(values.email)}">

  <fieldset>
    <legend>Where do you play?</legend>
    <div class="radio-row">
      <label><input type="radio" name="platform" value="java" required${checked('java')}> Java Edition</label>
      <label><input type="radio" name="platform" value="bedrock"${checked('bedrock')}> Bedrock (phone / tablet / Windows)</label>
    </div>
  </fieldset>

  <label for="username">Minecraft username / Xbox gamertag <small>— exactly as it appears in game</small></label>
  <input type="text" id="username" name="username" required maxlength="32" value="${v(values.username)}">

  <div class="cf-turnstile" data-sitekey="${v(env.TURNSTILE_SITE_KEY)}"></div>
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>

  <button type="submit">Submit request</button>
</form>`,
  );
}

export function formPage(env: Env): Response {
  return htmlResponse(renderForm(env, EMPTY, []), 200, {
    'content-security-policy':
      "default-src 'self'; script-src https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; style-src 'unsafe-inline'; img-src 'self' data:",
  });
}

function validate(values: FormValues): string[] {
  const errors: string[] = [];
  if (values.real_name.length < 1 || values.real_name.length > 80) {
    errors.push('Please give a name (up to 80 characters).');
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email) || values.email.length > 254) {
    errors.push('That email address does not look valid.');
  }
  if (values.platform !== 'java' && values.platform !== 'bedrock') {
    errors.push('Pick Java or Bedrock.');
  } else if (values.platform === 'java') {
    if (!/^[A-Za-z0-9_]{3,16}$/.test(values.username)) {
      errors.push('Java usernames are 3–16 letters, digits, or underscores.');
    }
  } else {
    // Gamertags are looser; bound length and keep it printable ASCII.
    if (!/^[A-Za-z0-9 _.'#-]{1,20}$/.test(values.username)) {
      errors.push(
        "Gamertags can use letters, digits, spaces, and _ . ' # - (up to 20 characters).",
      );
    }
  }
  return errors;
}

function duplicatePage(): string {
  return page(
    'Request already active',
    `
<h1>There's already an active request for that username</h1>
<div class="card">
<p>A pending or verified whitelist request already exists for this username on this
platform. If it's yours, use the status link you were shown when you submitted it —
each request gets exactly one.</p>
<p>If you lost the link, don't worry: nothing more is needed from you until the admin
decides, and decisions are sent by email. If the original request wasn't yours,
contact the server admin.</p>
<p><a href="/">Back to the form</a></p>
</div>`,
  );
}

// §5: the status URL is rendered in the HTTP response immediately, before and
// independent of email delivery. Email is queued separately; if it never
// sends, the applicant still leaves this page with a working link.
function submittedPage(env: Env, statusUrl: string, platform: Platform): string {
  return page(
    'Request received — now get rejected',
    `
<h1>Step 1 done ✔</h1>
<div class="card">
<p><strong>Save this link</strong> — it's your status page, and this is the only copy:</p>
<p class="biglink"><a href="${escapeHtml(statusUrl)}">${escapeHtml(statusUrl)}</a></p>
</div>
<div class="callout">
<strong>Step 2 — go get rejected:</strong>
${joinHelp({ serverAddress: env.SERVER_ADDRESS, bedrockPort: env.BEDROCK_PORT }, platform)}
</div>
<p>After that, a human decides. The answer arrives by email, and the link above always
shows where things stand.</p>`,
  );
}

export async function handleSubmit(
  request: Request,
  env: Env,
  deps: Deps,
): Promise<Response> {
  const db: DBLike = env.DB;
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return htmlResponse(renderForm(env, EMPTY, ['Malformed form submission.']), 400);
  }
  const values: FormValues = {
    real_name: String(form.get('real_name') ?? '').trim(),
    email: String(form.get('email') ?? '').trim(),
    username: String(form.get('username') ?? ''),
    platform: String(form.get('platform') ?? ''),
  };

  const errors = validate(values);
  if (errors.length) return htmlResponse(renderForm(env, values, errors), 400);

  const ip = request.headers.get('cf-connecting-ip');
  const turnstileToken = String(form.get('cf-turnstile-response') ?? '');
  if (!(await verifyTurnstile(deps.fetch, env.TURNSTILE_SECRET, turnstileToken, ip))) {
    return htmlResponse(
      renderForm(env, values, [
        'The human-check failed or expired. Please try it again.',
      ]),
      400,
    );
  }

  if (!(await allowSubmission(db, ip, deps.now()))) {
    return htmlResponse(
      renderForm(env, values, ['Too many requests from your network. Try again in an hour.']),
      429,
    );
  }

  const platform = values.platform as Platform;
  const cfg = normConfig(env);
  const norm = normalizeForm(platform, values.username, cfg);
  if (!norm.ok) {
    return htmlResponse(renderForm(env, values, ['That username is not usable.']), 400);
  }

  const token = randomToken();
  const tokenHash = await sha256Hex(token);
  let submissionId: number;
  try {
    const row = await db
      .prepare(
        `INSERT INTO submissions
           (real_name, email, username, normalized, normalization_version, platform,
            status, token_hash, created_at)
         VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
         RETURNING id`,
      )
      .bind(
        values.real_name,
        values.email,
        values.username,
        norm.normalized,
        cfg.version,
        platform,
        tokenHash,
        deps.now(),
      )
      .first<{ id: number }>();
    if (!row) throw new Error('insert returned no row');
    submissionId = row.id;
  } catch (e) {
    if (String(e).includes('UNIQUE constraint failed')) {
      // uq_active_submission: one active request per (platform, normalized).
      return htmlResponse(duplicatePage(), 409);
    }
    throw e;
  }

  // Queue the receipt and flush it best-effort in the background; the cron
  // retries and also backfills the event if this queue insert never happens.
  const key = receiptKey(submissionId);
  await queueEmail(db, submissionId, 'receipt', key);
  deps.defer(processEmailQueue(db, deps, env, key));

  const statusUrl = `${env.BASE_URL}/status/${token}`;
  return htmlResponse(submittedPage(env, statusUrl, platform), 200, {
    'cache-control': 'no-store',
  });
}
