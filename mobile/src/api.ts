import type {
  ConversationsResponse,
  MessagesResponse,
  SendResponse,
  TranslateResponse,
} from "./types";

// Base URL of the PC admin backend. Empty by default so requests hit the same
// origin (the Vite dev proxy forwards /api to the backend). For a cross-origin
// production deployment set VITE_API_BASE to the backend origin.
const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

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

export function translateMessages(
  messages: { id: string; text: string }[],
): Promise<TranslateResponse> {
  return postJson<TranslateResponse>("/api/m/translate", { messages });
}

export function sendReply(params: {
  platform: string;
  id: number;
  message: string;
  target_lang: string;
}): Promise<SendResponse> {
  return postJson<SendResponse>("/api/m/send", params);
}
