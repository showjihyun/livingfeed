"use client";

/**
 * DM 히스토리 데이터 계층 — PG read.messages 실배선 (ADR-003 pg-projector).
 *
 * WS 세션(session.ts)은 실시간 답장만 나른다 — 지난 대화는 이 경로로
 * 이어받는다 (WS 재접속·새로고침에도 대화가 사라지지 않는다).
 * `cursor`(event id ULID)로 더 과거 페이지를 이어받는다 — 응답은 시간 역순,
 * next_cursor는 페이지 마지막 event id다 (reads.py conversation 계약).
 * 인박스 목록은 /messages/threads — 액터별 마지막 메시지 1건 (reads.py threads 계약).
 * 백엔드 미가용이면 null — 호출측은 현재 화면을 유지한다 (조용한 강등).
 */

import { authHeaders, PLAYER_ID } from "./config";
import type { DmMessage } from "./types";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

/** 한 페이지 크기 — 이보다 덜 오면 더 과거가 없다는 뜻 (커서를 닫는 근거) */
const DM_PAGE_SIZE = 50;

/** 인박스 목록 크기 — 대화 상대(스레드) 수의 상한 */
const THREADS_LIMIT = 50;

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
      `${FEED_API_URL}/messages?world_id=${WORLD_ID}&player_id=${PLAYER_ID}&actor_id=${actorId}&limit=${DM_PAGE_SIZE}${cursorParam}`,
      { headers: authHeaders() },
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

/** feed-api GET /messages/threads 아이템 — 액터별 마지막 메시지 1건 (최신 대화 순) */
interface ThreadDoc {
  actor_id: string;
  last_text: string;
  last_channel: "dm" | "comment";
  last_at: string;
  last_event_id: string;
  last_tick: number;
  last_from_actor: boolean;
}

/** 인박스의 스레드 한 줄 — 대화 상대와 마지막 메시지 (표시 이름은 디렉터리 몫) */
export interface DmThread {
  actorId: string;
  lastText: string;
  lastChannel: "dm" | "comment";
  /** ISO 시각 — 로컬 upsert(방금 보낸/받은 메시지)도 이 좌표를 쓴다 */
  lastAt: string;
  /**
   * 마지막 메시지의 세계 tick (read.messages) — 조회 범위(lib/range) 하한 비교
   * 좌표. 로컬 upsert(방금 오간 말)는 undefined — "지금"이므로 어느 범위에도 든다.
   */
  lastTick?: number;
  lastFromActor: boolean;
}

/**
 * 이 플레이어의 대화 상대 목록 — 액터별 마지막 메시지 1건, 최신 대화 순.
 * 선제 DM(액터가 먼저 건 말)도 그저 스레드의 첫 마디로 실린다.
 * 백엔드 미가용이면 null — 호출측은 빈 인박스 규약을 유지한다 (조용한 강등).
 */
export async function fetchDmThreads(): Promise<DmThread[] | null> {
  try {
    const response = await fetch(
      `${FEED_API_URL}/messages/threads?world_id=${WORLD_ID}&player_id=${PLAYER_ID}&limit=${THREADS_LIMIT}`,
      { headers: authHeaders() },
    );
    if (!response.ok) throw new Error(`feed-api ${response.status}`);
    const body = (await response.json()) as { threads: ThreadDoc[] };
    return body.threads.map((t) => ({
      actorId: t.actor_id,
      lastText: t.last_text,
      lastChannel: t.last_channel,
      lastAt: t.last_at,
      lastTick: t.last_tick,
      lastFromActor: t.last_from_actor,
    }));
  } catch {
    return null;
  }
}
