// Opaque conversation id so the URL never exposes the platform name.
// Encodes "<platform>:<id>" as URL-safe base64.

export function encodeConvId(platform: string, id: number | string): string {
  const raw = `${platform}:${id}`;
  return btoa(unescape(encodeURIComponent(raw)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export function decodeConvId(cid: string): { platform: string; id: string } {
  try {
    const b64 = cid.replace(/-/g, "+").replace(/_/g, "/");
    const raw = decodeURIComponent(escape(atob(b64)));
    const i = raw.indexOf(":");
    if (i < 0) return { platform: "", id: "0" };
    return { platform: raw.slice(0, i), id: raw.slice(i + 1) };
  } catch {
    return { platform: "", id: "0" };
  }
}
