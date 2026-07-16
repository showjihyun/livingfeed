"use client";

/**
 * 라이브 피드 섹션 — 실 백엔드(feed.post.published)에서 흐르는 포스트.
 * 좋아요·댓글이 실제 포스트(post_id)에 붙는다: 댓글은 작성 액터에게 닿고
 * (player.comment.posted), 응답은 actor.message.sent로 돌아와 인라인 렌더된다.
 * 백엔드 미가용이면 "오프라인" 칩만 남는다 (하드코딩 데모 카드 없음).
 */

import { useEffect, useState, type KeyboardEvent } from "react";

import { ICON } from "@/lib/data";
import type { LivePost, LiveStatus } from "@/lib/live-feed";
import { relativeTime } from "@/lib/live-feed";
import type { FeedComment } from "@/lib/types";

import { Icon } from "./Icon";

const STATUS_CHIP: Record<LiveStatus, { label: string; bg: string; color: string; dot: string }> = {
  live: { label: "LIVE — 세계와 연결됨", bg: "#E3F5EC", color: "#3E8A66", dot: "#5FBF95" },
  connecting: { label: "연결 중...", bg: "#FFF6DE", color: "#A87F24", dot: "#E0B84F" },
  offline: { label: "오프라인 — 세계와 끊김", bg: "#F2F6FC", color: "#8C97AF", dot: "#B7C2D8" },
};

const AVATAR_COLORS = ["#AFC8F5", "#F2B8CF", "#BFE3CF", "#E8D5A8", "#CBBDE8", "#A8D8E8"];

function avatarColor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

/** 세계 사건(작성 액터 없음) 포스트는 댓글 대상이 아니다 — 실제 액터 포스트만 */
function isCommentable(post: LivePost): boolean {
  return post.authorId.startsWith("a_");
}

function Avatar({ seed, label, size }: { seed: string; label: string; size: number }) {
  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        background: avatarColor(seed),
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: size * 0.4,
        fontWeight: 800,
        color: "#3A4256",
        flexShrink: 0,
      }}
    >
      {label.slice(0, 1).toUpperCase()}
    </div>
  );
}

/** '쓰고 있어요'를 분 단위로 주장하지 않기 위한 전환 시점 — 세계는 tick 페이스로 답한다 */
const TYPING_PATIENT_AFTER_MS = 45_000;

function TypingDots() {
  // 답장은 그 사람의 tick 차례에 온다 (LOD·세계 시간) — 잠시 후엔 타이핑이 아니라
  // 기다림의 문장으로 바꾼다. 답이 오면 이 표시 자체가 댓글로 교체된다.
  const [patient, setPatient] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setPatient(true), TYPING_PATIENT_AFTER_MS);
    return () => clearTimeout(timer);
  }, []);
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", paddingLeft: 4 }}>
      <div
        style={{
          display: "flex",
          gap: 4,
          alignItems: "center",
          padding: "8px 12px",
          background: "#F2F6FC",
          borderRadius: 9999,
        }}
      >
        {[0, 0.2, 0.4].map((delay, i) => (
          <div
            key={i}
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: ["#6B7691", "#9AA6BF", "#CBD5E8"][i],
              animation: patient ? undefined : `lf-blink 1s ${delay}s infinite`,
            }}
          />
        ))}
      </div>
      <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
        {patient
          ? "전해졌어요 — 답장은 그 사람의 시간에 맞춰 와요"
          : "답을 쓰고 있어요..."}
      </div>
    </div>
  );
}

