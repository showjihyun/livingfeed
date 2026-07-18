"use client";

/**
 * World Feed 검색 데이터 계층 — feed-api GET /search (OS 인덱스).
 *
 * 화면의 라이브 목록은 흐르는 창일 뿐이다 — 검색은 세계의 전 역사를 뒤진다.
 * 작성자 이름 검색은 여기서 역해석한다: 인덱스는 actor_id만 알므로, 호출측이
 * 로스터(read.actors)에서 이름이 닿는 id들을 골라 함께 보낸다.
 * 미가용이면 null — 화면은 "검색이 닿지 않아요" 폴백 (조용한 강등 규약).
 */

import { useEffect, useState } from "react";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

/** 입력이 멎고 이만큼 지나면 질의한다 — 타자마다 서버를 두드리지 않는다 */
const DEBOUNCE_MS = 300;
const SEARCH_LIMIT = 20;

/** feed-api /search 아이템 — OS 포스트 doc의 관심 필드만 */
interface SearchDoc {
  event_id: string;
  actor_id: string | null;
  title: string | null;
  body: string | null;
  correlation_id: string | null;
  occurred_at: string;
  tick: number;
}

export interface WorldSearchResult {
  eventId: string;
  /** 작성자 — 세계 사건(Director incident)은 작성자가 없다 */
  authorId: string | null;
  title: string;
  body: string;
  /** 이야기 따라가기 좌표 — 없으면 사슬 화면이 열리지 않는다 */
  correlationId: string | null;
  occurredAt: string;
  tick: number;
}

export type WorldSearchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; results: WorldSearchResult[] }
  | { status: "offline" };

async function fetchSearch(
  query: string, actorIds: string[],
): Promise<WorldSearchResult[] | null> {
  const params = new URLSearchParams({ q: query, world_id: WORLD_ID });
  params.set("limit", String(SEARCH_LIMIT));
  for (const id of actorIds) params.append("actor", id);
  try {
    const response = await fetch(`${FEED_API_URL}/search?${params}`);
    if (!response.ok) return null;
    const data = (await response.json()) as { items: SearchDoc[] };
    return data.items.map((d) => ({
      eventId: d.event_id,
      authorId: d.actor_id,
      title: d.title ?? "",
      body: d.body ?? "",
      correlationId: d.correlation_id,
      occurredAt: d.occurred_at,
      tick: d.tick,
    }));
  } catch {
    return null;
  }
}

/** 검색 훅 — 디바운스 후 질의, 검색어가 비면 idle로 돌아간다 */
export function useWorldSearch(
  query: string, matchActorIds: (q: string) => string[],
): WorldSearchState {
  const [state, setState] = useState<WorldSearchState>({ status: "idle" });

  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) {
      setState({ status: "idle" });
      return;
    }
    setState({ status: "loading" });
    let cancelled = false;
    const timer = setTimeout(() => {
      void fetchSearch(trimmed, matchActorIds(trimmed)).then((results) => {
        if (cancelled) return;
        setState(results === null ? { status: "offline" } : { status: "ready", results });
      });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, matchActorIds]);

  return state;
}
