"use client";

/**
 * DM 히스토리 데이터 계층 — PG read.messages 실배선 (ADR-003 pg-projector).
 *
 * WS 세션(session.ts)은 실시간 답장만 나른다 — 지난 대화는 이 경로로
 * 이어받는다 (WS 재접속·새로고침에도 대화가 사라지지 않는다).
 * `cursor`(event id ULID)로 더 과거 페이지를 이어받는다 — 응답은 시간 역순,
 * next_cursor는 페이지 마지막 event id다 (reads.py conversation 계약).
 * 백엔드 미가용이면 null — 호출측은 현재 화면을 유지한다 (조용한 강등).
 */

import { PLAYER_ID } from "./config";
import type { DmMessage } from "./types";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";

/** 한 페이지 크기 — 이보다 덜 오면 더 과거가 없다는 뜻 (커서를 닫는 근거) */
const DM_PAGE_SIZE = 50;

/** feed-api GET /messages 아이템 — read.messages 행 (시간 역순) */
interface MessageDoc {
  event_id: string;
  sender: "player" | "actor";
  channel: "dm" | "comment";
  text: string;
}

/** 대화 한 페이지 — 시간 오름차순 메시지 + 더 과거로 가는 커서 (끝이면 null) */
export interface DmHistoryPage {
  messages: DmMessage[];
  nextCursor: string | null;
}

export async function fetchDmHistory(
  actorId: string,
  cursor?: string,
): Promise<DmHistoryPage | null> {
  try {
    const cursorParam = cursor ? `&cursor=${cursor}` : "";
    const response = await fetch(
      `${FEED_API_URL}/messages?player_id=${PLAYER_ID}&actor_id=${actorId}&limit=${DM_PAGE_SIZE}${cursorParam}`,
    );
    if (!response.ok) throw new Error(`feed-api ${response.status}`);
    const body = (await response.json()) as {
      items: MessageDoc[];
      next_cursor: string | null;
    };
    // 응답은 최신부터 — 채팅은 과거부터 그린다 (dm만, 댓글 채널은 피드 쪽 몫)
    const messages: DmMessage[] = body.items
      .filter((m) => m.channel === "dm")
      .reverse()
      .map((m) => ({
        from: m.sender === "player" ? "me" : "actor",
        text: m.text,
        eventId: m.event_id,
      }));
    // next_cursor는 마지막 페이지에서도 남는다 — 페이지가 덜 찼으면 여기서 닫는다
    const nextCursor = body.items.length < DM_PAGE_SIZE ? null : body.next_cursor;
    return { messages, nextCursor };
  } catch {
    return null;
  }
}
