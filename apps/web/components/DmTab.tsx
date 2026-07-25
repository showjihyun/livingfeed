"use client";

import { memo, useEffect, useState } from "react";
import type { KeyboardEvent } from "react";

import { ICON } from "@/lib/data";
import { useMessages, type Locale } from "@/lib/i18n";
import { relativeTime } from "@/lib/live-feed";
import type { DmThread } from "@/lib/messages";
import { disablePush, enablePush, getPushState } from "@/lib/push";
import type { PushState } from "@/lib/push";
import type { Range } from "@/lib/range";
import type { DmMessage } from "@/lib/types";
import { useWorldClock } from "@/lib/world-clock";
import { useDemoWorldTime } from "@/lib/world-clock-display";
import { COLOR, WEIGHT, RADIUS } from "@/lib/tokens";

import { Face } from "./Face";
import { Icon } from "./Icon";
import { Pressable } from "./Pressable";
import { RangeChips } from "./RangeChips";
import styles from "./lf.module.css";

const AVATAR_COLORS = [COLOR.accent, "#F2B8CF", "#BFE3CF", "#E8D5A8", "#CBBDE8", "#A8D8E8"];

function avatarColor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

const en = {
  inbox: "Inbox",
  threadCount: (n: number) => (n === 1 ? "1 conversation" : `${n} conversations`),
  emptyRange: "Nothing was said in this window — widen the range to see earlier conversations.",
  emptyInboxTitle: "No messages yet.",
  emptyInboxBody: "Say hello first — actors who remember you sometimes reach out on their own.",
  typing: "Typing…",
  emptyThreadPreview: "No words exchanged yet — say hello first",
  mePrefix: "Me: ",
  backToInbox: "Back to inbox",
  slowReplies: "Replies may take a while",
  loadingOlder: "Loading earlier messages…",
  loadOlder: "Load earlier messages",
  todayWorldTime: (time: string) => `Today · world time ${time}`,
  partnerTyping: (name: string) => `${name} is typing…`,
  replyPlaceholder: (name: string) => `Reply to ${name}…`,
  send: "Send",
  pushDenied: "Notifications are turned off in your browser settings",
  pushOn: "Turn off new-reply notifications",
  pushOff: "Get notified about replies that arrive while you're away",
};
const M: Record<Locale, typeof en> = {
  en,
  ko: {
    inbox: "받은 것",
    threadCount: (n) => `대화 ${n}개`,
    emptyRange: "이 기간엔 오간 말이 없어요 — 범위를 넓히면 지난 대화가 보여요.",
    emptyInboxTitle: "아직 받은 메시지가 없어요.",
    emptyInboxBody: "먼저 말을 걸어보세요 — 당신을 기억한 액터가 먼저 연락해오기도 합니다.",
    typing: "입력 중...",
    emptyThreadPreview: "아직 나눈 말이 없어요 — 먼저 말을 걸어보세요",
    mePrefix: "나: ",
    backToInbox: "목록으로 돌아가기",
    slowReplies: "답장이 느릴 수 있어요",
    loadingOlder: "이전 대화 불러오는 중...",
    loadOlder: "이전 대화 더 보기",
    todayWorldTime: (time) => `오늘 · 세계 시간 ${time}`,
    partnerTyping: (name) => `${name}가 입력 중...`,
    replyPlaceholder: (name) => `${name}에게 답장하기...`,
    send: "보내기",
    pushDenied: "브라우저 설정에서 알림 권한이 꺼져 있어요",
    pushOn: "새 답장 알림 끄기",
    pushOff: "자리를 비운 사이 도착한 답장을 알림으로 받기",
  },
};

