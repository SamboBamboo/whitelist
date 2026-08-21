// Cloudflare Turnstile server-side verification.

const VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';

export async function verifyTurnstile(
  fetchFn: typeof fetch,
  secret: string,
  token: string,
  remoteIp: string | null,
): Promise<boolean> {
  if (!secret) return false; // unset secret: closed, never open
  if (!token) return false;
  const body = new URLSearchParams({ secret, response: token });
  if (remoteIp) body.set('remoteip', remoteIp);
  try {
    const res = await fetchFn(VERIFY_URL, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { success?: boolean };
    return data.success === true;
  } catch {
    return false;
  }
}
