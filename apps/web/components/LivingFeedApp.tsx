"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  COMMENT_REPLIES,
  DM_REPLIES,
  MINJI_POST_FULL,
  STREAM_SPEED_MS,
  TOAST_DURATION_MS,
  WORLD_MIN_START,
  formatWorldTime,
} from "@/lib/data";
import { useRelationshipGraph } from "@/lib/graph";
import type { LivePost } from "@/lib/live-feed";
import { useLiveFeed } from "@/lib/live-feed";
import { fetchDmHistory } from "@/lib/messages";
import { useActorProfile } from "@/lib/profile";
import { naturalDelayMs, useActorSession } from "@/lib/session";
import type { DmMessage, FeedComment, RelKey, Screen, Tab, Toast } from "@/lib/types";

/** 데모 DM 상대 — 실제 액터로 존재한다 (agents/personas/minji-kim.yaml) */
const MINJI_ACTOR_ID = "a_minji_kim";

import { Curating } from "./Curating";
import { DmTab } from "./DmTab";
import { FeedTab } from "./FeedTab";
import { GraphTab } from "./GraphTab";
import { HiddenTab } from "./HiddenTab";
import { Onboarding } from "./Onboarding";
import { ProfileTab } from "./ProfileTab";
import { Sidebar } from "./Sidebar";
import { Toasts } from "./Toasts";

const INITIAL_DM: DmMessage[] = [
  { from: "me", text: "기획안 얘기 봤어요. 그거 민지님 아이디어인 거 팀 사람들도 다 알 거예요." },
  {
    from: "minji",
    text: "어제 그 얘기, 하루 종일 생각했어요. 고마워요. 사실 아무한테도 말 못 하고 있었거든요.",
  },
];

