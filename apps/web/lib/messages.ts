"use client";

/**
 * DM 히스토리 데이터 계층 — PG read.messages 실배선 (ADR-003 pg-projector).
 *
 * WS 세션(session.ts)은 실시간 답장만 나른다 — 지난 대화는 이 경로로
 * 이어받는다 (WS 재접속·새로고침에도 대화가 사라지지 않는다).
 * 백엔드 미가용이거나 기록이 없으면 null — 호출측은 데모 인트로를 유지한다.
 */

import { PLAYER_ID } from "./session";
import type { DmMessage } from "./types";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";

/** feed-api GET /messages 아이템 — read.messages 행 (시간 역순) */
interface MessageDoc {
  event_id: string;
  sender: "player" | "actor";
  channel: "dm" | "comment";
  text: string;
}

export async function fetchDmHistory(actorId: string): Promise<DmMessage[] | null> {
  try {
    const response = await fetch(
      `${FEED_API_URL}/messages?player_id=${PLAYER_ID}&actor_id=${actorId}&limit=50`,
    );
    if (!response.ok) throw new Error(`feed-api ${response.status}`);
    const body = (await response.json()) as { items: MessageDoc[] };
    const dm = body.items.filter((m) => m.channel === "dm");
    if (!dm.length) return null;
    // 응답은 최신부터 — 채팅은 과거부터 그린다
    return dm
      .reverse()
      .map((m) => ({ from: m.sender === "player" ? "me" : "minji", text: m.text }));
  } catch {
    return null;
  }
}
