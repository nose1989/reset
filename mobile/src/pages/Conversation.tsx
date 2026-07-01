import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { fetchMessages, sendReply, translateMessages } from "../api";
import { getCachedUnread, markCachedConversationRead } from "./ConversationList";
import type { Message } from "../types";

// Per-conversation cache of the loaded (and translated) messages. Survives
// navigating back to the list and reopening. Reused instead of re-fetching when
// the list reports the conversation has no new messages (unread === 0).
type CachedConversation = {
  messages: Message[];
  name: string;
  product: string;
  targetLang: string;
};
const messageCache: Record<string, CachedConversation> = {};

export default function Conversation() {
  const { platform = "", id = "0" } = useParams();
  const convId = Number(id);
  const [search] = useSearchParams();
  const navigate = useNavigate();

  const cacheKey = `${platform}:${convId}`;
  // Reuse cached messages only when the list says there are no new messages.
  const cached = messageCache[cacheKey];
  const canUseCache = cached != null && getCachedUnread(platform, convId) === 0;

  const [messages, setMessages] = useState<Message[]>(
    canUseCache ? cached.messages : [],
  );
  const [name, setName] = useState(
    canUseCache ? cached.name : search.get("name") || "会员",
  );
  const [product, setProduct] = useState(
    canUseCache ? cached.product : search.get("product") || "",
  );
  const [targetLang, setTargetLang] = useState(
    canUseCache ? cached.targetLang : "en",
  );
  const [loading, setLoading] = useState(!canUseCache);
  const [error, setError] = useState("");
  const [reply, setReply] = useState("");
  const [sending, setSending] = useState(false);
  const [showOriginal, setShowOriginal] = useState<Record<string, boolean>>({});

  const bottomRef = useRef<HTMLDivElement>(null);

  const runTranslations = useCallback(async (list: Message[]) => {
    const pending = list.filter((m) => m.translate && !m.translated && m.text);
    if (pending.length === 0) return;
    try {
      const data = await translateMessages(
        pending.map((m) => ({ id: m.id, text: m.text })),
      );
      if (!data.ok) return;
      const byId = new Map(data.results.map((r) => [r.id, r]));
      setMessages((prev) =>
        prev.map((m) => {
          const r = byId.get(m.id);
          return r ? { ...m, translated: r.translated, lang: r.label } : m;
        }),
      );
    } catch {
      /* translation is best-effort */
    }
  }, []);

  const load = useCallback(async () => {
    setError("");
    try {
      const data = await fetchMessages({
        platform,
        id: convId,
        name: search.get("name") || undefined,
        product: search.get("product") || undefined,
        email: search.get("email") || undefined,
      });
      if (!data.ok) throw new Error(data.error || "加载失败");
      setMessages(data.messages);
      if (data.name) setName(data.name);
      setProduct(data.product);
      setTargetLang(data.target_lang || "en");
      runTranslations(data.messages);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [platform, convId, search, runTranslations]);

  useEffect(() => {
    // No new messages for this chat → reuse the cached thread, skip the request.
    if (canUseCache) {
      runTranslations(messages);
      return;
    }
    load();
    // Only decide once per opened conversation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey]);

  // Keep the per-conversation cache in sync with what is currently displayed
  // (including translations that arrive after load), so the next open can reuse it.
  useEffect(() => {
    if (!loading) {
      messageCache[cacheKey] = { messages, name, product, targetLang };
    }
  }, [cacheKey, messages, name, product, targetLang, loading]);

  // Opening a chat marks it read on the backend, so drop its unread badge from
  // the cached list too — returning to the list won't show a stale red dot.
  useEffect(() => {
    markCachedConversationRead(platform, convId);
  }, [platform, convId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const submit = async () => {
    const text = reply.trim();
    if (!text || sending) return;
    setSending(true);
    setError("");
    try {
      const data = await sendReply({
        platform,
        id: convId,
        message: text,
        target_lang: targetLang,
      });
      if (!data.ok) throw new Error(data.error || "发送失败");
      setReply("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <button className="icon-btn" onClick={() => navigate("/")} aria-label="返回">
          ‹
        </button>
        <div className="topbar-titlewrap">
          <div className="topbar-title">{name}</div>
          {product && <div className="topbar-sub">{product}</div>}
        </div>
        <button className="icon-btn" onClick={() => load()} aria-label="刷新">
          ⟳
        </button>
      </header>

      {error && <div className="banner error">{error}</div>}

      <div className="msgs">
        {loading ? (
          <div className="empty">加载中…</div>
        ) : messages.length === 0 ? (
          <div className="empty">暂无消息</div>
        ) : (
          messages.map((m) => {
            const isOut = m.direction === "out";
            const hasTranslation = m.translate && !!m.translated;
            const original = showOriginal[m.id];
            return (
              <div key={m.id} className={`row ${isOut ? "out" : "in"}`}>
                <div className="bubble">
                  {m.attachment ? (
                    <Attachment att={m.attachment} text={m.text} />
                  ) : hasTranslation ? (
                    <>
                      <div className="bubble-text">
                        {original ? m.text : m.translated}
                      </div>
                      <div className="bubble-actions">
                        <button
                          className="mini-btn"
                          onClick={() =>
                            setShowOriginal((s) => ({ ...s, [m.id]: !original }))
                          }
                        >
                          {original ? "显示中文" : "查看原文"}
                        </button>
                        {m.lang && <span className="tag">{m.lang} → 中</span>}
                      </div>
                    </>
                  ) : (
                    <div className="bubble-text">
                      {m.translate && !m.translated ? "翻译中…" : m.text}
                    </div>
                  )}
                </div>
                {m.date && <div className="msg-time">{m.date}</div>}
              </div>
            );
          })
        )}
        <div ref={bottomRef} />
      </div>

      <div className="composer">
        <textarea
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          placeholder="输入回复…"
          rows={1}
        />
        <button
          className="send-btn"
          onClick={submit}
          disabled={sending || !reply.trim()}
        >
          {sending ? "…" : "发送"}
        </button>
      </div>
    </div>
  );
}

function Attachment({
  att,
  text,
}: {
  att: { filename: string; url: string; preview: string; is_image: boolean };
  text: string;
}) {
  return (
    <div className="attachment">
      {text && text !== att.filename && <div className="bubble-text">{text}</div>}
      {att.is_image && att.preview ? (
        <a href={att.url || att.preview} target="_blank" rel="noreferrer">
          <img className="att-img" src={att.preview} alt={att.filename} loading="lazy" />
        </a>
      ) : att.url ? (
        <a className="att-link" href={att.url} target="_blank" rel="noreferrer">
          {att.filename || "附件"}
        </a>
      ) : (
        <span className="att-name">{att.filename || "附件"}</span>
      )}
    </div>
  );
}
