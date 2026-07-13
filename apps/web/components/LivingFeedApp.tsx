"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { TOAST_DURATION_MS, WORLD_MIN_START, formatWorldTime } from "@/lib/data";
import { useActorDirectory } from "@/lib/actors";
import { FOCUS_ACTOR_ID } from "@/lib/config";
import { useRelationshipGraph } from "@/lib/graph";
import type { LivePost } from "@/lib/live-feed";
import { useLiveFeed } from "@/lib/live-feed";
import { fetchDmHistory } from "@/lib/messages";
import { useActorProfile } from "@/lib/profile";
import { naturalDelayMs, useActorSession } from "@/lib/session";
import type { DmMessage, FeedComment, RelKey, Screen, Tab, Toast } from "@/lib/types";

import { Curating } from "./Curating";
import { DmTab } from "./DmTab";
import { FeedTab } from "./FeedTab";
import { GraphTab } from "./GraphTab";
import { HiddenTab } from "./HiddenTab";
import { Onboarding } from "./Onboarding";
import { ProfileTab } from "./ProfileTab";
import { Sidebar } from "./Sidebar";
import { Toasts } from "./Toasts";

const ME_COMMENT = { bg: "#EDF3FD", avatarBg: "#D9E2F2" };
const ACTOR_COMMENT = { bg: "#F8FAFD", avatarBg: "#AFC8F5" };

export function LivingFeedApp() {
  const [screen, setScreen] = useState<Screen>("onboarding");
  const [tab, setTab] = useState<Tab>("feed");
  const [topics, setTopics] = useState<string[]>(["직장 드라마"]);
  const [worldMin, setWorldMin] = useState(WORLD_MIN_START);
  const [curateStep, setCurateStep] = useState(0);

  const [dmMsgs, setDmMsgs] = useState<DmMessage[]>([]);
  const [dmDraft, setDmDraft] = useState("");
  const [dmTyping, setDmTyping] = useState(false);

  // 라이브 포스트별 댓글 — 댓글은 특정 데모 카드가 아니라 실제 포스트에 붙는다
  const [commentsByPost, setCommentsByPost] = useState<Record<string, FeedComment[]>>({});
  const [typingPosts, setTypingPosts] = useState<ReadonlySet<string>>(new Set());

  const [toasts, setToasts] = useState<Toast[]>([]);
  const [graphSel, setGraphSel] = useState<RelKey>("mc");
  const [following, setFollowing] = useState(true);
  const [interventions, setInterventions] = useState(0);
  const [coachDismissed, setCoachDismissed] = useState(false);
  const [hiddenUnlocked] = useState(false);

  const toastSeq = useRef(0);
  const timers = useRef<number[]>([]);

  const after = useCallback((ms: number, fn: () => void) => {
    timers.current.push(window.setTimeout(fn, ms));
  }, []);

  useEffect(() => {
    // 세계 시간: 3초마다 +4분 (현실 4배속)
    const clock = window.setInterval(() => setWorldMin((m) => m + 4), 3000);
    const pending = timers.current;
    return () => {
      window.clearInterval(clock);
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

  // 실 백엔드 라이브 피드 — 세계 입장 후에만 구독 (미가용이면 오프라인 상태만)
  const { posts: livePosts, status: liveStatus } = useLiveFeed(screen === "app");

  // 관계 그래프 실측 (kuzu-projector, ADR-006) — 미가용이면 데모 배치 유지
  const relGraph = useRelationshipGraph(screen === "app");

  // 액터 명단(read.actors) — 표시 이름을 여기서 읽는다 (하드코딩 금지, ADR-012)
  const { byId } = useActorDirectory(screen === "app");
  const focusName = byId.get(FOCUS_ACTOR_ID)?.name ?? "상대";
  const authorName = useCallback(
    (actorId: string) => byId.get(actorId)?.name ?? actorId,
    [byId],
  );

  // 액터의 내면 실측 (pg-projector 신념·에피소드, ADR-003/008) — 미가용이면 데모 서사
  const focusProfile = useActorProfile(FOCUS_ACTOR_ID, screen === "app");

  // 지난 대화 이어받기 (read.messages) — 사용자가 아직 아무 말도 안 했을 때만 채운다
  useEffect(() => {
    if (screen !== "app") return;
    let cancelled = false;
    void fetchDmHistory(FOCUS_ACTOR_ID).then((history) => {
      if (cancelled || !history) return;
      setDmMsgs((current) => (current.length === 0 ? history : current));
    });
    return () => {
      cancelled = true;
    };
  }, [screen]);

  // 상호작용 세션 (WS) — DM/댓글/좋아요를 실세계에 꽂는다. 미가용이면 데모 폴백.
  const session = useActorSession({
    enabled: screen === "app",
    onReply: (reply) => {
      // 도착 즉시 렌더하지 않는다 — 1~3초 '타이핑'을 거쳐야 사람 같다
      if (reply.channel === "dm") {
        setDmTyping(true);
        after(naturalDelayMs(), () => {
          setDmTyping(false);
          setDmMsgs((list) => [...list, { from: "actor", text: reply.text }]);
        });
      } else if (reply.channel === "comment" && reply.postId) {
        const postId = reply.postId;
        after(naturalDelayMs(), () => {
          setTypingPosts((prev) => {
            const next = new Set(prev);
            next.delete(postId);
            return next;
          });
          setCommentsByPost((prev) => ({
            ...prev,
            [postId]: [
              ...(prev[postId] ?? []),
              { author: authorName(reply.actorId), text: reply.text, ...ACTOR_COMMENT },
            ],
          }));
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
      setCommentsByPost((prev) => ({
        ...prev,
        [post.id]: [
          ...(prev[post.id] ?? []),
          { author: "관찰자_0417 (나)", text: trimmed, ...ME_COMMENT },
        ],
      }));
      setInterventions((n) => n + 1);
      setCoachDismissed(true);
      // 실세계 경로 — player.comment.posted 적재, 응답은 onReply(channel=comment)로 온다.
      // 오프라인이면 답장은 오지 않는다 (세계가 살아있을 때만 반응이 있다).
      if (session.sendComment(post.authorId, post.id, trimmed)) {
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
    const text = dmDraft.trim();
    if (!text || dmTyping) return;
    setDmMsgs((list) => [...list, { from: "me", text }]);
    setDmDraft("");
    setInterventions((n) => n + 1);
    // 실세계 경로 — player.dm.sent 적재, 액터의 응답은 다음 tick에 push로 온다.
    // 오프라인이면 답장은 오지 않는다 (세계가 살아있을 때만 반응이 있다).
    if (session.sendDm(FOCUS_ACTOR_ID, text)) setDmTyping(true);
  }, [dmDraft, dmTyping, session]);

  const closeToast = useCallback((id: number) => {
    setToasts((list) => list.filter((x) => x.id !== id));
  }, []);

  const worldTime = formatWorldTime(worldMin);
  const goDm = useCallback(() => setTab("dm"), []);

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
            commentsByPost={commentsByPost}
            typingPosts={typingPosts}
            onComment={commentOnPost}
            authorName={authorName}
            showCoach={screen === "app" && !coachDismissed}
            onDismissCoach={() => setCoachDismissed(true)}
          />
        )}
        {tab === "profile" && (
          <ProfileTab
            following={following}
            onToggleFollow={() => setFollowing((f) => !f)}
            goDm={goDm}
            profile={focusProfile.profile}
          />
        )}
        {tab === "dm" && (
          <DmTab
            worldTime={worldTime}
            partnerName={focusName}
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