export function LivingFeedApp() {
  const [screen, setScreen] = useState<Screen>("onboarding");
  const [tab, setTab] = useState<Tab>("feed");
  const [topics, setTopics] = useState<string[]>(["직장 드라마"]);
  const [worldMin, setWorldMin] = useState(WORLD_MIN_START);
  const [curateStep, setCurateStep] = useState(0);

  const [streamText, setStreamText] = useState("");
  const [streamDone, setStreamDone] = useState(false);

  const [minjiLiked, setMinjiLiked] = useState(false);
  const [minjiLikes, setMinjiLikes] = useState(30);
  const [chulsuLiked, setChulsuLiked] = useState(false);
  const [chulsuLikes, setChulsuLikes] = useState(12);

  const [minjiComments, setMinjiComments] = useState<FeedComment[]>([]);
  const [commentDraft, setCommentDraft] = useState("");
  const [minjiTyping, setMinjiTyping] = useState(false);

  const [dmMsgs, setDmMsgs] = useState<DmMessage[]>(INITIAL_DM);
  const [dmDraft, setDmDraft] = useState("");
  const [dmTyping, setDmTyping] = useState(false);

  const [toasts, setToasts] = useState<Toast[]>([]);
  const [graphSel, setGraphSel] = useState<RelKey>("mc");
  const [followMinji, setFollowMinji] = useState(true);
  const [interventions, setInterventions] = useState(12);
  const [coachDismissed, setCoachDismissed] = useState(false);
  const [hiddenUnlocked, setHiddenUnlocked] = useState(false);

  // 시나리오 진행 카운터 — 렌더에 쓰이지 않으므로 ref
  const commentStep = useRef(0);
  const dmIdx = useRef(0);
  const chulsuVisited = useRef(false);
  const toastSeq = useRef(0);

  const timers = useRef<number[]>([]);
  const streamTimer = useRef<number | undefined>(undefined);

  const after = useCallback((ms: number, fn: () => void) => {
    timers.current.push(window.setTimeout(fn, ms));
  }, []);

  useEffect(() => {
    // 세계 시간: 3초마다 +4분 (현실 4배속)
    const clock = window.setInterval(() => setWorldMin((m) => m + 4), 3000);
    const pending = timers.current;
    return () => {
      window.clearInterval(clock);
      if (streamTimer.current !== undefined) window.clearInterval(streamTimer.current);
      pending.forEach((t) => window.clearTimeout(t));
    };
  }, []);

  const toast = useCallback(
    (t: Omit<Toast, "id">) => {
      const id = ++toastSeq.current;
      setToasts((list) => [...list, { ...t, id }]);
      after(TOAST_DURATION_MS, () => setToasts((list) => list.filter((x) => x.id !== id)));
    },
    [after],
  );

  // 실 백엔드 라이브 피드 — 세계 입장 후에만 구독 (미가용이면 데모만 동작)
  const { posts: livePosts, status: liveStatus } = useLiveFeed(screen === "app");

  // 관계 그래프 실측 (kuzu-projector, ADR-006) — 미가용이면 데모 배치 유지
  const relGraph = useRelationshipGraph(screen === "app");

  // 민지의 내면 실측 (pg-projector 신념·에피소드, ADR-003/008) — 미가용이면 데모 서사
  const minjiProfile = useActorProfile(MINJI_ACTOR_ID, screen === "app");

  // 지난 대화 이어받기 (read.messages) — 아직 데모 인트로 그대로일 때만 교체한다:
  // 사용자가 이미 대화를 시작했다면 히스토리가 그 위를 덮어쓰면 안 된다
  useEffect(() => {
    if (screen !== "app") return;
    let cancelled = false;
    void fetchDmHistory(MINJI_ACTOR_ID).then((history) => {
      if (cancelled || !history) return;
      setDmMsgs((current) => (current === INITIAL_DM ? history : current));
    });
    return () => {
      cancelled = true;
    };
  }, [screen]);

  // 상호작용 세션 (WS) — DM/좋아요를 실세계에 꽂는다. 미가용이면 데모 폴백.
  const session = useActorSession({
    enabled: screen === "app",
    onReply: (reply) => {
      if (reply.channel === "dm") {
        // 도착 즉시 렌더하지 않는다 — 1~3초 '타이핑'을 거쳐야 사람 같다
        setDmTyping(true);
        after(naturalDelayMs(), () => {
          setDmTyping(false);
          setDmMsgs((list) => [...list, { from: "minji", text: reply.text }]);
        });
      }
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
      // 오프라인이면 로컬 하트만 남는다 — 개입은 세계가 살아있을 때만 흔적이 된다
      session.addReaction(post.authorId, post.id);
    },
    [session],
  );

  const startStream = useCallback(() => {
    let i = 0;
    if (streamTimer.current !== undefined) window.clearInterval(streamTimer.current);
    streamTimer.current = window.setInterval(() => {
      i += 1;
      if (i >= MINJI_POST_FULL.length) {
        window.clearInterval(streamTimer.current);
        setStreamText(MINJI_POST_FULL);
        setStreamDone(true);
      } else {
        setStreamText(MINJI_POST_FULL.slice(0, i));
      }
    }, STREAM_SPEED_MS);
  }, []);

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
    after(3500, () => {
      setScreen("app");
      startStream();
    });
  }, [after, startStream]);

  const likeMinji = useCallback(() => {
    // 주의: updater 안에서 다른 setState를 부르면 StrictMode 이중 호출로 값이 틀어진다
    const liked = !minjiLiked;
    setMinjiLiked(liked);
    setMinjiLikes((n) => n + (liked ? 1 : -1));
    if (liked) setInterventions((n) => n + 1);
    setCoachDismissed(true);
  }, [minjiLiked]);

  const likeChulsu = useCallback(() => {
    const first = !chulsuVisited.current && !chulsuLiked;
    chulsuVisited.current = true;
    const liked = !chulsuLiked;
    setChulsuLiked(liked);
    setChulsuLikes((n) => n + (liked ? 1 : -1));
    if (liked) setInterventions((n) => n + 1);
    setCoachDismissed(true);
    if (first) {
      after(2000, () =>
        toast({
          icon: "user-round",
          iconBg: "#FFF6DE",
          iconColor: "#A87F24",
          title: "철수가 당신의 프로필을 방문했어요",
          body: '"좋아요 눌러주신 분이군요. 소개팅 얘기 또 올릴게요." — 당신의 좋아요를 기억해요',
        }),
      );
    }
  }, [after, chulsuLiked, toast]);

  const sendComment = useCallback(() => {
    const text = commentDraft.trim();
    if (!text || minjiTyping) return;
    setMinjiComments((list) => [
      ...list,
      { author: "관찰자_0417 (나)", text, bg: "#EDF3FD", avatarBg: "#D9E2F2" },
    ]);
    setCommentDraft("");
    setMinjiTyping(true);
    setInterventions((n) => n + 1);
    toast({
      icon: "check",
      iconBg: "#E3F5EC",
      iconColor: "#3E8A66",
      title: "댓글이 전달되었어요",
      body: "민지가 곧 확인해요 — 지금 깨어 있어요",
    });
    const step = Math.min(commentStep.current, COMMENT_REPLIES.length - 1);
    const replyDelay = naturalDelayMs(); // 즉답 금지 — 1~3초 생각하고 답한다
    after(replyDelay, () => {
      commentStep.current += 1;
      setMinjiTyping(false);
      setMinjiComments((list) => [
        ...list,
        { author: "김민지", text: COMMENT_REPLIES[step], bg: "#F8FAFD", avatarBg: "#AFC8F5" },
      ]);
    });
    if (step === 0) {
      after(replyDelay + 2400, () =>
        toast({
          icon: "git-branch",
          iconBg: "#EDF3FD",
          iconColor: "#5F7EC9",
          title: "당신의 댓글이 세계를 바꿨어요",
          body: "민지가 팀장과의 면담을 잡았어요 · 민지↔철수의 긴장이 조금 풀렸어요",
        }),
      );
      after(replyDelay + 6500, () =>
        toast({
          icon: "feather",
          iconBg: "#FFF6DE",
          iconColor: "#A87F24",
          title: "당신이 시작한 이야기 — '면담의 날' 1화",
          body: "오늘 당신의 댓글에서 시작된 사건 연쇄. 이 드라마의 원작자는 당신이에요.",
        }),
      );
      after(replyDelay + 10500, () => {
        setHiddenUnlocked(true);
        toast({
          icon: "lock-open",
          iconBg: "#F1EDFB",
          iconColor: "#7a68b3",
          title: "Hidden Feed가 열렸어요",
          body: "민지가 당신에게만 털어놓는 이야기가 있어요. 사이드바에서 확인해보세요.",
        });
      });
    }
  }, [after, commentDraft, minjiTyping, toast]);

  const sendDm = useCallback(() => {
    const text = dmDraft.trim();
    if (!text || dmTyping) return;
    setDmMsgs((list) => [...list, { from: "me", text }]);
    setDmDraft("");
    setDmTyping(true);
    setInterventions((n) => n + 1);
    // 실세계 경로 — player.dm.sent 적재, 민지의 응답은 다음 tick에 push로 온다
    if (session.sendDm(MINJI_ACTOR_ID, text)) return;
    // 오프라인 데모 폴백 (프로토타입 시나리오) — 즉답 금지, 1~3초 타이핑
    const idx = Math.min(dmIdx.current, DM_REPLIES.length - 1);
    after(naturalDelayMs(), () => {
      dmIdx.current += 1;
      setDmTyping(false);
      setDmMsgs((list) => [...list, { from: "minji", text: DM_REPLIES[idx] }]);
    });
  }, [after, dmDraft, dmTyping, session]);

  const closeToast = useCallback((id: number) => {
    setToasts((list) => list.filter((x) => x.id !== id));
  }, []);

  const worldTime = formatWorldTime(worldMin);
  const goProfile = useCallback(() => setTab("profile"), []);
  const goDm = useCallback(() => setTab("dm"), []);
  const goGraph = useCallback(() => setTab("graph"), []);

  return (
    <div style={{ height: "100vh", display: "flex", overflow: "hidden", position: "relative" }}>
      {screen === "onboarding" && (
        <Onboarding
          topics={topics}
          onToggleTopic={toggleTopic}
          worldTime={worldTime}
          onEnter={enterWorld}
        />
      )}
      {screen === "curating" && <Curating step={curateStep} worldTime={worldTime} />}

      <Sidebar
        tab={tab}
        onSelectTab={setTab}
        dmBadge={dmTyping ? "1" : ""}
        hiddenUnlocked={hiddenUnlocked}
        worldTime={worldTime}
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
            likedLive={likedLive}
            onLikeLive={likeLivePost}
            showCoach={screen === "app" && !coachDismissed}
            onDismissCoach={() => setCoachDismissed(true)}
            streaming={screen === "app" && !streamDone}
            streamText={streamText}
            streamDone={streamDone}
            minjiLiked={minjiLiked}
            minjiLikes={minjiLikes}
            onLikeMinji={likeMinji}
            minjiCommentCount={14 + minjiComments.length}
            minjiComments={minjiComments}
            minjiTyping={minjiTyping}
            commentDraft={commentDraft}
            onCommentDraftChange={setCommentDraft}
            onSendComment={sendComment}
            chulsuLiked={chulsuLiked}
            chulsuLikes={chulsuLikes}
            onLikeChulsu={likeChulsu}
            goProfile={goProfile}
            goDm={goDm}
            goGraph={goGraph}
          />
        )}
        {tab === "profile" && (
          <ProfileTab
            following={followMinji}
            onToggleFollow={() => setFollowMinji((f) => !f)}
            goDm={goDm}
            profile={minjiProfile.profile}
          />
        )}
        {tab === "dm" && (
          <DmTab
            worldTime={worldTime}
            messages={dmMsgs}
            typing={dmTyping}
            draft={dmDraft}
            onDraftChange={setDmDraft}
            onSend={sendDm}
          />
        )}
        {tab === "hidden" && <HiddenTab />}
        {tab === "graph" && (
          <GraphTab
            sel={graphSel}
            onSelect={setGraphSel}
            liveEdges={relGraph.edges}
            liveAvailable={relGraph.available}
          />
        )}
      </div>

      <Toasts toasts={toasts} onClose={closeToast} />
    </div>
  );
}
