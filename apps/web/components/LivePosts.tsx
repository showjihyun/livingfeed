"use client";

/**
 * 라이브 피드 섹션 — 실 백엔드(feed.post.published)에서 흐르는 포스트.
 * 데모 시나리오 카드(민지/철수)와 공존한다: 백엔드가 없으면 이 섹션은
 * "오프라인" 칩만 남기고 데모가 화면을 채운다.
 */

import type { LivePost, LiveStatus } from "@/lib/live-feed";
import { relativeTime } from "@/lib/live-feed";

const STATUS_CHIP: Record<LiveStatus, { label: string; bg: string; color: string; dot: string }> = {
  live: { label: "LIVE — 세계와 연결됨", bg: "#E3F5EC", color: "#3E8A66", dot: "#5FBF95" },
  connecting: { label: "연결 중...", bg: "#FFF6DE", color: "#A87F24", dot: "#E0B84F" },
  offline: { label: "오프라인 — 데모 데이터", bg: "#F2F6FC", color: "#8C97AF", dot: "#B7C2D8" },
};

const AVATAR_COLORS = ["#AFC8F5", "#F2B8CF", "#BFE3CF", "#E8D5A8", "#CBBDE8", "#A8D8E8"];

function avatarColor(authorId: string): string {
  let hash = 0;
  for (const ch of authorId) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

/** "a_aria_kim" → "AK", "김아리, ..."식 제목의 표시 이니셜 */
function initials(post: LivePost): string {
  const name = post.title.split(",")[0]?.trim() ?? post.authorId;
  return name.slice(0, 1).toUpperCase();
}

function LivePostCard({
  post,
  liked,
  onLike,
}: {
  post: LivePost;
  liked: boolean;
  onLike: (post: LivePost) => void;
}) {
  return (
    <div
      style={{
        border: "1.5px solid #E2EAF6",
        borderRadius: 20,
        padding: "18px 24px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        animation: "lf-pop 0.35s ease-out",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div
          style={{
            width: 40,
            height: 40,
            borderRadius: "50%",
            background: avatarColor(post.authorId),
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 16,
            fontWeight: 800,
            color: "#3A4256",
            flexShrink: 0,
          }}
        >
          {initials(post)}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <div style={{ fontSize: 15, fontWeight: 800 }}>{post.title}</div>
          <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
            {relativeTime(post.occurredAt)} · 드라마 {Math.round(post.dramaScore * 100)}
            {post.tags.map((tag) => ` · #${tag}`).join("")}
          </div>
        </div>
      </div>
      <div style={{ fontSize: 15, lineHeight: 1.65, fontWeight: 500 }}>{post.body}</div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div
          onClick={() => onLike(post)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "6px 13px",
            background: liked ? "#C76F93" : "#FDEDF3",
            color: liked ? "#ffffff" : "#C76F93",
            borderRadius: 9999,
            fontSize: 13,
            fontWeight: 800,
            cursor: "pointer",
            userSelect: "none",
          }}
        >
          ♥ {liked ? "전달됨" : "좋아요"}
        </div>
        {liked && (
          <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
            세계가 당신의 반응을 알아차렸어요
          </div>
        )}
      </div>
    </div>
  );
}

export function LivePosts({
  posts,
  status,
  liked,
  onLike,
}: {
  posts: LivePost[];
  status: LiveStatus;
  liked: ReadonlySet<string>;
  onLike: (post: LivePost) => void;
}) {
  const chip = STATUS_CHIP[status];
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ fontSize: 14, fontWeight: 800, color: "#3A4256" }}>지금 세계에서</div>
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "3px 10px",
            background: chip.bg,
            color: chip.color,
            borderRadius: 9999,
            fontSize: 11,
            fontWeight: 800,
          }}
        >
          <div
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: chip.dot,
              animation: status === "live" ? "lf-blink 1.6s infinite" : undefined,
            }}
          />
          {chip.label}
        </div>
      </div>
      {posts.map((post) => (
        <LivePostCard key={post.id} post={post} liked={liked.has(post.id)} onLike={onLike} />
      ))}
    </>
  );
}
