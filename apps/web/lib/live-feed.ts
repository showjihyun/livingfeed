"use client";

/**
 * 라이브 피드 데이터 계층 — 실 백엔드 계약 배선 (ADR-010/014).
 *
 * 초기 목록: feed-api GET /feed (랭킹 첫 화면, 평탄화된 색인 문서)
 * 라이브:    TAL subscribe() — SSE, 봉투(EventEnvelope) 수신. 재접속은 TAL 책임.
 * 백엔드 미가용이면 status="offline" — 화면은 데모 시나리오만 보여준다.
 */

import { useEffect, useState } from "react";

import { createTransport } from "@livingfeed/api-client";
import type { EventEnvelope, FeedPostPublished } from "@livingfeed/schemas";

const GATEWAY_URL = process.env.NEXT_PUBLIC_LF_GATEWAY_URL ?? "http://localhost:8000";
const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const MAX_POSTS = 50;

export type LiveStatus = "connecting" | "live" | "offline";

export interface LivePost {
  /** post id = 봉투 event_id (ULID) — 커서와 같은 좌표계 */
  id: string;
  title: string;
  body: string;
  authorId: string;
  occurredAt: string;
  dramaScore: number;
  tags: string[];
}

/** feed-api 응답 아이템 — os-projector가 평탄화한 색인 문서 */
interface FeedDoc {
  event_id: string;
  actor_id: string;
  occurred_at: string;
  title: string;
  body: string;
  drama_score: number;
  tags: string[];
}

function fromDoc(doc: FeedDoc): LivePost {
  return {
    id: doc.event_id,
    title: doc.title,
    body: doc.body,
    authorId: doc.actor_id,
    occurredAt: doc.occurred_at,
    dramaScore: doc.drama_score,
    tags: doc.tags,
  };
}

function fromEnvelope(envelope: EventEnvelope): LivePost {
  const p = envelope.payload as unknown as FeedPostPublished;
  return {
    id: envelope.event_id,
    title: p.title,
    body: p.body,
    authorId: envelope.actor_id ?? p.participants[0] ?? "?",
    occurredAt: envelope.occurred_at,
    dramaScore: p.drama_score,
    tags: p.tags,
  };
}

function mergePosts(prev: LivePost[], incoming: LivePost[]): LivePost[] {
  const byId = new Map(prev.map((p) => [p.id, p]));
  for (const post of incoming) byId.set(post.id, post);
  // ULID = 시간순 정렬 가능 — 최신이 위로
  return [...byId.values()].sort((a, b) => b.id.localeCompare(a.id)).slice(0, MAX_POSTS);
}

export function relativeTime(iso: string, now = Date.now()): string {
  const diffS = Math.max(0, Math.floor((now - Date.parse(iso)) / 1000));
  if (diffS < 60) return "방금";
  if (diffS < 3600) return `${Math.floor(diffS / 60)}분 전`;
  if (diffS < 86400) return `${Math.floor(diffS / 3600)}시간 전`;
  return `${Math.floor(diffS / 86400)}일 전`;
}

export function useLiveFeed(enabled: boolean): { posts: LivePost[]; status: LiveStatus } {
  const [posts, setPosts] = useState<LivePost[]>([]);
  const [status, setStatus] = useState<LiveStatus>("connecting");

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const add = (incoming: LivePost[]) => {
      if (!cancelled) setPosts((prev) => mergePosts(prev, incoming));
    };

    // SSE를 먼저 열고(신규분) 초기 목록을 나중에 합친다 — 겹침은 id 병합이 흡수,
    // 순서를 바꾸면 fetch와 구독 사이의 이벤트가 유실될 수 있다
    const subscription = createTransport({ baseUrl: GATEWAY_URL }).subscribe({
      feeds: ["world"],
      onItem: (envelope) => {
        setStatus("live");
        add([fromEnvelope(envelope)]);
      },
      onError: () => {
        // 재접속은 TAL(EventSource)이 알아서 한다 — 표시만 강등
        setStatus((s) => (s === "live" ? "connecting" : "offline"));
      },
    });

    void (async () => {
      try {
        const response = await fetch(`${FEED_API_URL}/feed?types=world&limit=20`);
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as { items: FeedDoc[] };
        add(body.items.map(fromDoc));
        if (!cancelled) setStatus("live");
      } catch {
        if (!cancelled) setStatus((s) => (s === "live" ? s : "offline"));
      }
    })();

    return () => {
      cancelled = true;
      subscription.close();
    };
  }, [enabled]);

  return { posts, status };
}
