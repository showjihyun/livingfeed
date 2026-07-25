"use client";

/**
 * 라이브 피드 섹션 — 실 백엔드(feed.post.published)에서 흐르는 포스트.
 * 좋아요·댓글이 실제 포스트(post_id)에 붙는다: 댓글은 작성 액터에게 닿고
 * (player.comment.posted), 응답은 actor.message.sent로 돌아와 인라인 렌더된다.
 * 백엔드 미가용이면 "오프라인" 칩만 남는다 (하드코딩 데모 카드 없음).
 */

import { useEffect, useState, type KeyboardEvent } from "react";

import type { DerivedStories } from "@/lib/comments";
import { PLAYER_ID } from "@/lib/config";
import { ICON } from "@/lib/data";
import { useMessages, type Locale } from "@/lib/i18n";
import type { LivePost, LiveStatus } from "@/lib/live-feed";
import { relativeTime } from "@/lib/live-feed";
import type { StoryTimeline } from "@/lib/story";
import { useStory } from "@/lib/story";
import type { FeedComment } from "@/lib/types";
import { COLOR, WEIGHT, RADIUS } from "@/lib/tokens";

import { Icon } from "./Icon";
import { Pressable } from "./Pressable";

const STATUS_CHIP: Record<LiveStatus, { bg: string; color: string; dot: string }> = {
  live: { bg: COLOR.successSoft, color: COLOR.success, dot: COLOR.successBright },
  connecting: { bg: "#FFF6DE", color: "#A87F24", dot: "#E0B84F" },
  offline: { bg: COLOR.surface, color: COLOR.faint, dot: "#B7C2D8" },
};

const en = {
  statusLabel: {
    live: "LIVE — connected to the world",
    connecting: "Connecting...",
    offline: "Offline — cut off from the world",
  } as Record<LiveStatus, string>,
  originBadge: "Origin",
  typingPatient: "Delivered — the reply will come in their own time",
  typing: "Typing a reply…",
  arcBadge: "Life chapter",
  debutBadge: "New face",
  yourCreationBadge: "Your creation",
  startedByYouBadge: "A story you started",
  drama: (score: number) => `Drama ${score}`,
  likeDelivered: "Delivered",
  like: "Like",
  collapseStory: "Collapse the story",
  followStory: "Follow the story",
  latestDerived: (title: string) => `Latest: ${title}`,
  collapseBorn: "Collapse the stories",
  bornCount: (n: number) => `Stories born from this conversation · ${n}`,
  born: "Stories born from this conversation",
  commentPlaceholder: (name: string) =>
    `Leave ${name} a comment… (an intervention leaves its trace)`,
  send: "Send",
  nowInWorld: "In the world right now",
  emptyOffline: "No stories are flowing yet — they will fill in here when the world wakes.",
};
const M: Record<Locale, typeof en> = {
  en,
  ko: {
    statusLabel: {
      live: "LIVE — 세계와 연결됨",
      connecting: "연결 중...",
      offline: "오프라인 — 세계와 끊김",
    },
    originBadge: "시작점",
    typingPatient: "전해졌어요 — 답장은 그 사람의 시간에 맞춰 와요",
    typing: "답을 쓰고 있어요...",
    arcBadge: "인생의 장",
    debutBadge: "새 얼굴",
    yourCreationBadge: "당신이 빚은 인물",
    startedByYouBadge: "당신이 시작한 이야기",
    drama: (score) => `드라마 ${score}`,
    likeDelivered: "전달됨",
    like: "좋아요",
    collapseStory: "이야기 접기",
    followStory: "이야기 따라가기",
    latestDerived: (title) => `가장 최근: ${title}`,
    collapseBorn: "낳은 이야기 접기",
    bornCount: (n) => `이 대화가 낳은 이야기 · ${n}`,
    born: "이 대화가 낳은 이야기",
    commentPlaceholder: (name) => `${name}에게 댓글 남기기... (개입은 흔적을 남겨요)`,
    send: "전송",
    nowInWorld: "지금 세계에서",
    emptyOffline: "아직 흐르는 이야기가 없어요 — 세계가 깨어나면 여기에 채워집니다.",
  },
};

const AVATAR_COLORS = [COLOR.accent, "#F2B8CF", "#BFE3CF", "#E8D5A8", "#CBBDE8", "#A8D8E8"];

function avatarColor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

