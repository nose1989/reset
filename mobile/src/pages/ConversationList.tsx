import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchConversations, resolveBackendUrl } from "../api";
import type { Avatar, Conversation } from "../types";

// Kept outside the component so it survives unmount when navigating into a
// conversation and back. Lets us return to the list without re-fetching (no
// flicker/refresh) and restore the exact scroll position we left from.
const listCache: {
  items: Conversation[];
  unreadTotal: number;
  loaded: boolean;
  scrollTop: number;
} = { items: [], unreadTotal: 0, loaded: false, scrollTop: 0 };

// Unread count for a conversation as last known by the cached list. Returns
// null when the list has not been loaded or the conversation is not in it, so
// callers can fall back to fetching. `0` means "no new messages".
export function getCachedUnread(platform: string, id: number): number | null {
  if (!listCache.loaded) return null;
  const item = listCache.items.find((c) => c.platform === platform && c.id === id);
  return item ? item.unread : null;
}

// Called when a conversation is opened: clear its unread badge in the cached
// list so returning to the list (which reuses the cache, no refetch) no longer
// shows the red dot for the chat we just read. The backend already marks the
// chat read when its messages are fetched.
export function markCachedConversationRead(platform: string, id: number) {
  let removed = 0;
  listCache.items = listCache.items.map((c) => {
    if (c.platform === platform && c.id === id && c.unread > 0) {
      removed = c.unread;
      return { ...c, unread: 0 };
    }
    return c;
  });
  if (removed) listCache.unreadTotal = Math.max(0, listCache.unreadTotal - removed);
}

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
  const listRef = useRef<HTMLUListElement>(null);
  const [items, setItems] = useState<Conversation[]>(listCache.items);
  const [unreadTotal, setUnreadTotal] = useState(listCache.unreadTotal);
  const [loading, setLoading] = useState(!listCache.loaded);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const data = await fetchConversations();
      if (!data.ok) throw new Error(data.error || "加载失败");
      setItems(data.conversations);
      setUnreadTotal(data.unread_total);
      listCache.items = data.conversations;
      listCache.unreadTotal = data.unread_total;
      listCache.loaded = true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Only fetch on first visit. On return from a conversation we reuse the cache
  // so the list is not refreshed.
  useEffect(() => {
    if (!listCache.loaded) load();
  }, [load]);

  // Restore the scroll position we had when leaving for a conversation.
  useLayoutEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listCache.scrollTop;
  }, []);

  const rememberScroll = () => {
    if (listRef.current) listCache.scrollTop = listRef.current.scrollTop;
  };

  const open = (c: Conversation) => {
    rememberScroll();
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
        <ul className="conv-list" ref={listRef} onScroll={rememberScroll}>
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
