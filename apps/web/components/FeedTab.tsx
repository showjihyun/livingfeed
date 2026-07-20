import { memo, useState } from "react";

import type { DerivedStories } from "@/lib/comments";
import type { LivePost, LiveStatus } from "@/lib/live-feed";
import type { Range } from "@/lib/range";
import { useWorldSearch } from "@/lib/search";
import type { WorldSearchResult } from "@/lib/search";
import { useStory } from "@/lib/story";
import type { FeedComment } from "@/lib/types";

import { Icon } from "./Icon";
import { ICON } from "@/lib/data";
import { COLOR } from "@/lib/tokens";
import { LivePosts, StoryThread } from "./LivePosts";
import { Pressable } from "./Pressable";
import { RangeChips } from "./RangeChips";

interface FeedTabProps {
  livePosts: LivePost[];
  liveStatus: LiveStatus;
  /** 조회 범위(세계 시간) — 전체/오늘/이번 주/이번 달 (lib/range) */
  range: Range;
  onRangeChange: (range: Range) => void;
  likedLive: ReadonlySet<string>;
  onLikeLive: (post: LivePost) => void;
  /** 포스트별 댓글·타이핑·작성 핸들러 — 댓글은 실제 라이브 포스트에 붙는다 */
  commentsByPost: Record<string, FeedComment[]>;
  /** 포스트별 "이 대화가 낳은 이야기" — 개입 사슬을 승계한 후속 포스트 요약 */
  derivedByPost: Record<string, DerivedStories>;
  typingPosts: ReadonlySet<string>;
  onComment: (post: LivePost, text: string) => void;
  /** actor_id → 표시 이름 (라이브 identity, 하드코딩 금지) */
  authorName: (actorId: string) => string;
  /** 검색어에 이름이 닿는 액터 id들 — 로스터 역해석 (인덱스는 이름을 모른다) */
  matchActorIds: (q: string) => string[];
  showCoach: boolean;
  onDismissCoach: () => void;
}