function LivePostCard({
  post,
  liked,
  onLike,
  comments,
  typing,
  onComment,
  authorLabel,
}: {
  post: LivePost;
  liked: boolean;
  onLike: (post: LivePost) => void;
  comments: FeedComment[];
  typing: boolean;
  onComment: (post: LivePost, text: string) => void;
  authorLabel: string;
}) {
  const [draft, setDraft] = useState("");
  const commentable = isCommentable(post);
  // 인생의 장이 넘어간 순간 (ADR-014/plan-08) — 일반 포스트와 결이 다른 서사 마디
  const isArcTransition = post.tags.includes("arc_transition");

  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    onComment(post, text);
    setDraft("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") submit();
  };

  return (
    <div
      style={{
        border: isArcTransition ? "1.5px solid #D8CCF2" : "1.5px solid #E2EAF6",
        background: isArcTransition ? "#FBFAFE" : undefined,
        borderRadius: 20,
        padding: "18px 24px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        animation: "lf-pop 0.35s ease-out",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <Avatar seed={post.authorId} label={authorLabel} size={40} />
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ fontSize: 15, fontWeight: 800 }}>{post.title}</div>
            {isArcTransition && (
              <div
                style={{
                  padding: "2px 10px",
                  background: "#EFE9FB",
                  color: "#7B62C9",
                  borderRadius: 9999,
                  fontSize: 11,
                  fontWeight: 800,
                }}
              >
                인생의 장
              </div>
            )}
          </div>
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
        {commentable && comments.length > 0 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 13px",
              background: "#F2F6FC",
              color: "#6B7691",
              borderRadius: 9999,
              fontSize: 13,
              fontWeight: 800,
            }}
          >
            <Icon d={ICON.messageCircle} size={14} /> {comments.length}
          </div>
        )}
      </div>

      {comments.map((cm, i) => (
        <div
          key={i}
          style={{
            display: "flex",
            gap: 10,
            padding: "12px 14px",
            background: cm.bg,
            borderRadius: 14,
          }}
        >
          <Avatar seed={cm.author} label={cm.author} size={28} />
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <div style={{ fontSize: 13, fontWeight: 800 }}>{cm.author}</div>
            <div style={{ fontSize: 14, lineHeight: 1.55, fontWeight: 500 }}>{cm.text}</div>
          </div>
        </div>
      ))}

      {typing && <TypingDots />}

      {commentable && (
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKey}
            placeholder={`${authorLabel}에게 댓글 남기기... (개입은 흔적을 남겨요)`}
            style={{
              flex: 1,
              background: "#F2F6FC",
              border: "none",
              outline: "none",
              borderRadius: 9999,
              padding: "11px 18px",
              fontSize: 14,
              color: "#3A4256",
              fontWeight: 500,
            }}
          />
          <div
            onClick={submit}
            style={{
              padding: "10px 18px",
              background: "#6D8DD6",
              color: "#fff",
              borderRadius: 9999,
              fontSize: 14,
              fontWeight: 800,
              cursor: "pointer",
            }}
          >
            전송
          </div>
        </div>
      )}
    </div>
  );
}

export function LivePosts({
  posts,
  status,
  liked,
  onLike,
  commentsByPost,
  typingPosts,
  onComment,
  authorName,
}: {
  posts: LivePost[];
  status: LiveStatus;
  liked: ReadonlySet<string>;
  onLike: (post: LivePost) => void;
  commentsByPost: Record<string, FeedComment[]>;
  typingPosts: ReadonlySet<string>;
  onComment: (post: LivePost, text: string) => void;
  authorName: (actorId: string) => string;
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
      {posts.length === 0 && status !== "live" && (
        <div
          style={{
            border: "1.5px dashed #D8E1F0",
            borderRadius: 18,
            padding: "28px 24px",
            textAlign: "center",
            color: "#8C97AF",
            fontSize: 14,
            fontWeight: 600,
          }}
        >
          아직 흐르는 이야기가 없어요 — 세계가 깨어나면 여기에 채워집니다.
        </div>
      )}
      {posts.map((post) => (
        <LivePostCard
          key={post.id}
          post={post}
          liked={liked.has(post.id)}
          onLike={onLike}
          comments={commentsByPost[post.id] ?? []}
          typing={typingPosts.has(post.id)}
          onComment={onComment}
          authorLabel={authorName(post.authorId)}
        />
      ))}
    </>
  );
}
