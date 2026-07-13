"use client";

/**
 * Hidden Feed 데이터 계층 — 당신에게만 닿은 비공개 이야기 (ADR-014 private 등급).
 *
 * feed-api /feed?types=private&player_id= → Redis 타임라인(redis-projector)의
 * private 엔트리. 액터가 당신에게만 보낸 것(답장·귓속말)이 여기 모인다.
 * 미가용/빈 경우 빈 목록 — 화면은 빈 상태를 보인다 (하드코딩 데모 없음).
 */

import { useEffect, useState } from "react";

import { PLAYER_ID } from "./config";

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

export function useHiddenFeed(enabled: boolean): { items: HiddenItem[]; available: boolean } {
  const [state, setState] = useState<{ items: HiddenItem[]; available: boolean }>({
    items: [],
    available: false,
  });

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch(
          `${FEED_API_URL}/feed?types=private&player_id=${PLAYER_ID}&world_id=${WORLD_ID}&limit=30`,
        );
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as { items: TimelineDoc[] };
        if (cancelled) return;
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
    const timer = window.setInterval(() => void load(), 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled]);

  return state;
}
