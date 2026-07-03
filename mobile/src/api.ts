import type {
  ConversationsResponse,
  DeliverResponse,
  MessagesResponse,
  PhrasesResponse,
  SendResponse,
  TranslateResponse,
  VerifyCodeResponse,
} from "./types";

// Base URL of the PC admin backend. Empty by default so requests hit the same
// origin (the Vite dev proxy forwards /api to the backend). For a cross-origin
// production deployment set VITE_API_BASE to the backend origin.
const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

// Resolve a possibly backend-relative URL (e.g. a "/assets/..." brand logo)
// against the backend origin. Absolute URLs are returned unchanged.
export function resolveBackendUrl(url: string): string {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

export function fetchConversations(): Promise<ConversationsResponse> {
  return getJson<ConversationsResponse>("/api/m/conversations");
}

export function fetchMessages(params: {
  platform: string;
  id: number;
  name?: string;
  product?: string;
  email?: string;
}): Promise<MessagesResponse> {
  const query = new URLSearchParams({
    platform: params.platform,
    id: String(params.id),
  });
  if (params.name) query.set("name", params.name);
  if (params.product) query.set("product", params.product);
  if (params.email) query.set("email", params.email);
  return getJson<MessagesResponse>(`/api/m/messages?${query.toString()}`);
}

export async function verifyCode(code: string): Promise<VerifyCodeResponse> {
  // Unlike getJson, keep the JSON body on non-2xx so the backend's error
  // message (invalid format / not found) can be shown to the user.
  const res = await fetch(
    `${API_BASE}/api/m/verify_code?code=${encodeURIComponent(code)}`,
    { cache: "no-store" },
  );
  return (await res.json()) as VerifyCodeResponse;
}

export function fetchPhrases(): Promise<PhrasesResponse> {
  return getJson<PhrasesResponse>("/api/m/phrases");
}

export function translateMessages(
  messages: { id: string; text: string }[],
): Promise<TranslateResponse> {
  return postJson<TranslateResponse>("/api/m/translate", { messages });
}

// Send a reply. Text-only replies go as JSON; when images are attached the
// whole reply (text + files) is sent as multipart/form-data. The JSON body is
// kept even on non-2xx so the backend error message can be shown.
export async function sendReply(params: {
  platform: string;
  id: number;
  message: string;
  target_lang: string;
  files?: File[];
  phrase_id?: string;
}): Promise<SendResponse> {
  const url = `${API_BASE}/api/m/send`;
  let res: Response;
  if (params.files && params.files.length > 0) {
    const form = new FormData();
    form.set("platform", params.platform);
    form.set("id", String(params.id));
    form.set("message", params.message);
    form.set("target_lang", params.target_lang);
    if (params.phrase_id) form.set("phrase_id", params.phrase_id);
    for (const file of params.files) form.append("files", file);
    res = await fetch(url, { method: "POST", body: form });
  } else {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform: params.platform,
        id: params.id,
        message: params.message,
        target_lang: params.target_lang,
        ...(params.phrase_id ? { phrase_id: params.phrase_id } : {}),
      }),
    });
  }
  return (await res.json()) as SendResponse;
}

// Mark a GGSEL order as delivered (or back to pending), mirroring the seller
// panel's "Product delivered" toggle. Keep the JSON body on non-2xx so the
// backend's error message can be surfaced to the user.
export async function setDelivered(params: {
  platform: string;
  id: number;
  delivered: boolean;
}): Promise<DeliverResponse> {
  const res = await fetch(`${API_BASE}/api/m/deliver`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return (await res.json()) as DeliverResponse;
}