/** 검색 결과 카드 하나 — "이야기 따라가기"로 기존 사슬 화면과 합류한다 */
function SearchResultCard({
  result,
  authorName,
}: {
  result: WorldSearchResult;
  authorName: (actorId: string) => string;
}) {
  const [open, setOpen] = useState(false);
  const story = useStory(open ? (result.correlationId ?? undefined) : undefined);
  const author = result.authorId ? authorName(result.authorId) : "세계";
  return (
    <div
      style={{
        background: COLOR.white,
        border: "1.5px solid #EEF3FB",
        borderRadius: 18,
        padding: "16px 20px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      {result.title && <div style={{ fontSize: 15, fontWeight: 800 }}>{result.title}</div>}
      {result.body && (
        <div
          style={{
            fontSize: 13,
            color: "#5A6378",
            fontWeight: 600,
            lineHeight: 1.6,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {result.body}
        </div>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div
          style={{
            padding: "2px 10px",
            background: COLOR.primarySoft,
            color: COLOR.primaryDeep,
            borderRadius: 9999,
            fontSize: 11,
            fontWeight: 800,
          }}
        >
          {author}
        </div>
        {result.correlationId && (
          <Pressable
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              fontSize: 12,
              fontWeight: 800,
              color: "#8B7BD8",
            }}
          >
            <Icon d={ICON.gitBranch} size={13} /> 이야기 따라가기
          </Pressable>
        )}
      </div>
      {open && story && <StoryThread story={story} />}
    </div>
  );
}

// 가장 무거운 탭 — 무관한 루트 상태 변화(토스트·타 탭 데이터)에 리렌더되지 않게 memo.
// props는 전부 안정적(useCallback/원시)이라 실제로 memo가 먹는다.
export const FeedTab = memo(FeedTabInner);

function FeedTabInner({
  livePosts,
  liveStatus,
  range,
  onRangeChange,
  likedLive,
  onLikeLive,
  commentsByPost,
  derivedByPost,
  typingPosts,
  onComment,
  authorName,
  matchActorIds,
  showCoach,
  onDismissCoach,
}: FeedTabProps) {
  // 검색 — 라이브는 흐르는 창일 뿐, 검색은 세계의 전 역사를 뒤진다 (OS 인덱스)
  const [query, setQuery] = useState("");
  const search = useWorldSearch(query, matchActorIds);
  const searching = query.trim().length > 0;

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
              background: COLOR.successSoft,
              color: COLOR.success,
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
                background: COLOR.successBright,
                animation: "lf-blink 1.6s infinite",
              }}
            />
            실시간
          </div>
        </div>
        <div style={{ fontSize: 13, color: COLOR.faint, fontWeight: 600 }}>
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
        {/* 검색 — 이름·제목·본문으로 세계의 전 역사를 (스튜디오와 같은 결) */}
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="세계의 이야기 검색 — 이름·제목·본문"
          style={{
            border: "1.5px solid #E2EAF6",
            borderRadius: 12,
            padding: "10px 14px",
            fontSize: 14,
            fontWeight: 600,
            color: COLOR.ink,
            outline: "none",
            background: "#FDFDFE",
            maxWidth: 420,
            fontFamily: "inherit",
          }}
        />

        {searching && (
          <>
            {search.status === "offline" ? (
              <div style={{ fontSize: 13, color: COLOR.faint, fontWeight: 600 }}>
                검색이 세계에 닿지 않아요 — 연결되면 다시 시도해주세요.
              </div>
            ) : search.status === "ready" && search.results.length === 0 ? (
              <div
                style={{
                  border: "1.5px dashed #D8DEEA",
                  borderRadius: 20,
                  padding: "36px 24px",
                  textAlign: "center",
                  color: COLOR.faint,
                  fontSize: 14,
                  fontWeight: 600,
                  lineHeight: 1.7,
                  background: COLOR.white,
                }}
              >
                이 검색어에 닿는 이야기가 아직 없어요.
              </div>
            ) : search.status === "ready" ? (
              <>
                <div style={{ fontSize: 12, fontWeight: 700, color: COLOR.faint }}>
                  세계의 역사에서 {search.results.length}건이 닿았어요
                </div>
                {search.results.map((result) => (
                  <SearchResultCard
                    key={result.eventId}
                    result={result}
                    authorName={authorName}
                  />
                ))}
              </>
            ) : (
              <div style={{ fontSize: 13, color: COLOR.faint, fontWeight: 600 }}>
                세계의 역사를 뒤지는 중…
              </div>
            )}
          </>
        )}

        {/* 조회 범위 — 세계 시간의 창 (목록 상단, 모든 탭 공통 자리) */}
        {!searching && <RangeChips value={range} onChange={onRangeChange} />}

        {/* Aha 코치 배너 — 첫 개입을 유도 (특정 인물에 묶이지 않는 안내) */}
        {!searching && showCoach && (
          <div
            style={{
              background: COLOR.primarySoft,
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
                background: COLOR.white,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}
            >
              <Icon d={ICON.sparkles} size={17} color={COLOR.primaryDeep} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 800, color: COLOR.ink }}>
                지금 세계가 흐르고 있어요
              </div>
              <div style={{ fontSize: 13, color: COLOR.primaryDeep, fontWeight: 600 }}>
                마음이 가는 이야기에 좋아요나 댓글로 조용히 시작해보세요 — 그것만으로도 세계는
                당신을 알아차립니다.
              </div>
            </div>
            <Pressable
              onClick={onDismissCoach}
              aria-label="코치 닫기"
              style={{
                color: COLOR.faint,
                fontSize: 16,
                fontWeight: 800,
                lineHeight: 1,
                padding: 4,
              }}
            >
              ×
            </Pressable>
          </div>
        )}

        {/* 라이브 피드 — 실 백엔드(feed.post.published). 좋아요·댓글이 실제 포스트에 붙는다.
            검색 중에는 결과가 자리를 차지한다 — 검색어를 비우면 라이브로 돌아온다 */}
        {!searching && (
          <LivePosts
            posts={livePosts}
            status={liveStatus}
            emptyNotice={
              range !== "all"
                ? "이 기간엔 세계가 조용했어요 — 범위를 넓히면 지난 이야기가 보여요."
                : undefined
            }
            liked={likedLive}
            onLike={onLikeLive}
            commentsByPost={commentsByPost}
            derivedByPost={derivedByPost}
            typingPosts={typingPosts}
            onComment={onComment}
            authorName={authorName}
          />
        )}
      </div>
    </>
  );
}