interface DmTabProps {
  /** 인박스 스레드 목록 (최신 대화 순) — 표시 이름은 nameOf로 푼다 (하드코딩 금지) */
  threads: DmThread[];
  /** 실측 스레드가 하나도 없는가 — 빈 인박스 안내(데모 인트로 규약)를 띄우는 근거 */
  emptyInbox: boolean;
  /** 조회 범위(세계 시간) — 인박스 목록에만 적용, 열린 대화 뷰는 대화 전체다 */
  range: Range;
  onRangeChange: (range: Range) => void;
  /** actor_id → 표시 이름 (라이브 디렉터리, 모르면 '누군가' 폴백) */
  nameOf: (actorId: string) => string;
  /** 아직 열어보지 않은 새 DM이 있는 스레드들 */
  unread: ReadonlySet<string>;
  /** 지금 입력 중인 액터들 — 목록에서는 '입력 중...', 열린 스레드에선 말풍선 */
  typingActors: ReadonlySet<string>;
  /** 열린 스레드의 상대 — null이면 목록 뷰 */
  openActorId: string | null;
  messages: DmMessage[];
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: () => void;
  /** 더 과거 대화 페이지가 남아있는가 — 커서 소진·백엔드 미가용이면 숨긴다 */
  canLoadOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  onOpenThread: (actorId: string) => void;
  onBack: () => void;
}

/**
 * 서사 푸시 벨 — 자리를 비운 사이의 답장·안부를 브라우저 알림으로 (plan/11 §D1).
 * 구독 중이면 채워진 벨, 아니면 빈 벨. 권한이 거부됐으면 비활성 + 안내 툴팁.
 * 미지원·게이트웨이 미가용이면 아예 그리지 않는다 (조용한 강등 — 다크 패턴 금지).
 */
function PushBell() {
  const t = useMessages(M);
  const [state, setState] = useState<PushState>("unsupported");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    getPushState().then((s) => {
      if (alive) setState(s);
    });
    return () => {
      alive = false;
    };
  }, []);

  if (state === "unsupported") return null;
  const on = state === "on";
  const denied = state === "denied";
  const title = denied ? t.pushDenied : on ? t.pushOn : t.pushOff;
  const toggle = async () => {
    if (busy || denied) return;
    setBusy(true);
    try {
      setState(on ? await disablePush() : await enablePush());
    } finally {
      setBusy(false);
    }
  };
  return (
    <Pressable
      onClick={toggle}
      title={title}
      aria-label={title}
      aria-pressed={on}
      disabled={denied || busy}
      className={denied ? undefined : styles.press92}
      style={{
        width: 30,
        height: 30,
        borderRadius: "50%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        cursor: denied ? "default" : "pointer",
        background: on ? COLOR.primarySoft : COLOR.surface,
        opacity: denied ? 0.45 : busy ? 0.6 : 1,
      }}
    >
      <Icon
        d={ICON.bell}
        size={15}
        color={on ? COLOR.primaryDeep : COLOR.faint}
        style={on ? { fill: "currentColor" } : undefined}
      />
    </Pressable>
  );
}

export const DmTab = memo(DmTabInner);

function DmTabInner(props: DmTabProps) {
  return props.openActorId === null ? (
    <InboxList {...props} />
  ) : (
    <Conversation {...props} openActorId={props.openActorId} />
  );
}

