import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  fetchMessages,
  sendReply,
  setDelivered,
  translateMessages,
  verifyCode,
} from "../api";
import { getCachedUnread, markCachedConversationRead } from "./ConversationList";
import { decodeConvId } from "../convId";
import { downscaleImage } from "../image";
import type {
  DeliveryStatus,
  Message,
  OrderOption,
  VerifyStatus,
} from "../types";

// Per-conversation cache of the loaded (and translated) messages. Survives
// navigating back to the list and reopening. Reused instead of re-fetching when
// the list reports the conversation has no new messages (unread === 0).
type CachedConversation = {
  messages: Message[];
  name: string;
  product: string;
  targetLang: string;
  options: OrderOption[];
  delivery: DeliveryStatus;
};

const NO_DELIVERY: DeliveryStatus = { supported: false };
const messageCache: Record<string, CachedConversation> = {};

export default function Conversation() {
  const { cid = "" } = useParams();
  const { platform, id } = decodeConvId(cid);
  const convId = Number(id);
  const hints = (useLocation().state || {}) as {
    name?: string;
    product?: string;
    email?: string;
  };
  const navigate = useNavigate();

  const cacheKey = `${platform}:${convId}`;
  // Reuse cached messages only when the list says there are no new messages.
  const cached = messageCache[cacheKey];
  const canUseCache = cached != null && getCachedUnread(platform, convId) === 0;

  const [messages, setMessages] = useState<Message[]>(
    canUseCache ? cached.messages : [],
  );
  const [name, setName] = useState(
    canUseCache ? cached.name : hints.name || "会员",
  );
  const [product, setProduct] = useState(
    canUseCache ? cached.product : hints.product || "",
  );
  const [targetLang, setTargetLang] = useState(
    canUseCache ? cached.targetLang : "en",
  );
  const [options, setOptions] = useState<OrderOption[]>(
    canUseCache ? cached.options : [],
  );
  const [delivery, setDelivery] = useState<DeliveryStatus>(
    canUseCache ? cached.delivery : NO_DELIVERY,
  );
  const [delivering, setDelivering] = useState(false);
  const [showOptions, setShowOptions] = useState(true);
  const [loading, setLoading] = useState(!canUseCache);
  const [error, setError] = useState("");
  const [reply, setReply] = useState("");
  const [sending, setSending] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [previews, setPreviews] = useState<string[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const [verify, setVerify] = useState<VerifyStatus>({ needs: false });
  const [code, setCode] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [verifyError, setVerifyError] = useState("");
  const [toast, setToast] = useState("");
  const toastTimer = useRef<number | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const msgsRef = useRef<HTMLDivElement>(null);
  const [showScrollDown, setShowScrollDown] = useState(false);

  const updateScrollDown = useCallback(() => {
    const el = msgsRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollDown(distanceFromBottom > 200);
  }, []);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, []);

  const showToast = useCallback((text: string) => {
    setToast(text);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 2000);
  }, []);

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
        name: hints.name || undefined,
        product: hints.product || undefined,
        email: hints.email || undefined,
      });
      if (!data.ok) throw new Error(data.error || "加载失败");
      setMessages(data.messages);
      if (data.name) setName(data.name);
      setProduct(data.product);
      setTargetLang(data.target_lang || "en");
      // Order options can come back empty on a transient/cold backend fetch.
      // Don't blank out options we already have in that case.
      if (data.options && data.options.length > 0) setOptions(data.options);
      setDelivery(data.delivery || NO_DELIVERY);
      setVerify(data.verify || { needs: false });
      runTranslations(data.messages);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [platform, convId, hints, runTranslations]);

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
      messageCache[cacheKey] = {
        messages,
        name,
        product,
        targetLang,
        options,
        delivery,
      };
    }
  }, [cacheKey, messages, name, product, targetLang, options, delivery, loading]);

  // Opening a chat marks it read on the backend, so drop its unread badge from
  // the cached list too — returning to the list won't show a stale red dot.
  useEffect(() => {
    markCachedConversationRead(platform, convId);
  }, [platform, convId]);

  // Object URLs for the attachment thumbnails; revoked when the selection changes.
  useEffect(() => {
    const urls = files.map((f) => URL.createObjectURL(f));
    setPreviews(urls);
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [files]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
    updateScrollDown();
  }, [messages, updateScrollDown]);

  useEffect(() => {
    return () => {
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
    };
  }, []);

  const onPickFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files || []).filter((f) =>
      f.type.startsWith("image/"),
    );
    e.target.value = "";
    if (!picked.length) return;
    const processed = await Promise.all(picked.map(downscaleImage));
    setFiles((prev) => [...prev, ...processed]);
  };

  const removeFile = (idx: number) =>
    setFiles((prev) => prev.filter((_, i) => i !== idx));

  const submit = async () => {
    const text = reply.trim();
    if ((!text && files.length === 0) || sending) return;
    setSending(true);
    setError("");
    try {
      const data = await sendReply({
        platform,
        id: convId,
        message: text,
        target_lang: targetLang,
        files: files.length > 0 ? files : undefined,
      });
      if (!data.ok) throw new Error(data.error || "发送失败");
      setReply("");
      setFiles([]);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  const markDelivered = async () => {
    if (delivering || delivery.delivered) return;
    setDelivering(true);
    setError("");
    try {
      const data = await setDelivered({ platform, id: convId, delivered: true });
      if (!data.ok) throw new Error(data.error || "操作失败");
      setDelivery({
        supported: true,
        status: data.status,
        delivered: !!data.delivered,
      });
      showToast("已标记发货");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDelivering(false);
    }
  };

  const submitCode = async () => {
    const value = code.trim();
    if (!value || verifying) return;
    setVerifying(true);
    setVerifyError("");
    try {
      const data = await verifyCode(value);
      if (!data.ok || !data.item) throw new Error(data.error || "验证失败");
      showToast("验证成功");
    } catch (e) {
      setVerifyError(e instanceof Error ? e.message : String(e));
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div className="app">
      {toast && <div className="toast">{toast}</div>}
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

      {options.length > 0 && (
        <div className="order-options">
          <button
            className="order-options-head"
            onClick={() => setShowOptions((v) => !v)}
            aria-expanded={showOptions}
          >
            <span className="order-options-title">购买选项（{options.length}）</span>
            <span className="order-options-toggle">{showOptions ? "收起" : "展开"}</span>
          </button>
          {showOptions && (
            <div className="order-options-body">
              {options.map((o, i) => (
                <div className="order-option" key={`${o.name}-${i}`}>
                  <span className="order-option-name">{o.name}</span>
                  <span className="order-option-value">{o.value}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {delivery.supported && (
        <div className={`delivery-bar ${delivery.delivered ? "is-delivered" : ""}`}>
          <div className="delivery-info">
            <span className="delivery-title">发货状态</span>
            <span className="delivery-state">
              {delivery.delivered ? "已发货" : "待发货"}
            </span>
          </div>
          {!delivery.delivered && (
            <button
              className="delivery-btn"
              onClick={markDelivered}
              disabled={delivering}
            >
              {delivering ? "处理中…" : "标记已发货"}
            </button>
          )}
        </div>
      )}

      {verify.needs && (
        <div className="verify-box">
          <div className="verify-head">
            <span className="verify-title">手动发货 · 需买家 16 位验证码</span>
            {verify.state && <span className="verify-state">{verify.state}</span>}
          </div>
          {!verify.verified && (
            <>
              <div className="verify-form">
                <input
                  className="verify-input"
                  value={code}
                  onChange={(e) =>
                    setCode(
                      e.target.value.replace(/[^A-Za-z0-9]/g, "").slice(0, 16),
                    )
                  }
                  placeholder="粘贴买家提供的 16 位码"
                  maxLength={16}
                  autoCapitalize="characters"
                  autoCorrect="off"
                  spellCheck={false}
                />
                <button
                  className="verify-btn"
                  onClick={submitCode}
                  disabled={verifying || code.trim().length !== 16}
                >
                  {verifying ? "验证中…" : "验证"}
                </button>
              </div>
              {verifyError && <div className="verify-error">{verifyError}</div>}
            </>
          )}
        </div>
      )}

      <div className="msgs" ref={msgsRef} onScroll={updateScrollDown}>
        {loading ? (
          <div className="empty">加载中…</div>
        ) : messages.length === 0 ? (
          <div className="empty">暂无消息</div>
        ) : (
          messages.map((m) => {
            const isOut = m.direction === "out";
            const hasTranslation = m.translate && !!m.translated;
            return (
              <div key={m.id} className={`row ${isOut ? "out" : "in"}`}>
                <div className="bubble">
                  {m.attachment ? (
                    <Attachment att={m.attachment} text={m.text} />
                  ) : hasTranslation ? (
                    <>
                      <div className="bubble-text">{m.translated}</div>
                      {m.lang && (
                        <div className="bubble-actions">
                          <span className="tag">{m.lang} → 中</span>
                        </div>
                      )}
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

      {showScrollDown && (
        <button
          className="scroll-down-btn"
          onClick={scrollToBottom}
          aria-label="滚动到底部"
        >
          ↓
        </button>
      )}

      <div className="composer">
        {files.length > 0 && (
          <div className="attach-previews">
            {files.map((f, i) => (
              <div className="attach-thumb" key={`${f.name}-${f.size}-${i}`}>
                {previews[i] && <img src={previews[i]} alt={f.name} />}
                <button
                  className="attach-remove"
                  onClick={() => removeFile(i)}
                  aria-label="移除图片"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="composer-row">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={onPickFiles}
          />
          <button
            className="attach-btn"
            onClick={() => fileRef.current?.click()}
            disabled={sending}
            aria-label="添加图片"
          >
            📷
          </button>
          <textarea
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            placeholder="输入回复…"
            rows={1}
          />
          <button
            className="send-btn"
            onClick={submit}
            disabled={sending || (!reply.trim() && files.length === 0)}
          >
            {sending ? "…" : "发送"}
          </button>
        </div>
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