/** 세계 사건(작성 액터 없음) 포스트는 댓글 대상이 아니다 — 실제 액터 포스트만 */
function isCommentable(post: LivePost): boolean {
  return post.authorId.startsWith("a_");
}

interface ThreadedComment {
  comment: FeedComment;
  /** 원 배열 인덱스 — event id 없는 로컬 댓글(ack 전)의 안정 키 */
  index: number;
  depth: 0 | 1;
}

/**
 * 평평한 시간순 목록 → 깊이 1 스레드. 답장(in_reply_to가 목록의 다른 댓글을
 * 가리킴)을 그 중첩 뿌리 바로 아래로 모은다 — "나 → 액터가 나에게"의 왕복이
 * 시간 순서에 흩어지지 않고 한 묶음으로 읽힌다. 답글의 답글은 최상위 조상
 * 밑에 붙인다(깊이 1 평탄화 — 라우터의 깊이 1 규약과 같은 결). 부모가 목록에
 * 없는 답장(되읽기 창 밖·좋아요 인사처럼 댓글 아닌 원인)은 최상위로 남긴다.
 */
export function threadComments(comments: FeedComment[], postId: string): ThreadedComment[] {
  const byEventId = new Map<string, number>();
  comments.forEach((c, i) => {
    if (c.eventId) byEventId.set(c.eventId, i);
  });
  const rootOf = (start: number): number => {
    let at = start;
    const seen = new Set<number>([at]);
    for (;;) {
      const target = comments[at].inReplyTo;
      if (!target || target === postId) return at; // 최상위(포스트 직속)
      const parent = byEventId.get(target);
      if (parent === undefined || seen.has(parent)) return at; // 부모 미상·순환 방어
      seen.add(parent);
      at = parent;
    }
  };
  const tops: number[] = [];
  const repliesByRoot = new Map<number, number[]>();
  comments.forEach((_, i) => {
    const root = rootOf(i);
    if (root === i) {
      tops.push(i);
      return;
    }
    const siblings = repliesByRoot.get(root);
    if (siblings) siblings.push(i);
    else repliesByRoot.set(root, [i]);
  });
  return tops.flatMap((top): ThreadedComment[] => [
    { comment: comments[top], index: top, depth: 0 },
    ...(repliesByRoot.get(top) ?? []).map(
      (i): ThreadedComment => ({ comment: comments[i], index: i, depth: 1 }),
    ),
  ]);
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
        fontWeight: WEIGHT.heavy,
        color: COLOR.ink,
        flexShrink: 0,
      }}
    >
      {label.slice(0, 1).toUpperCase()}
    </div>
  );
}

/** 이야기 사슬 팔레트 — 인생의 장 배지(파스텔 #EFE9FB/#7B62C9)와 구분되는 짙은 보라 */
const STORY_VIOLET = "#8A63D2";

/**
 * 인과의 실 — 내 개입에서 시작된 사건 연쇄를 시작점부터 시간순으로 보여준다
 * (plan/03 §단계 3→4 "이 이야기의 시작점", Event Sourcing correlation 사슬).
 * 항목의 이름·문장은 전부 BE 실측(es + read.actors) — 하드코딩 서사 없음.
 */
