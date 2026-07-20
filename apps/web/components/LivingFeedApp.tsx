"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { TOAST_DURATION_MS } from "@/lib/data";
import { useActorDirectory } from "@/lib/actors";
import type { DerivedStories } from "@/lib/comments";
import { fetchPostComments } from "@/lib/comments";
import { FOCUS_ACTOR_ID, PLAYER_NAME } from "@/lib/config";
import { useRelationshipGraph, useWorldGraph } from "@/lib/graph";
import { useCommunities, useCommunityFeed } from "@/lib/community";
import { useHiddenFeed } from "@/lib/hidden";
import type { LivePost } from "@/lib/live-feed";
import { useLiveFeed } from "@/lib/live-feed";
import type { DmThread } from "@/lib/messages";
import { fetchDmHistory, fetchDmThreads } from "@/lib/messages";
import { useActorProfile } from "@/lib/profile";
import { rangeTickBounds, type Range } from "@/lib/range";
import { naturalDelayMs, useActorSession } from "@/lib/session";
import type { DmMessage, FeedComment, Screen, Tab, Toast } from "@/lib/types";
import { currentTick } from "@/lib/world-clock";

import { Curating } from "./Curating";
import { Digest } from "./Digest";
import { DmTab } from "./DmTab";
import { FeedTab } from "./FeedTab";
import { CommunityTab } from "./CommunityTab";
import { HiddenTab } from "./HiddenTab";
import { Onboarding } from "./Onboarding";
import { ProfileTab } from "./ProfileTab";
import { Sidebar } from "./Sidebar";
import { TabFallback } from "./TabFallback";
import { Toasts } from "./Toasts";

// 코드 스플리팅 — 가장 무겁고 드물게 여는 탭은 초기 번들에서 빼고 열 때 청크를
// 받아온다 (첫 화면=World Feed의 TTI 개선). 클라이언트 전용이라 ssr:false.
const StudioTab = dynamic(() => import("./StudioTab").then((m) => m.StudioTab), {
  ssr: false,
  loading: () => <TabFallback />,
});
const GraphTab = dynamic(() => import("./GraphTab").then((m) => m.GraphTab), {
  ssr: false,
  loading: () => <TabFallback />,
});

const ME_COMMENT = { bg: "#EDF3FD", avatarBg: "#D9E2F2" };
const ACTOR_COMMENT = { bg: "#F8FAFD", avatarBg: "#AFC8F5" };
// 내 댓글의 자기표시 — 표시명은 config seam에서 온다 (원시 id 노출 금지)
const MY_COMMENT_AUTHOR = `${PLAYER_NAME} (나)`;

