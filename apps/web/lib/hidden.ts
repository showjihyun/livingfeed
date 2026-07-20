"use client";

/**
 * Hidden Feed 데이터 계층 — 당신에게만 닿은 비공개 이야기 (ADR-014 private 등급).
 *
 * feed-api /feed?types=private&player_id= → Redis 타임라인(redis-projector)의
 * private 엔트리. 액터가 당신에게만 보낸 것(답장·귓속말)이 여기 모인다.
 * 조회 범위(오늘/이번 주/이번 달)는 from_tick 서버 필터 — 타임라인 엔트리의
 * tick(ADR-011)을 feed-api가 거른다. 15초 재조회마다 경계를 다시 계산하므로
 * 세계일이 넘어가면 "오늘"의 창도 함께 넘어간다.
 * 미가용/빈 경우 빈 목록 — 화면은 빈 상태를 보인다 (하드코딩 데모 없음).
 */

import { useEffect, useState } from "react";

import { authHeaders, PLAYER_ID } from "./config";
import { rangeTickBounds, type Range } from "./range";
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
  const [state, setState] = useState<{ items: HiddenItem[]; available: boolean }>({
    items: [],
    available: false,
  });
  // 신뢰의 언락은 래치다 — 범위 조회가 비어도 "닿은 적 있음"은 사라지지 않는다
  const [unlocked, setUnlocked] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const load = async () => {
      try {
        // 경계는 매 조회마다 현재 tick으로 — 세계일이 넘어가면 창도 넘어간다
        const bounds = rangeTickBounds(range, currentTick());
        const fromParam = bounds ? `&from_tick=${bounds.fromTick}` : "";
        const response = await fetch(
          `${FEED_API_URL}/feed?types=private&player_id=${PLAYER_ID}&world_id=${WORLD_ID}&limit=30${fromParam}`,
          { headers: authHeaders() },
        );
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as { items: TimelineDoc[] };
        if (cancelled) return;
        if (body.items.length > 0) setUnlocked(true);
        setState({
          available: true,
          items: body.items.map((d) => ({
            id: d.event_id,
            actorId: d.actor_id ?? "?",
            body: d.body,
            occurredAt: d.occurred_at,
          })),
        });
      } catch {
        if (!cancelled) setState((s) => ({ ...s, available: false }));
      }
    };
    void load();
    // 배경 탭에서는 폴링 정지 (안 보이는 화면에 네트워크·리렌더 낭비 금지)
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled, range]);

  return { ...state, unlocked };
}