export function StoryThread({ story }: { story: StoryTimeline }) {
  const t = useMessages(M);
  return (
    <div
      style={{
        background: COLOR.surfaceAlt,
        border: "1.5px solid #E9E1F8",
        borderRadius: RADIUS.sm,
        padding: "14px 16px",
        display: "flex",
        flexDirection: "column",
        animation: "lf-pop 0.35s ease-out",
      }}
    >
      {story.items.map((item, i) => {
        const isOrigin = i === 0;
        const isLast = i === story.items.length - 1;
        return (
          <div key={item.eventId} style={{ display: "flex", gap: 10 }}>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                width: 12,
                flexShrink: 0,
              }}
            >
              <div
                style={{
                  width: isOrigin ? 10 : 7,
                  height: isOrigin ? 10 : 7,
                  borderRadius: "50%",
                  background: isOrigin ? STORY_VIOLET : "#CBBDE8",
                  marginTop: 4,
                  flexShrink: 0,
                }}
              />
              {!isLast && <div style={{ width: 2, flex: 1, background: "#E4DBF6", minHeight: 10 }} />}
            </div>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 2,
                paddingBottom: isLast ? 0 : 12,
              }}
            >
              <div
                style={{
                  fontSize: 12,
                  fontWeight: WEIGHT.heavy,
                  color: isOrigin ? STORY_VIOLET : COLOR.muted,
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  flexWrap: "wrap",
                }}
              >
                {item.actor}
                {isOrigin && (
                  <span
                    style={{
                      padding: "1px 8px",
                      background: STORY_VIOLET,
                      color: COLOR.white,
                      borderRadius: RADIUS.pill,
                      fontSize: 10,
                      fontWeight: WEIGHT.heavy,
                    }}
                  >
                    {t.originBadge}
                  </span>
                )}
                <span style={{ color: "#B0A6CC", fontWeight: WEIGHT.semibold }}>
                  {relativeTime(item.occurredAt)}
                </span>
              </div>
              <div style={{ fontSize: 13.5, lineHeight: 1.55, fontWeight: WEIGHT.regular, color: COLOR.ink }}>
                {item.summary}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** '쓰고 있어요'를 분 단위로 주장하지 않기 위한 전환 시점 — 세계는 tick 페이스로 답한다 */
const TYPING_PATIENT_AFTER_MS = 45_000;

function TypingDots() {
  // 답장은 그 사람의 tick 차례에 온다 (LOD·세계 시간) — 잠시 후엔 타이핑이 아니라
  // 기다림의 문장으로 바꾼다. 답이 오면 이 표시 자체가 댓글로 교체된다.
  const t = useMessages(M);
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
          background: COLOR.surface,
          borderRadius: RADIUS.pill,
        }}
      >
        {[0, 0.2, 0.4].map((delay, i) => (
          <div
            key={i}
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: [COLOR.muted, "#9AA6BF", "#CBD5E8"][i],
              animation: patient ? undefined : `lf-blink 1s ${delay}s infinite`,
            }}
          />
        ))}
      </div>
      <div style={{ fontSize: 12, color: COLOR.faint, fontWeight: WEIGHT.semibold }}>
        {patient ? t.typingPatient : t.typing}
      </div>
    </div>
  );
}

