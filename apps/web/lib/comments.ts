"use client";

/**
 * 댓글 스레드 데이터 계층 — PG read.messages 실배선 (ADR-003 pg-projector).
 *
 * WS 세션(session.ts)은 실시간 답글만 나른다 — 지난 댓글(내 개입의 흔적과
 * 액터들의 소셜 루프)은 이 경로로 되읽는다. 새로고침에도 댓글이 남는 것이
 * 도파민 루프(개입→반응 확인)의 전제다.
 * 응답은 시간 오름차순(reads.py post_comments 계약) — 그대로 그리면 스레드다.
 * 백엔드 미가용이면 null — 호출측은 라이브 분만 유지한다 (조용한 강등).
 */

import { PLAYER_ID } from "./config";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

/** 한 포스트의 댓글 상한 — feed-api max_limit과 같은 좌표 */
const COMMENTS_LIMIT = 100;

/** feed-api GET /posts/{id}/comments 아이템 — read.messages 행 (시간순) */
interface CommentDoc {
  event_id: string;
  author_kind: "player" | "actor";
  author_id: string;
  /** 액터 저자의 표시 이름 (read.actors 그라운딩) — 플레이어·미선언 액터는 null */
  author_name: string | null;
  text: string;
  in_reply_to: string | null;
}

/** 포스트에 달린 댓글 하나 — 표시 문장은 호출측이 이름으로 그라운딩한다 */
export interface PostComment {
  eventId: string;
  authorKind: "player" | "actor";
  authorId: string;
  authorName: string | null;
  text: string;
  /** 이 관찰자(나)의 댓글 — 자기표시('나')와 말풍선 색의 근거 */
  isMine: boolean;
  /** 다른 댓글에 대한 답장 (in_reply_to ≠ post_id) — 스레드 들여쓰기 근거 */
  isReply: boolean;
}

export async function fetchPostComments(postId: string): Promise<PostComment[] | null> {
  try {
    const response = await fetch(
      `${FEED_API_URL}/posts/${postId}/comments?world_id=${WORLD_ID}&limit=${COMMENTS_LIMIT}`,
    );
    if (!response.ok) throw new Error(`feed-api ${response.status}`);
    const body = (await response.json()) as { items: CommentDoc[] };
    return body.items.map((c) => ({
      eventId: c.event_id,
      authorKind: c.author_kind,
      authorId: c.author_id,
      authorName: c.author_name,
      text: c.text,
      isMine: c.author_kind === "player" && c.author_id === PLAYER_ID,
      isReply: c.in_reply_to !== null && c.in_reply_to !== postId,
    }));
  } catch {
    return null;
  }
}
