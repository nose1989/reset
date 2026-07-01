import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchConversations } from "../api";
import type { Conversation } from "../types";

export default function ConversationList() {
  const navigate = useNavigate();
  const [items, setItems] = useState<Conversation[]>([]);
  const [unreadTotal, setUnreadTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const data = await fetchConversations();
      if (!data.ok) throw new Error(data.error || "加载失败");
      setItems(data.conversations);
      setUnreadTotal(data.unread_total);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const open = (c: Conversation) => {
    const query = new URLSearchParams({
      name: c.name,
      product: c.product,
      email: c.email,
    });
    navigate(`/c/${c.platform}/${c.id}?${query.toString()}`);
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-title">消息</div>
        <div className="topbar-meta">
          {unreadTotal > 0 ? `${unreadTotal} 未读` : `${items.length} 会话`}
        </div>
        <button className="icon-btn" onClick={() => load()} aria-label="刷新">
          ⟳
        </button>
      </header>

      {error && (
        <div className="banner error">
          加载失败：{error}
          <button className="link-btn" onClick={() => load()}>
            重试
          </button>
        </div>
      )}

      {loading ? (
        <div className="empty">加载中…</div>
      ) : items.length === 0 && !error ? (
        <div className="empty">暂无会话</div>
      ) : (
        <ul className="conv-list">
          {items.map((c) => (
            <li
              key={`${c.platform}:${c.id}`}
              className="conv-item"
              onClick={() => open(c)}
            >
              <div className="avatar">{c.initial}</div>
              <div className="conv-main">
                <div className="conv-name">{c.name}</div>
                <div className="conv-preview">{c.preview}</div>
              </div>
              <div className="conv-side">
                <div className="conv-time">{c.time_label}</div>
                {c.unread > 0 && <div className="badge">{c.unread}</div>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