function LivePostCard({
  post,
  liked,
  onLike,
  comments,
  derived,
  typing,
  onComment,
  authorLabel,
}: {
  post: LivePost;
  liked: boolean;
  onLike: (post: LivePost) => void;
  comments: FeedComment[];
  /** "이 대화가 낳은 이야기" — 이 스레드의 개입 사슬을 승계한 후속 포스트 요약 */
  derived?: DerivedStories;
  typing: boolean;
  onComment: (post: LivePost, text: string) => void;
  authorLabel: string;
}) {
  const t = useMessages(M);
  const [draft, setDraft] = useState("");
  const commentable = isCommentable(post);
  // 인생의 장이 넘어간 순간 (ADR-014/plan-08) — 일반 포스트와 결이 다른 서사 마디
  const isArcTransition = post.tags.includes("arc_transition");
  // 새 인물의 데뷔 (페르소나 스튜디오의 방생) — 세계가 한 명을 받아들인 순간
  const isDebut = post.tags.includes("debut");
  // 내가 빚은 인물의 데뷔 — 저자성 표식 (plan/03: 실세의 보상은 저자성이다)
  const isYourCreation = isDebut && post.createdBy === PLAYER_ID;

  // 이야기 사슬 실측 (plan/03 §단계 3→4) — 미가용이면 null, 어포던스·배지 숨김
  const story = useStory(post.correlationId || undefined);
  const [storyOpen, setStoryOpen] = useState(false);
  // 사슬이 1건뿐이면 따라갈 이야기가 없다 — 어포던스를 만들지 않는다
  const canFollowStory = story !== null && story.items.length > 1;

  // 이 대화가 낳은 이야기 — 스레드의 개입 사슬(correlation = 포스트 자신)이 낳은
  // 2차 사건이 있을 때만 사슬을 실측한다. 화면은 기존 /story 사슬 표면과 합류한다
  // (plan/03 실세 단계: "이 사건, 내가 만들었다" — 신규 화면 발명 금지).
  const dialogueStory = useStory(derived ? post.id : undefined);
  const [dialogueOpen, setDialogueOpen] = useState(false);
  const canFollowDialogue =
    derived !== undefined && dialogueStory !== null && dialogueStory.items.length > 0;

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
        border: isDebut
          ? "1.5px solid #C7E6D2"
          : isArcTransition
            ? "1.5px solid #D8CCF2"
            : "1.5px solid #E2EAF6",
        background: isDebut ? "#FAFEFB" : isArcTransition ? COLOR.surfaceAlt : undefined,
        borderRadius: RADIUS.xl,
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
            <div style={{ fontSize: 15, fontWeight: WEIGHT.heavy }}>{post.title}</div>
            {isArcTransition && (
              <div
                style={{
                  padding: "2px 10px",
                  background: "#EFE9FB",
                  color: "#7B62C9",
                  borderRadius: RADIUS.pill,
                  fontSize: 11,
                  fontWeight: WEIGHT.heavy,
                }}
              >
                {t.arcBadge}
              </div>
            )}
            {isDebut && (
              <div
                style={{
                  padding: "2px 10px",
                  background: "#E7F6EC",
                  color: "#2F8F55",
                  borderRadius: RADIUS.pill,
                  fontSize: 11,
                  fontWeight: WEIGHT.heavy,
                }}
              >
                {t.debutBadge}
              </div>
            )}
            {isYourCreation && (
              // 창조의 저자성 — "내가 빚은 사람이 저기서 살아간다" (plan/03)
              <div
                style={{
                  padding: "2px 10px",
                  background: STORY_VIOLET,
                  color: COLOR.white,
                  borderRadius: RADIUS.pill,
                  fontSize: 11,
                  fontWeight: WEIGHT.heavy,
                }}
              >
                {t.yourCreationBadge}
              </div>
            )}
            {story?.startedByYou && !isYourCreation && (
              // 저자성 배지 — "이 드라마의 원작자가 나" (plan/03 §단계 3→4)
              <div
                style={{
                  padding: "2px 10px",
                  background: STORY_VIOLET,
                  color: COLOR.white,
                  borderRadius: RADIUS.pill,
                  fontSize: 11,
                  fontWeight: WEIGHT.heavy,
                }}
              >
                {t.startedByYouBadge}
              </div>
            )}
          </div>
          <div style={{ fontSize: 12, color: COLOR.faint, fontWeight: WEIGHT.semibold }}>
            {relativeTime(post.occurredAt)} · {t.drama(Math.round(post.dramaScore * 100))}
            {post.tags.map((tag) => ` · #${tag}`).join("")}
          </div>
        </div>
      </div>
      <div style={{ fontSize: 15, lineHeight: 1.65, fontWeight: WEIGHT.regular }}>{post.body}</div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Pressable
          onClick={() => onLike(post)}
          aria-pressed={liked}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "6px 13px",
            background: liked ? COLOR.pink : "#FDEDF3",
            color: liked ? COLOR.white : COLOR.pink,
            borderRadius: RADIUS.pill,
            fontSize: 13,
            fontWeight: WEIGHT.heavy,
            userSelect: "none",
          }}
        >
          ♥ {liked ? t.likeDelivered : t.like}
        </Pressable>
        {commentable && comments.length > 0 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 13px",
              background: COLOR.surface,
              color: COLOR.muted,
              borderRadius: RADIUS.pill,
              fontSize: 13,
              fontWeight: WEIGHT.heavy,
            }}
          >
            <Icon d={ICON.messageCircle} size={14} /> {comments.length}
          </div>
        )}
        {canFollowStory && (
          <Pressable
            onClick={() => setStoryOpen((open) => !open)}
            aria-expanded={storyOpen}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 13px",
              background: storyOpen ? STORY_VIOLET : "#F4EFFC",
              color: storyOpen ? COLOR.white : STORY_VIOLET,
              borderRadius: RADIUS.pill,
              fontSize: 13,
              fontWeight: WEIGHT.heavy,
              userSelect: "none",
            }}
          >
            <Icon d={ICON.gitBranch} size={14} />{" "}
            {storyOpen ? t.collapseStory : t.followStory}
          </Pressable>
        )}
        {canFollowDialogue && (
          // 2차 사건의 존재 — 이 스레드의 대화가 세계에 후속 이야기를 낳았다
          <Pressable
            onClick={() => setDialogueOpen((open) => !open)}
            aria-expanded={dialogueOpen}
            title={
              derived?.latestTitle ? t.latestDerived(derived.latestTitle) : undefined
            }
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 13px",
              background: dialogueOpen ? STORY_VIOLET : "#F4EFFC",
              color: dialogueOpen ? COLOR.white : STORY_VIOLET,
              borderRadius: RADIUS.pill,
              fontSize: 13,
              fontWeight: WEIGHT.heavy,
              userSelect: "none",
            }}
          >
            <Icon d={ICON.sparkles} size={14} />{" "}
            {dialogueOpen
              ? t.collapseBorn
              : derived && derived.count > 1
                ? t.bornCount(derived.count)
                : t.born}
          </Pressable>
        )}
      </div>

      {storyOpen && story && <StoryThread story={story} />}
      {dialogueOpen && dialogueStory && <StoryThread story={dialogueStory} />}

      {threadComments(comments, post.id).map(({ comment: cm, index, depth }) => (
        <div
          // 되읽은 댓글은 event id가 안정 키 — ack 전의 내 댓글만 순번
          key={cm.eventId ?? `local-${index}`}
          style={{
            display: "flex",
            gap: 10,
            padding: "12px 14px",
            background: cm.bg,
            borderRadius: RADIUS.sm,
            // 답장은 부모 바로 아래에 들여쓴다 — 깊이 1 스레드의 결
            marginLeft: depth ? 26 : 0,
          }}
        >
          <Avatar seed={cm.author} label={cm.author} size={28} />
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <div style={{ fontSize: 13, fontWeight: WEIGHT.heavy }}>{cm.author}</div>
            <div style={{ fontSize: 14, lineHeight: 1.55, fontWeight: WEIGHT.regular }}>{cm.text}</div>
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
            placeholder={t.commentPlaceholder(authorLabel)}
            style={{
              flex: 1,
              background: COLOR.surface,
              border: "none",
              outline: "none",
              borderRadius: RADIUS.pill,
              padding: "11px 18px",
              fontSize: 14,
              color: COLOR.ink,
              fontWeight: WEIGHT.regular,
            }}
          />
          <Pressable
            onClick={submit}
            style={{
              padding: "10px 18px",
              background: COLOR.primary,
              color: COLOR.white,
              borderRadius: RADIUS.pill,
              fontSize: 14,
              fontWeight: WEIGHT.heavy,
            }}
          >
            {t.send}
          </Pressable>
        </div>
      )}
    </div>
  );
}

