// Password gate for the review preview: every page, data file and API route sits behind HTTP Basic auth.
// Fails closed: with no SITE_PASSWORD configured, nothing is served.
const LOCKED = {"cache-control": "no-store", "x-frame-options": "DENY", "x-content-type-options": "nosniff",
  "content-security-policy": "default-src 'none'; frame-ancestors 'none'", "x-robots-tag": "noindex, nofollow"};
const REALM = 'Basic realm="Settistics preview", charset="UTF-8"';

function sameText(a, b) {
  // Compare every character so response time does not reveal how much of the password matched.
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

export function passwordFrom(header) {
  const [scheme, encoded] = (header || "").split(" ");
  if (scheme !== "Basic" || !encoded) return null;
  try {
    const decoded = new TextDecoder().decode(Uint8Array.from(atob(encoded), c => c.charCodeAt(0)));
    return decoded.slice(decoded.indexOf(":") + 1);  // any username is accepted
  } catch {
    return null;
  }
}

// Riot's product-URL verification must reach this one file without logging in.
const PUBLIC_PATHS = new Set(["/riot.txt"]);

export default async (request, context) => {
  if (PUBLIC_PATHS.has(new URL(request.url).pathname)) return context.next();
  const expected = globalThis.Netlify?.env.get("SITE_PASSWORD");
  if (!expected) {
    return new Response("This preview is locked: no password has been configured.", {status: 503, headers: LOCKED});
  }
  const supplied = passwordFrom(request.headers.get("authorization"));
  if (supplied === null || !sameText(supplied, expected)) {
    return new Response("Password required.", {status: 401, headers: {...LOCKED, "www-authenticate": REALM}});
  }
  const response = await context.next();
  response.headers.set("x-robots-tag", "noindex, nofollow");
  response.headers.set("cache-control", "private, no-store");
  return response;
};

export const config = {path: "/*"};
