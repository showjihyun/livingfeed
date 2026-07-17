"use client";

/**
 * 이야기 사슬 데이터 계층 — feed-api GET /story/{correlation_id} (plan/03 §단계 3→4).
 *
 * 내 개입에서 시작된 사건 연쇄를 "이 이야기의 시작점" 형태로 따라간다 —
 * 실세의 보상은 저자성이다. 미가용이면 null — 화면은 어포던스를 숨긴다
 * (라이브 피드·프로필과 같은 실측/폴백 규약, 하드코딩 서사 없음).
 */

import { useEffect, useState } from "react";

import { PLAYER_ID } from "./config";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

export interface StoryItem {
  eventId: string;
  type: string;
  tick: number;
  occurredAt: string;
  /** 표시 이름 — BE가 read.actors로 해석. 요청자 본인은 "당신" (식별자 비노출) */
  actor: string;
  /** 타입별 한글 한 줄 (BE summarize 규칙) */
  summary: string;
}

export interface StoryTimeline {
  items: StoryItem[];
  /** 사슬의 첫 항목 — 이 이야기의 시작점 */
  origin: StoryItem | null;
  /** 시작점이 나의 개입인가 — "이 드라마의 원작자가 나" (plan/03) */
  startedByYou: boolean;
}

interface StoryItemRow {
  event_id: string;
  type: string;
  tick: number;
  occurred_at: string;
  actor: string;
  summary: string;
}

interface StoryResponse {
  items: StoryItemRow[];
  origin: StoryItemRow | null;
  started_by_you: boolean;
}

function toItem(row: StoryItemRow): StoryItem {
  return {
    eventId: row.event_id,
    type: row.type,
    tick: row.tick,
    occurredAt: row.occurred_at,
    actor: row.actor,
    summary: row.summary,
  };
}

// 카드마다 사슬을 실측한다 — 같은 correlation의 재조회를 막는 모듈 캐시.
// 캐시는 성공분만 담는다: 사슬은 자라지만(새 사건), 한 세션의 카드 렌더에서는
// 마운트 시점 스냅샷이면 충분하다 (실패는 담지 않아 다음 마운트에 재시도).
const storyCache = new Map<string, Promise<StoryTimeline | null>>();

export function fetchStory(correlationId: string): Promise<StoryTimeline | null> {
  const cached = storyCache.get(correlationId);
  if (cached) return cached;
  const promise = (async (): Promise<StoryTimeline | null> => {
    try {
      const response = await fetch(
        `${FEED_API_URL}/story/${correlationId}?world_id=${WORLD_ID}&player_id=${PLAYER_ID}`,
      );
      if (!response.ok) throw new Error(`feed-api ${response.status}`);
      const body = (await response.json()) as StoryResponse;
      return {
        items: body.items.map(toItem),
        origin: body.origin ? toItem(body.origin) : null,
        startedByYou: body.started_by_you,
      };
    } catch {
      // 미가용 — 어포던스를 숨긴다 (조용한 강등, 라이브 피드와 같은 규약)
      storyCache.delete(correlationId);
      return null;
    }
  })();
  storyCache.set(correlationId, promise);
  return promise;
}

/** 카드 하나의 사슬 실측 — 미가용이면 null (화면은 어포던스·배지를 숨긴다) */
export function useStory(correlationId: string | undefined): StoryTimeline | null {
  const [story, setStory] = useState<StoryTimeline | null>(null);

  useEffect(() => {
    if (!correlationId) return;
    let cancelled = false;
    void fetchStory(correlationId).then((timeline) => {
      if (!cancelled) setStory(timeline);
    });
    return () => {
      cancelled = true;
    };
  }, [correlationId]);

  return story;
}