export function LivePosts({
  posts,
  status,
  emptyNotice,
  liked,
  onLike,
  commentsByPost,
  derivedByPost,
  typingPosts,
  onComment,
  authorName,
}: {
  posts: LivePost[];
  status: LiveStatus;
  /** 연결은 살아있는데 목록이 빈 이유(조회 범위 등) — 있으면 빈 상태 문장을 그린다 */
  emptyNotice?: string;
  liked: ReadonlySet<string>;
  onLike: (post: LivePost) => void;
  commentsByPost: Record<string, FeedComment[]>;
  /** 포스트별 "이 대화가 낳은 이야기" 요약 — 없으면 배지도 없다 */
  derivedByPost: Record<string, DerivedStories>;
  typingPosts: ReadonlySet<string>;
  onComment: (post: LivePost, text: string) => void;
  authorName: (actorId: string) => string;
}) {
  const t = useMessages(M);
  const chip = STATUS_CHIP[status];
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ fontSize: 14, fontWeight: WEIGHT.heavy, color: COLOR.ink }}>{t.nowInWorld}</div>
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "3px 10px",
            background: chip.bg,
            color: chip.color,
            borderRadius: RADIUS.pill,
            fontSize: 11,
            fontWeight: WEIGHT.heavy,
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
          {t.statusLabel[status]}
        </div>
      </div>
      {posts.length === 0 && (status !== "live" || emptyNotice) && (
        <div
          style={{
            border: "1.5px dashed #D8E1F0",
            borderRadius: RADIUS.lg,
            padding: "28px 24px",
            textAlign: "center",
            color: COLOR.faint,
            fontSize: 14,
            fontWeight: WEIGHT.semibold,
          }}
        >
          {status !== "live" ? t.emptyOffline : emptyNotice}
        </div>
      )}
      {posts.map((post) => (
        <LivePostCard
          key={post.id}
          post={post}
          liked={liked.has(post.id)}
          onLike={onLike}
          comments={commentsByPost[post.id] ?? []}
          derived={derivedByPost[post.id]}
          typing={typingPosts.has(post.id)}
          onComment={onComment}
          authorLabel={authorName(post.authorId)}
        />
      ))}
    </>
  );
}
