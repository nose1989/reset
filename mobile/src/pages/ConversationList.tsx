import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchConversations, resolveBackendUrl } from "../api";
import type { Avatar, Conversation } from "../types";

function ConvAvatar({ avatar, initial }: { avatar?: Avatar; initial: string }) {
  const [imgOk, setImgOk] = useState(true);
  if (avatar?.kind === "brand" && avatar.logo && imgOk) {
    return (
      <div
        className="avatar brand-avatar"
        style={{ background: avatar.background || "#111827" }}
      >
        <img
          className="brand-logo"
          src={resolveBackendUrl(avatar.logo)}
          alt={avatar.name || ""}
          loading="lazy"
          onError={() => setImgOk(false)}
        />
      </div>
    );
  }
  if (avatar?.kind === "brand" || avatar?.kind === "generic") {
    return (
      <div className="avatar generic-avatar">
        <span className="avatar-mark">{avatar.mark || initial}</span>
        {avatar.label && <span className="avatar-label">{avatar.label}</span>}
      </div>
    );
  }
  return <div className="avatar">{initial}</div>;
}

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
              <div className="avatar-wrap">
                <ConvAvatar avatar={c.avatar} initial={c.initial} />
                {c.unread > 0 && (
                  <span className="unread-dot">
                    {c.unread > 99 ? "99+" : c.unread}
                  </span>
                )}
              </div>
              <div className="conv-main">
                <div className="conv-name">{c.name}</div>
                <div className="conv-preview">{c.preview}</div>
              </div>
              <div className="conv-side">
                <div className="conv-time">{c.time_label}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
