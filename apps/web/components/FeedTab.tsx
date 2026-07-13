import type { LivePost, LiveStatus } from "@/lib/live-feed";
import type { FeedComment } from "@/lib/types";

import { Icon } from "./Icon";
import { ICON } from "@/lib/data";
import { LivePosts } from "./LivePosts";

interface FeedTabProps {
  livePosts: LivePost[];
  liveStatus: LiveStatus;
  likedLive: ReadonlySet<string>;
  onLikeLive: (post: LivePost) => void;
  /** 포스트별 댓글·타이핑·작성 핸들러 — 댓글은 실제 라이브 포스트에 붙는다 */
  commentsByPost: Record<string, FeedComment[]>;
  typingPosts: ReadonlySet<string>;
  onComment: (post: LivePost, text: string) => void;
  /** actor_id → 표시 이름 (라이브 identity, 하드코딩 금지) */
  authorName: (actorId: string) => string;
  showCoach: boolean;
  onDismissCoach: () => void;
}

export function FeedTab({
  livePosts,
  liveStatus,
  likedLive,
  onLikeLive,
  commentsByPost,
  typingPosts,
  onComment,
  authorName,
  showCoach,
  onDismissCoach,
}: FeedTabProps) {
  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "18px 32px",
          borderBottom: "1.5px solid #EEF3FB",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ fontSize: 20, fontWeight: 800 }}>World Feed</div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              background: "#E3F5EC",
              color: "#3E8A66",
              borderRadius: 9999,
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            <div
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: "#5FBF95",
                animation: "lf-blink 1.6s infinite",
              }}
            />
            실시간
          </div>
        </div>
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
          당신의 개입이 세계에 흔적을 남깁니다
        </div>
      </div>

      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "24px 32px",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        {/* Aha 코치 배너 — 첫 개입을 유도 (특정 인물에 묶이지 않는 안내) */}
        {showCoach && (
          <div
            style={{
              background: "#EDF3FD",
              border: "1.5px solid #CFE0F8",
              borderRadius: 18,
              padding: "14px 20px",
              display: "flex",
              alignItems: "center",
              gap: 14,
              animation: "lf-pop 0.35s ease-out",
            }}
          >
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 12,
                background: "#fff",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}
            >
              <Icon d={ICON.sparkles} size={17} color="#5F7EC9" />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 800, color: "#3A4256" }}>
                지금 세계가 흐르고 있어요
              </div>
              <div style={{ fontSize: 13, color: "#5F7EC9", fontWeight: 600 }}>
                마음이 가는 이야기에 좋아요나 댓글로 조용히 시작해보세요 — 그것만으로도 세계는
                당신을 알아차립니다.
              </div>
            </div>
            <div
              onClick={onDismissCoach}
              style={{
                color: "#8C97AF",
                cursor: "pointer",
                fontSize: 16,
                fontWeight: 800,
                lineHeight: 1,
                padding: 4,
              }}
            >
              ×
            </div>
          </div>
        )}

        {/* 라이브 피드 — 실 백엔드(feed.post.published). 좋아요·댓글이 실제 포스트에 붙는다 */}
        <LivePosts
          posts={livePosts}
          status={liveStatus}
          liked={likedLive}
          onLike={onLikeLive}
          commentsByPost={commentsByPost}
          typingPosts={typingPosts}
          onComment={onComment}
          authorName={authorName}
        />
      </div>
    </>
  );
}