/** 목록 뷰 — 대화 상대별 스레드 카드 (마지막 한 줄·상대 발신이면 결 구분) */
function InboxList({
  threads,
  emptyInbox,
  range,
  onRangeChange,
  nameOf,
  unread,
  typingActors,
  onOpenThread,
}: DmTabProps) {
  const t = useMessages(M);
  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "18px 28px",
          borderBottom: "1.5px solid #EEF3FB",
        }}
      >
        <div style={{ fontSize: 20, fontWeight: WEIGHT.heavy }}>{t.inbox}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ fontSize: 13, color: COLOR.faint, fontWeight: WEIGHT.semibold }}>
            {/* 빈 인박스의 시작점 카드(데모 인트로)는 아직 대화가 아니다 */}
            {t.threadCount(emptyInbox ? 0 : threads.length)}
          </div>
          <PushBell />
        </div>
      </div>

      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "18px 20px",
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        {/* 조회 범위 — 세계 시간의 창 (목록 상단, 모든 탭 공통 자리) */}
        <RangeChips value={range} onChange={onRangeChange} />

        {!emptyInbox && threads.length === 0 && (
          // 대화는 있지만 이 기간엔 없다 — 범위의 빈 상태 (인박스의 결)
          <div
            style={{
              border: "1.5px dashed #D5DEEE",
              borderRadius: RADIUS.lg,
              padding: "26px 22px",
              textAlign: "center",
              color: COLOR.faint,
              fontSize: 13,
              fontWeight: WEIGHT.semibold,
              lineHeight: 1.7,
              background: "#FBFCFE",
              marginBottom: 6,
            }}
          >
            {t.emptyRange}
          </div>
        )}

        {emptyInbox && (
          // 빈 인박스 — 세계가 먼저 말을 걸어올 때까지의 우아한 공백 (데모 인트로 규약)
          <div
            style={{
              border: "1.5px dashed #D5DEEE",
              borderRadius: RADIUS.lg,
              padding: "26px 22px",
              textAlign: "center",
              color: COLOR.faint,
              fontSize: 13,
              fontWeight: WEIGHT.semibold,
              lineHeight: 1.7,
              background: "#FBFCFE",
              marginBottom: 6,
            }}
          >
            {t.emptyInboxTitle}
            <br />
            {t.emptyInboxBody}
          </div>
        )}

        {threads.map((thread) => {
          const typing = typingActors.has(thread.actorId);
          const isUnread = unread.has(thread.actorId);
          // 상대 발신(선제 DM·답장)은 결이 다르다 — 받은 말은 짙게, 내 말은 옅게
          const fromActor = thread.lastFromActor;
          const preview = typing ? t.typing : thread.lastText || t.emptyThreadPreview;
          return (
            <Pressable
              key={thread.actorId}
              onClick={() => onOpenThread(thread.actorId)}
              className={styles.press95}
              style={{
                width: "100%",
                textAlign: "left",
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "13px 14px",
                borderRadius: RADIUS.md,
                background: isUnread ? "#F4F8FE" : COLOR.white,
                border: `1.5px solid ${isUnread ? "#D9E5F9" : COLOR.borderSoft}`,
              }}
            >
              <div
                style={{
                  width: 42,
                  height: 42,
                  borderRadius: "50%",
                  background: avatarColor(thread.actorId),
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 16,
                  fontWeight: WEIGHT.heavy,
                  color: COLOR.ink,
                  flexShrink: 0,
                }}
              >
                {nameOf(thread.actorId).slice(0, 1)}
              </div>
              <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 3 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ fontSize: 14, fontWeight: WEIGHT.heavy, color: COLOR.ink }}>
                    {nameOf(thread.actorId)}
                  </div>
                  {thread.lastAt && (
                    <div style={{ fontSize: 11, color: COLOR.fainter, fontWeight: WEIGHT.semibold }}>
                      {relativeTime(thread.lastAt)}
                    </div>
                  )}
                </div>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: fromActor || typing ? 600 : 500,
                    color: typing ? COLOR.primaryDeep : fromActor ? COLOR.ink : COLOR.faint,
                    fontStyle: typing ? "italic" : undefined,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {!typing && !fromActor && thread.lastText ? t.mePrefix : ""}
                  {preview}
                </div>
              </div>
              {isUnread && (
                <div
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: COLOR.primary,
                    flexShrink: 0,
                  }}
                />
              )}
            </Pressable>
          );
        })}
      </div>
    </>
  );
}

