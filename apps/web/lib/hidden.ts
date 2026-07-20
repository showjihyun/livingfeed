"use client";

/**
 * Hidden Feed 데이터 계층 — 당신에게만 닿은 비공개 이야기 (ADR-014 private 등급).
 *
 * feed-api /feed?types=private&player_id= → Redis 타임라인(redis-projector)의
 * private 엔트리. 액터가 당신에게만 보낸 것(답장·귓속말)이 여기 모인다.
 * 조회 범위(오늘/이번 주/이번 달)는 from_tick 서버 필터 — 세계일이 넘어가면
 * currentTick이 바뀌어 SWR key가 갱신되고 "오늘"의 창도 함께 넘어간다.
 * 폴링·dedup·배경탭 정지는 SWR이 맡는다(lib/swr). 미가용/빈 경우 빈 목록.
 */

import { useEffect, useState } from "react";
import useSWR from "swr";

import { PLAYER_ID } from "./config";
import { rangeTickBounds, type Range } from "./range";
import { authedJsonFetcher, POLL_OPTIONS } from "./swr";
import { currentTick } from "./world-clock";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

export interface HiddenItem {
  id: string;
  actorId: string;
  body: string;
  occurredAt: string;
}

interface TimelineDoc {
  event_id: string;
  actor_id: string | null;
  body: string;
  occurred_at: string;
}

export function useHiddenFeed(
  enabled: boolean,
  range: Range = "all",
): { items: HiddenItem[]; available: boolean; unlocked: boolean } {
  // 경계는 현재 tick 기준 — 세계일이 넘어가면 key가 바뀌어 창이 함께 넘어간다
  const bounds = enabled ? rangeTickBounds(range, currentTick()) : null;
  const fromParam = bounds ? `&from_tick=${bounds.fromTick}` : "";
  const key = enabled
    ? `${FEED_API_URL}/feed?types=private&player_id=${PLAYER_ID}&world_id=${WORLD_ID}&limit=30${fromParam}`
    : null;
  const { data, error } = useSWR<{ items: TimelineDoc[] }>(key, authedJsonFetcher, {
    refreshInterval: 15_000,
    ...POLL_OPTIONS,
  });

  const items = (data?.items ?? []).map((d) => ({
    id: d.event_id,
    actorId: d.actor_id ?? "?",
    body: d.body,
    occurredAt: d.occurred_at,
  }));

  // 신뢰의 언락은 래치다 — 한 번이라도 닿았으면 범위 조회가 비어도 유지된다
  const [unlocked, setUnlocked] = useState(false);
  const hasItems = items.length > 0;
  useEffect(() => {
    if (hasItems) setUnlocked(true);
  }, [hasItems]);

  return { items, available: data !== undefined && !error, unlocked };
}
