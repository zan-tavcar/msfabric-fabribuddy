/**
 * First-touch attribution: where a visitor came from, kept in the browser until
 * sign-up sends it with the new organisation. The website passes its own tags
 * on to the app link (ref, utm_*, gclid, wref = the site's referrer, landing).
 */
const KEY = "fl_source";
const PARAMS = [
  "ref",
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_term",
  "utm_content",
  "gclid",
] as const;

export function captureSource(): void {
  try {
    if (window.localStorage.getItem(KEY)) return; // first touch wins
    const params = new URLSearchParams(window.location.search);
    const source: Record<string, string> = {};
    for (const name of PARAMS) {
      const value = params.get(name);
      if (value) source[name] = value.slice(0, 200);
    }
    const referrer = params.get("wref") || document.referrer;
    if (referrer) source.referrer = referrer.slice(0, 200);
    const landing = params.get("landing");
    if (landing) source.landing = landing.slice(0, 200);
    if (Object.keys(source).length) window.localStorage.setItem(KEY, JSON.stringify(source));
  } catch {
    // storage blocked: attribution is best-effort, never a reason to fail
  }
}

export function storedSource(): Record<string, string> {
  try {
    return JSON.parse(window.localStorage.getItem(KEY) || "{}");
  } catch {
    return {};
  }
}