/** 대화 뷰 — 기존 1:1 대화 경험 그대로, 상대만 파라미터화 (뒤로가기로 목록 복귀) */
function Conversation({
  nameOf,
  typingActors,
  openActorId,
  messages,
  draft,
  onDraftChange,
  onSend,
  canLoadOlder,
  loadingOlder,
  onLoadOlder,
  onBack,
}: DmTabProps & { openActorId: string }) {
  const t = useMessages(M);
  // 세계 시각은 공용 스토어에서 — 루트 prop 대신 (앵커 전엔 데모 폴백)
  const worldTime = useWorldClock(useDemoWorldTime());
  const partnerName = nameOf(openActorId);
  const typing = typingActors.has(openActorId);
  const partnerBg = avatarColor(openActorId);
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") onSend();
  };

  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "15px 28px",
          borderBottom: "1.5px solid #EEF3FB",
        }}
      >
        <Pressable
          onClick={onBack}
          aria-label={t.backToInbox}
          className={styles.press92}
          style={{
            width: 34,
            height: 34,
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: COLOR.surface,
            flexShrink: 0,
          }}
        >
          <Icon d={ICON.arrowLeft} size={16} color={COLOR.primaryDeep} />
        </Pressable>
        <Face preset="dmHeader38" bg={partnerBg} />
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div style={{ fontSize: 15, fontWeight: WEIGHT.heavy }}>{partnerName}</div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Icon d={ICON.moon} size={12} color={COLOR.faint} />
            <div style={{ fontSize: 12, color: COLOR.faint, fontWeight: WEIGHT.semibold }}>
              {t.slowReplies}
            </div>
          </div>
        </div>
      </div>

      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "24px 28px",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        {canLoadOlder && (
          // 과거 방향 커서 페이지네이션 — 과거 메시지를 목록 위에 이어 붙인다
          <Pressable
            onClick={onLoadOlder}
            disabled={loadingOlder}
            className={styles.press95}
            style={{
              alignSelf: "center",
              fontSize: 12,
              color: COLOR.primaryDeep,
              background: COLOR.primarySoft,
              padding: "5px 14px",
              borderRadius: RADIUS.pill,
              fontWeight: WEIGHT.bold,
              cursor: loadingOlder ? "default" : "pointer",
              opacity: loadingOlder ? 0.6 : 1,
            }}
          >
            {loadingOlder ? t.loadingOlder : t.loadOlder}
          </Pressable>
        )}

        <div
          style={{
            alignSelf: "center",
            fontSize: 12,
            color: COLOR.faint,
            background: COLOR.surface,
            padding: "5px 14px",
            borderRadius: RADIUS.pill,
            fontWeight: WEIGHT.bold,
          }}
        >
          {t.todayWorldTime(worldTime)}
        </div>

        {messages.map((msg, i) => {
          const mine = msg.from === "me";
          const next = messages[i + 1];
          const showAvatar = !mine && (i === messages.length - 1 || next?.from === "me");
          return (
            // 히스토리 메시지는 event id로 고정 — 위로 이어 붙여도 기존 말풍선이 리마운트되지 않는다
            <div
              key={msg.eventId ?? `local-${i}`}
              style={{
                display: "flex",
                gap: 10,
                alignItems: "flex-end",
                justifyContent: mine ? "flex-end" : "flex-start",
              }}
            >
              {showAvatar && <Face preset="dm30" bg={partnerBg} />}
              <div
                style={{
                  maxWidth: 440,
                  background: mine ? COLOR.primary : COLOR.surface,
                  color: mine ? COLOR.white : COLOR.ink,
                  padding: "12px 17px",
                  borderRadius: mine ? "18px 18px 4px 18px" : "18px 18px 18px 4px",
                  fontSize: 14,
                  lineHeight: 1.55,
                  fontWeight: WEIGHT.regular,
                  animation: "lf-pop 0.25s ease-out",
                }}
              >
                {msg.text}
              </div>
            </div>
          );
        })}

        {typing && (
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <div
              style={{
                display: "flex",
                gap: 4,
                alignItems: "center",
                padding: "11px 15px",
                background: COLOR.surface,
                borderRadius: "18px 18px 18px 4px",
              }}
            >
              <div
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: COLOR.muted,
                  animation: "lf-blink 1s infinite",
                }}
              />
              <div
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: "#9AA6BF",
                  animation: "lf-blink 1s 0.2s infinite",
                }}
              />
              <div
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: "#CBD5E8",
                  animation: "lf-blink 1s 0.4s infinite",
                }}
              />
            </div>
            <div style={{ fontSize: 12, color: COLOR.faint, fontWeight: WEIGHT.semibold }}>
              {t.partnerTyping(partnerName)}
            </div>
          </div>
        )}
      </div>

      <div
        style={{
          padding: "16px 28px 22px",
          borderTop: "1.5px solid #EEF3FB",
          display: "flex",
          gap: 12,
          alignItems: "center",
        }}
      >
        <input
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          onKeyDown={onKey}
          placeholder={t.replyPlaceholder(partnerName)}
          style={{
            flex: 1,
            background: COLOR.surface,
            border: "none",
            outline: "none",
            borderRadius: RADIUS.pill,
            padding: "13px 20px",
            fontSize: 14,
            color: COLOR.ink,
            fontWeight: WEIGHT.regular,
          }}
        />
        <Pressable
          onClick={onSend}
          aria-label={t.send}
          className={styles.press92}
          style={{
            width: 44,
            height: 44,
            background: COLOR.primary,
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "0 4px 12px rgba(109,141,214,0.35)",
          }}
        >
          <Icon d={ICON.send} size={18} color={COLOR.white} />
        </Pressable>
      </div>
    </>
  );
}