export function LivingFeedApp() {
  const [screen, setScreen] = useState<Screen>("onboarding");
  const [tab, setTab] = useState<Tab>("feed");
  // 조회 범위(세계 시간, lib/range) — 탭마다 독립 선택, 기본은 전체(현행 화면)
  const [feedRange, setFeedRange] = useState<Range>("all");
  const [hiddenRange, setHiddenRange] = useState<Range>("all");
  const [dmRange, setDmRange] = useState<Range>("all");
  // 선택된 커뮤니티 — 내집단 렌즈(ADR-014). null이면 목록의 첫 커뮤니티를 자동 선택
  const [selectedCommunity, setSelectedCommunity] = useState<string | null>(null);
  const [profileRange, setProfileRange] = useState<Range>("all");
  const [topics, setTopics] = useState<string[]>(["직장 드라마"]);
  const [curateStep, setCurateStep] = useState(0);

  // 다중 대화 인박스 — 스레드 목록(threads API 실측) + 액터별 대화 상태.
  // 특정 액터 고정 없음: 어떤 액터가 먼저 말을 걸어와도(선제 DM) 스레드가 된다.
  const [dmThreads, setDmThreads] = useState<DmThread[]>([]);
  // 열린 스레드의 상대 — null이면 목록 뷰
  const [openDmActor, setOpenDmActor] = useState<string | null>(null);
  const [dmMsgsByActor, setDmMsgsByActor] = useState<Record<string, DmMessage[]>>({});
  const [dmDraft, setDmDraft] = useState("");
  // 입력 중(답장 준비)인 액터들 — 열린 스레드에선 말풍선, 목록에선 '입력 중...'
  const [dmTypingActors, setDmTypingActors] = useState<ReadonlySet<string>>(new Set());
  // 아직 열어보지 않은 새 DM이 있는 스레드들 — 목록·사이드바 배지의 근거
  const [dmUnread, setDmUnread] = useState<ReadonlySet<string>>(new Set());
  // 더 과거 대화로 가는 커서 (액터별, read.messages) — 키 없음=미로드, null=끝/미가용
  const [dmCursorByActor, setDmCursorByActor] = useState<Record<string, string | null>>({});
  const [dmLoadingOlder, setDmLoadingOlder] = useState(false);

  // 라이브 포스트별 댓글 — 댓글은 특정 데모 카드가 아니라 실제 포스트에 붙는다
  const [commentsByPost, setCommentsByPost] = useState<Record<string, FeedComment[]>>({});
  const [typingPosts, setTypingPosts] = useState<ReadonlySet<string>>(new Set());
  // 포스트별 "이 대화가 낳은 이야기" 요약 — 개입 사슬을 승계한 후속 포스트 (plan/03)
  const [derivedByPost, setDerivedByPost] = useState<Record<string, DerivedStories>>({});

  const [toasts, setToasts] = useState<Toast[]>([]);
  const [selectedActor, setSelectedActor] = useState<string | null>(null);
  const [following, setFollowing] = useState(true);
  const [interventions, setInterventions] = useState(0);
  const [coachDismissed, setCoachDismissed] = useState(false);

  const toastSeq = useRef(0);
  const timers = useRef<number[]>([]);

  // 지연 콜백(타이핑 연출 뒤 도착)이 '지금 보고 있는 화면'을 판단할 때 쓰는 최신값 창
  const tabRef = useRef(tab);
  tabRef.current = tab;
  const openDmActorRef = useRef(openDmActor);
  openDmActorRef.current = openDmActor;

  // 적재 확인을 기다리는 댓글 커맨드 — seq → post id (ack가 오면 event id를 채운다)
  const pendingCommentAcks = useRef<Map<number, string>>(new Map());

  const after = useCallback((ms: number, fn: () => void) => {
    timers.current.push(window.setTimeout(fn, ms));
  }, []);

  useEffect(() => {
    // 지연 콜백 타이머 정리 — 세계 시계는 리프(useWorldClock)로 분리해 3초 틱이
    // 앱 전체를 리렌더하지 않게 했다 (world-clock-display.ts).
    const pending = timers.current;
    return () => pending.forEach((t) => window.clearTimeout(t));
  }, []);

  const toast = useCallback(
    (t: Omit<Toast, "id">) => {
      const id = ++toastSeq.current;
      setToasts((list) => [...list, { ...t, id }]);
      after(TOAST_DURATION_MS, () => setToasts((list) => list.filter((x) => x.id !== id)));
    },
    [after],
  );

  // 실 백엔드 라이브 피드 — 세계 입장 후에만 구독 (미가용이면 오프라인 상태만).
  // 범위 선택 시 recent+from_tick 서버 조회가 합쳐진다 (SSE 구독은 범위와 무관).
  const { posts: livePosts, status: liveStatus } = useLiveFeed(screen === "app", feedRange);

  // 관계 그래프 실측 (kuzu-projector, ADR-006) — 미가용이면 빈 상태
  // 관계 그래프는 그래프 탭에서만 소비된다 — 다른 탭에서 20초마다 폴링하지 않는다
  // (worldGraph와 같은 게이팅). 탭 전환 시 available=false→데이터로 자연 강등.
  const relGraph = useRelationshipGraph(screen === "app" && tab === "graph");
  // 세계 전체 관계망 (액터↔액터 포함) — 그래프 탭을 볼 때만 폴링한다
  const worldGraph = useWorldGraph(screen === "app" && tab === "graph");

  // Hidden Feed — 당신에게만 닿은 비공개 이야기 (private 타임라인, ADR-014).
  // 신뢰가 열어준다: 닿은 것이 하나라도 있었으면 언락 — 범위 조회가 비어도
  // 래치는 유지된다 (범위는 조회 조건이지 신뢰의 철회가 아니다).
  const hidden = useHiddenFeed(screen === "app", hiddenRange);
  const hiddenUnlocked = hidden.unlocked;

  // 커뮤니티 — 세계의 내집단 렌즈 (ADR-014). 목록은 상시, 피드는 탭 활성 시에만
  const communities = useCommunities(screen === "app");
  const communityFeed = useCommunityFeed(
    selectedCommunity, screen === "app" && tab === "community",
  );

  // 액터 명단(read.actors) — 표시 이름을 여기서 읽는다 (하드코딩 금지, ADR-012)
  const { byId } = useActorDirectory(screen === "app");
  const authorName = useCallback(
    // 식별자는 사람 이름이 아니다 — 이름을 모르는 인물은 '누군가'로 둔다 (내레이터와 같은 결)
    (actorId: string) => byId.get(actorId)?.name ?? "누군가",
    [byId],
  );
  const identityOf = useCallback((actorId: string) => byId.get(actorId), [byId]);
  // 검색어→작성자 id 역해석 — 인덱스는 이름을 모르므로 로스터가 다리를 놓는다
  const matchActorIds = useCallback(
    (q: string) => {
      const needle = q.trim().toLowerCase();
      if (!needle) return [];
      return [...byId.values()]
        .filter((a) => a.name.toLowerCase().includes(needle))
        .map((a) => a.actorId)
        .slice(0, 8);
    },
    [byId],
  );

  // 액터의 내면 실측 (pg-projector 신념·에피소드, ADR-003/008) — 미가용이면 데모 서사.
  // 겪은 일(에피소드)만 범위 조회를 받는다 — 신념·아크는 "지금의 내면"이다.
  const focusProfile = useActorProfile(FOCUS_ACTOR_ID, screen === "app", profileRange);

  // 되읽은 댓글 합류 — 이미 화면에 있는 분(라이브 WS·방금 쓴 내 댓글) 앞에
  // 시간순으로 이어 붙인다 (event id 중복 제거 — DM 히스토리와 같은 규약)
  const mergeLoadedComments = useCallback((postId: string, loaded: FeedComment[]) => {
    setCommentsByPost((prev) => {
      const current = prev[postId] ?? [];
      const seen = new Set(current.map((c) => c.eventId).filter(Boolean));
      const older = loaded.filter((c) => !c.eventId || !seen.has(c.eventId));
      if (older.length === 0) return prev;
      return { ...prev, [postId]: [...older, ...current] };
    });
  }, []);

  // 피드에 뜬 포스트의 지난 댓글 되읽기 (read.messages) — 새로고침에도 내 개입의
  // 흔적과 액터들의 소셜 루프가 남는다. 포스트당 한 번만 요청한다 (미가용이면
  // 라이브 분만 — 조용한 강등).
  const commentsRequested = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (screen !== "app") return;
    for (const post of livePosts) {
      if (!post.authorId.startsWith("a_")) continue; // 댓글은 액터 포스트에만 붙는다
      if (commentsRequested.current.has(post.id)) continue;
      commentsRequested.current.add(post.id);
      void fetchPostComments(post.id).then((loaded) => {
        if (!loaded) return;
        // 파생 요약 — 이 대화가 낳은 후속 포스트의 존재 (배지·사슬 어포던스 재료)
        if (loaded.derived && loaded.derived.count > 0) {
          const derived = loaded.derived;
          setDerivedByPost((prev) => ({ ...prev, [post.id]: derived }));
        }
        if (loaded.comments.length === 0) return;
        mergeLoadedComments(
          post.id,
          loaded.comments.map((c) => ({
            // 이름 그라운딩: 액터는 BE 동봉 이름(없으면 디렉터리→'누군가'), 나는 자기표시
            author: c.isMine
              ? MY_COMMENT_AUTHOR
              : c.authorKind === "actor"
                ? (c.authorName ?? authorName(c.authorId))
                : "다른 관찰자",
            text: c.text,
            eventId: c.eventId,
            inReplyTo: c.inReplyTo,
            ...(c.isMine ? ME_COMMENT : ACTOR_COMMENT),
          })),
        );
      });
    }
  }, [screen, livePosts, authorName, mergeLoadedComments]);

  // 인박스 목록 이어받기 (threads API) — 액터별 마지막 메시지 1건, 최신 대화 순.
  // 로컬에서 이미 대화가 시작됐으면 덮지 않는다 (히스토리와 같은 '빈 곳만 채우기' 규약).
  useEffect(() => {
    if (screen !== "app") return;
    let cancelled = false;
    void fetchDmThreads().then((threads) => {
      if (cancelled || !threads || threads.length === 0) return;
      setDmThreads((current) => (current.length === 0 ? threads : current));
    });
    return () => {
      cancelled = true;
    };
  }, [screen]);

  // 지난 대화 이어받기 (read.messages) — 스레드를 연 순간, 아직 아무 말도 안 한 스레드만 채운다
  useEffect(() => {
    if (screen !== "app" || openDmActor === null) return;
    if (openDmActor in dmCursorByActor) return; // 이미 이어받은 스레드
    const actorId = openDmActor;
    let cancelled = false;
    void fetchDmHistory(actorId).then((page) => {
      if (cancelled || !page) return;
      setDmCursorByActor((prev) => ({ ...prev, [actorId]: page.nextCursor }));
      if (page.messages.length === 0) return;
      setDmMsgsByActor((prev) =>
        (prev[actorId] ?? []).length === 0 ? { ...prev, [actorId]: page.messages } : prev,
      );
    });
    return () => {
      cancelled = true;
    };
  }, [screen, openDmActor, dmCursorByActor]);

  // 이전 대화 더 보기 — 과거 페이지를 받아 목록 위에 이어 붙인다 (event id 중복 제거)
  const loadOlderDms = useCallback(() => {
    const actorId = openDmActor;
    if (!actorId) return;
    const cursor = dmCursorByActor[actorId];
    if (!cursor || dmLoadingOlder) return;
    setDmLoadingOlder(true);
    void fetchDmHistory(actorId, cursor).then((page) => {
      setDmLoadingOlder(false);
      // 미가용 — 커서를 남겨 다음 클릭에 재시도 (조용한 강등)
      if (!page) return;
      setDmCursorByActor((prev) => ({ ...prev, [actorId]: page.nextCursor }));
      if (page.messages.length === 0) return;
      setDmMsgsByActor((prev) => {
        const current = prev[actorId] ?? [];
        const seen = new Set(current.map((m) => m.eventId).filter(Boolean));
        const older = page.messages.filter((m) => !m.eventId || !seen.has(m.eventId));
        // 페이지는 시간 오름차순 — 앞에 붙이면 전체 시간순이 유지된다
        return { ...prev, [actorId]: [...older, ...current] };
      });
    });
  }, [openDmActor, dmCursorByActor, dmLoadingOlder]);

  // 스레드에 새 마지막 한 줄 반영 — 목록 맨 위로 (없던 상대면 새 스레드가 된다)
  const touchThread = useCallback((actorId: string, text: string, fromActor: boolean) => {
    setDmThreads((prev) => [
      {
        actorId,
        lastText: text,
        lastChannel: "dm",
        lastAt: new Date().toISOString(),
        lastFromActor: fromActor,
      },
      ...prev.filter((t) => t.actorId !== actorId),
    ]);
  }, []);

  const appendDm = useCallback((actorId: string, msg: DmMessage) => {
    setDmMsgsByActor((prev) => ({ ...prev, [actorId]: [...(prev[actorId] ?? []), msg] }));
  }, []);

  // 상호작용 세션 (WS) — DM/댓글/좋아요를 실세계에 꽂는다. 미가용이면 데모 폴백.
  const session = useActorSession({
    enabled: screen === "app",
    onReply: (reply) => {
      // 도착 즉시 렌더하지 않는다 — 1~3초 '타이핑'을 거쳐야 사람 같다
      if (reply.channel === "dm") {
        const actorId = reply.actorId;
        if (!actorId) return; // 발신 액터 미상 — 어느 스레드에도 꽂을 수 없다
        setDmTypingActors((prev) => new Set(prev).add(actorId));
        after(naturalDelayMs(), () => {
          setDmTypingActors((prev) => {
            const next = new Set(prev);
            next.delete(actorId);
            return next;
          });
          appendDm(actorId, { from: "actor", text: reply.text });
          // 선제 DM 포함 — 없던 상대면 여기서 새 스레드가 태어난다
          touchThread(actorId, reply.text, true);
          // 지금 그 스레드를 보고 있지 않으면 미읽음 (목록·사이드바 배지)
          if (!(tabRef.current === "dm" && openDmActorRef.current === actorId)) {
            setDmUnread((prev) => new Set(prev).add(actorId));
          }
        });
      } else if (reply.channel === "comment" && reply.postId) {
        const postId = reply.postId;
        after(naturalDelayMs(), () => {
          setTypingPosts((prev) => {
            const next = new Set(prev);
            next.delete(postId);
            return next;
          });
          setCommentsByPost((prev) => {
            const current = prev[postId] ?? [];
            // 되읽기(REST)와 라이브(WS)가 겹칠 수 있다 — event id로 한 번만 싣는다
            if (current.some((c) => c.eventId === reply.eventId)) return prev;
            return {
              ...prev,
              [postId]: [
                ...current,
                {
                  author: authorName(reply.actorId),
                  text: reply.text,
                  eventId: reply.eventId,
                  // 답장 좌표 — 내 댓글(ack로 event id를 받은) 밑에 중첩된다
                  inReplyTo: reply.inReplyTo,
                  ...ACTOR_COMMENT,
                },
              ],
            };
          });
        });
      }
    },
    // 댓글 커맨드의 적재 확인 — 방금 쓴 내 댓글에 event id를 채운다.
    // 이게 있어야 액터의 라이브 답장(in_reply_to = 내 댓글 id)이 내 말 밑에 붙는다.
    onAck: (seq, eventId) => {
      const postId = pendingCommentAcks.current.get(seq);
      if (!postId) return; // 댓글이 아닌 커맨드(좋아요·팔로우)의 ack
      pendingCommentAcks.current.delete(seq);
      setCommentsByPost((prev) => {
        const current = prev[postId];
        if (!current) return prev;
        return {
          ...prev,
          [postId]: current.map((c) => (c.localSeq === seq ? { ...c, eventId } : c)),
        };
      });
    },
  });

  const [likedLive, setLikedLive] = useState<ReadonlySet<string>>(new Set());
  const likeLivePost = useCallback(
    (post: LivePost) => {
      setLikedLive((prev) => {
        if (prev.has(post.id)) return prev;
        const next = new Set(prev);
        next.add(post.id);
        return next;
      });
      setInterventions((n) => n + 1);
      setCoachDismissed(true);
      // 오프라인이면 로컬 하트만 남는다 — 개입은 세계가 살아있을 때만 흔적이 된다
      session.addReaction(post.authorId, post.id);
    },
    [session],
  );

  const commentOnPost = useCallback(
    (post: LivePost, text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      // 실세계 경로 — player.comment.posted 적재, 응답은 onReply(channel=comment)로 온다.
      // 오프라인이면 답장은 오지 않는다 (세계가 살아있을 때만 반응이 있다).
      const seq = session.sendComment(post.authorId, post.id, trimmed);
      if (seq !== null) pendingCommentAcks.current.set(seq, post.id);
      setCommentsByPost((prev) => ({
        ...prev,
        [post.id]: [
          ...(prev[post.id] ?? []),
          // localSeq — ack(event id)가 오면 이 댓글이 답장 중첩의 부모가 된다
          { author: MY_COMMENT_AUTHOR, text: trimmed, ...ME_COMMENT,
            ...(seq !== null ? { localSeq: seq } : {}) },
        ],
      }));
      setInterventions((n) => n + 1);
      setCoachDismissed(true);
      if (seq !== null) {
        setTypingPosts((prev) => new Set(prev).add(post.id));
        toast({
          icon: "check",
          iconBg: "#E3F5EC",
          iconColor: "#3E8A66",
          title: "댓글이 전달되었어요",
          body: `${authorName(post.authorId)}에게 닿았어요 — 개입은 흔적을 남겨요`,
        });
      }
    },
    [session, toast, authorName],
  );

  const toggleTopic = useCallback((label: string) => {
    setTopics((prev) =>
      prev.includes(label) ? prev.filter((t) => t !== label) : [...prev, label].slice(-2),
    );
  }, []);

  const enterWorld = useCallback(() => {
    setScreen("curating");
    setCurateStep(0);
    after(1100, () => setCurateStep(1));
    after(2300, () => setCurateStep(2));
    after(3500, () => setScreen("app"));
  }, [after]);

  const sendDm = useCallback(() => {
    const actorId = openDmActor;
    if (!actorId) return;
    const text = dmDraft.trim();
    if (!text || dmTypingActors.has(actorId)) return;
    appendDm(actorId, { from: "me", text });
    touchThread(actorId, text, false);
    setDmDraft("");
    setInterventions((n) => n + 1);
    // 실세계 경로 — player.dm.sent 적재, 액터의 응답은 다음 tick에 push로 온다.
    // 오프라인이면 답장은 오지 않는다 (세계가 살아있을 때만 반응이 있다).
    if (session.sendDm(actorId, text)) {
      setDmTypingActors((prev) => new Set(prev).add(actorId));
    }
  }, [openDmActor, dmDraft, dmTypingActors, appendDm, touchThread, session]);

  const closeToast = useCallback((id: number) => {
    setToasts((list) => list.filter((x) => x.id !== id));
  }, []);

  // 스레드 열기 — 대화 뷰로 전환, 미읽음 해제 (히스토리 로드는 위 effect가 이어받는다)
  const openThread = useCallback((actorId: string) => {
    setOpenDmActor(actorId);
    setDmDraft("");
    setDmUnread((prev) => {
      if (!prev.has(actorId)) return prev;
      const next = new Set(prev);
      next.delete(actorId);
      return next;
    });
  }, []);

  // 열린 스레드로 돌아오면 미읽음 해제 — 다른 탭에 가 있는 동안 도착한 DM의 배지 정리
  useEffect(() => {
    if (tab !== "dm" || openDmActor === null) return;
    setDmUnread((prev) => {
      if (!prev.has(openDmActor)) return prev;
      const next = new Set(prev);
      next.delete(openDmActor);
      return next;
    });
  }, [tab, openDmActor, dmUnread]);

  // 프로필의 "메시지 보내기" — 보고 있던 액터와의 스레드를 바로 연다
  const goDm = useCallback(() => {
    setTab("dm");
    openThread(FOCUS_ACTOR_ID);
  }, [openThread]);

  // 자식 memo가 실제로 먹도록 인라인 화살표 prop을 안정화한다 (참조 고정)
  const dismissCoach = useCallback(() => setCoachDismissed(true), []);
  const goFeed = useCallback(() => setTab("feed"), []);
  const closeThread = useCallback(() => setOpenDmActor(null), []);
  const toggleFollow = useCallback(() => {
    // 팔로우는 세계에 남는 선언 — 세션이 살아있으면 함께 보낸다 (오프라인은 로컬 토글)
    setFollowing((prev) => {
      const next = !prev;
      session.setFollow(FOCUS_ACTOR_ID, next);
      return next;
    });
  }, [session]);

  // 받은 것의 조회 범위 — 마지막 메시지의 세계 tick(서버 동봉 last_tick)으로
  // 클라에서 거른다. 목록이 로컬 우선(방금 오간 말의 낙관 갱신)이라 서버 재조회
  // 대신 이 방식이 정직하다 — 로컬 항목(tick 미상)은 "지금"이니 어느 범위에도 든다.
  const dmVisibleThreads = useMemo(() => {
    const bounds = rangeTickBounds(dmRange, currentTick());
    if (bounds === null) return dmThreads;
    return dmThreads.filter((t) => t.lastTick === undefined || t.lastTick >= bounds.fromTick);
  }, [dmRange, dmThreads]);

  // 실측 스레드가 하나도 없을 때의 시작점 — 기존 데모 인트로 경험 보존:
  // 포커스 액터에게 먼저 말을 걸 수 있는 스레드 하나를 남긴다 (이름은 디렉터리 파생)
  const displayThreads: DmThread[] =
    dmThreads.length > 0
      ? dmVisibleThreads
      : [{ actorId: FOCUS_ACTOR_ID, lastText: "", lastChannel: "dm", lastAt: "", lastFromActor: false }];

  return (
    <div style={{ height: "100vh", display: "flex", overflow: "hidden", position: "relative" }}>
      {screen === "onboarding" && (
        <Onboarding
          topics={topics}
          onToggleTopic={toggleTopic}
          onEnter={enterWorld}
        />
      )}
      {screen === "curating" && <Curating step={curateStep} />}

      <Sidebar
        tab={tab}
        onSelectTab={setTab}
        dmBadge={dmUnread.size > 0 ? String(dmUnread.size) : ""}
        hiddenUnlocked={hiddenUnlocked}
        interventions={interventions}
      />

      <div
        style={{
          flex: 1,
          background: "#fff",
          borderLeft: "1.5px solid #E2EAF6",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {tab === "feed" && (
          <FeedTab
            livePosts={livePosts}
            liveStatus={liveStatus}
            range={feedRange}
            onRangeChange={setFeedRange}
            likedLive={likedLive}
            onLikeLive={likeLivePost}
            commentsByPost={commentsByPost}
            derivedByPost={derivedByPost}
            typingPosts={typingPosts}
            onComment={commentOnPost}
            authorName={authorName}
            matchActorIds={matchActorIds}
            showCoach={screen === "app" && !coachDismissed}
            onDismissCoach={dismissCoach}
          />
        )}
        {tab === "profile" && (
          <ProfileTab
            following={following}
            onToggleFollow={toggleFollow}
            goDm={goDm}
            profile={focusProfile.profile}
            episodeRange={profileRange}
            onEpisodeRangeChange={setProfileRange}
            hasMoreEpisodes={focusProfile.hasMoreEpisodes}
            loadingEpisodes={focusProfile.loadingEpisodes}
            onLoadMoreEpisodes={focusProfile.loadMoreEpisodes}
          />
        )}
        {tab === "dm" && (
          <DmTab
            threads={displayThreads}
            emptyInbox={dmThreads.length === 0}
            range={dmRange}
            onRangeChange={setDmRange}
            nameOf={authorName}
            unread={dmUnread}
            typingActors={dmTypingActors}
            openActorId={openDmActor}
            messages={openDmActor ? (dmMsgsByActor[openDmActor] ?? []) : []}
            draft={dmDraft}
            onDraftChange={setDmDraft}
            onSend={sendDm}
            canLoadOlder={
              openDmActor !== null && (dmCursorByActor[openDmActor] ?? null) !== null
            }
            loadingOlder={dmLoadingOlder}
            onLoadOlder={loadOlderDms}
            onOpenThread={openThread}
            onBack={closeThread}
          />
        )}
        {tab === "community" && (
          <CommunityTab
            communities={communities}
            selectedId={selectedCommunity}
            onSelect={setSelectedCommunity}
            posts={communityFeed.posts}
            available={communityFeed.available}
            nameOf={authorName}
          />
        )}
        {tab === "hidden" && (
          <HiddenTab
            items={hidden.items}
            nameOf={authorName}
            range={hiddenRange}
            onRangeChange={setHiddenRange}
          />
        )}
        {tab === "studio" && (
          // 창조자 도구 — 인물을 빚어 세계에 풀어놓는다. 데뷔는 World Feed에서 지켜본다.
          <StudioTab enabled={screen === "app"} onGoWorldFeed={goFeed} />
        )}
        {tab === "graph" && (
          <GraphTab
            edges={relGraph.edges}
            available={relGraph.available}
            world={worldGraph}
            nameOf={authorName}
            identityOf={identityOf}
            selected={selectedActor}
            onSelect={setSelectedActor}
          />
        )}
      </div>

      {/* 개인 다이제스트 — 입장 직후 부재가 임계 이상이면 재회 카드 (plan/11 §D7) */}
      {screen === "app" && <Digest nameOf={authorName} />}

      <Toasts toasts={toasts} onClose={closeToast} />
    </div>
  );
}
