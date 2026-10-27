// How the notebook keyed and packed each answer — the other half of
// core/src/fabribuddy_intenant/payloads.py. Change both together.
//
// Key: the request path, percent-decoded, plus its query parameters decoded,
// sorted and joined as k=v&k=v; then SHA-256 in hex.
// Body: JSON, gzip, base64, in chunks of at most 4,000 characters.

export function normalize(url: string): string {
  const [rawPath, rawQuery = ""] = url.split("?", 2);
  const pairs = [...new URLSearchParams(rawQuery).entries()]
    .filter(([, v]) => v !== "")
    .sort(([ka, va], [kb, vb]) => (ka < kb ? -1 : ka > kb ? 1 : va < vb ? -1 : va > vb ? 1 : 0));
  let canonical = safeDecode(rawPath);
  if (pairs.length) canonical += "?" + pairs.map(([k, v]) => `${k}=${v}`).join("&");
  return canonical;
}

function safeDecode(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}

export async function key(url: string): Promise<string> {
  const bytes = new TextEncoder().encode(normalize(url));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function unpack(chunks: string[]): Promise<unknown> {
  const binary = atob(chunks.join(""));
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return JSON.parse(await new Response(stream).text());
}
