"use client";

/**
 * 상호작용 세션 데이터 계층 — WS /session 실배선 (ADR-010/012).
 *
 * DM·댓글·좋아요를 실제 세계에 꽂는다: 커맨드는 player.* 이벤트로 적재되고
 * 액터의 응답(actor.message.sent)이 이 세션으로 push된다.
 * 백엔드 미가용이면 status="offline" — 호출측은 데모 시나리오로 폴백한다.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { createTransport } from "@livingfeed/api-client";
import type { SessionHandle } from "@livingfeed/api-client";
import type { ActorMessageSent } from "@livingfeed/schemas";

import { PLAYER_ID } from "./config";

const GATEWAY_URL = process.env.NEXT_PUBLIC_LF_GATEWAY_URL ?? "http://localhost:8000";

/** 응답·댓글 표시 전의 '생각하고 타이핑하는' 시간 — 즉답은 기계 티가 난다 */
export function naturalDelayMs(): number {
  return 1000 + Math.random() * 2000; // 1~3초
}

export type SessionStatus = "connecting" | "live" | "offline";

export interface ActorReply {
  channel: "comment" | "dm";
  actorId: string;
  text: string;
  postId: string | null;
  /** 봉투 event_id — 되읽은 댓글(read.messages)과의 중복 제거 키 */
  eventId: string;
}

export function useActorSession(opts: {
  enabled: boolean;
  onReply: (reply: ActorReply) => void;
}): {
  status: SessionStatus;
  sendDm: (targetActorId: string, text: string) => boolean;
  sendComment: (targetActorId: string, postId: string, text: string) => boolean;
  addReaction: (targetActorId: string, postId: string) => boolean;
  setFollow: (targetActorId: string, following: boolean) => boolean;
} {
  const [status, setStatus] = useState<SessionStatus>("connecting");
  const handleRef = useRef<SessionHandle | null>(null);
  const liveRef = useRef(false);
  const onReplyRef = useRef(opts.onReply);
  onReplyRef.current = opts.onReply;

  useEffect(() => {
    if (!opts.enabled) return;
    let sawOpen = false;
    const session = createTransport({ baseUrl: GATEWAY_URL }).openSession({
      playerId: PLAYER_ID,
      onReply: (envelope) => {
        const p = envelope.payload as unknown as ActorMessageSent;
        onReplyRef.current({
          channel: p.channel,
          actorId: envelope.actor_id ?? "",
          text: p.text,
          postId: p.post_id,
          eventId: envelope.event_id,
        });
      },
      onStatus: (s) => {
        if (s === "open") {
          sawOpen = true;
          liveRef.current = true;
          setStatus("live");
        } else if (s === "connecting") {
          liveRef.current = false;
          // 한 번도 못 붙었으면 offline 취급 — 데모 폴백이 이어받는다
          setStatus(sawOpen ? "connecting" : "offline");
        }
      },
      onCommandError: (seq, message) => {
        console.warn(`세션 커맨드 거부(seq=${seq}): ${message}`);
      },
    });
    handleRef.current = session;
    return () => {
      liveRef.current = false;
      handleRef.current = null;
      session.close();
    };
  }, [opts.enabled]);

  // 반환값 true = 실세계로 전송됨, false = 오프라인 (호출측이 데모로 폴백)
  const sendDm = useCallback((targetActorId: string, text: string) => {
    if (!liveRef.current || !handleRef.current) return false;
    handleRef.current.sendDm(targetActorId, text);
    return true;
  }, []);
  const sendComment = useCallback((targetActorId: string, postId: string, text: string) => {
    if (!liveRef.current || !handleRef.current) return false;
    handleRef.current.sendComment(targetActorId, postId, text);
    return true;
  }, []);
  const addReaction = useCallback((targetActorId: string, postId: string) => {
    if (!liveRef.current || !handleRef.current) return false;
    handleRef.current.addReaction(targetActorId, postId);
    return true;
  }, []);
  const setFollow = useCallback((targetActorId: string, following: boolean) => {
    if (!liveRef.current || !handleRef.current) return false;
    handleRef.current.setFollow(targetActorId, following);
    return true;
  }, []);

  return { status, sendDm, sendComment, addReaction, setFollow };
}
