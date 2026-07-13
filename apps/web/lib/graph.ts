"use client";

/**
 * 관계 그래프 실측 데이터 — gateway /graph/relationships (ADR-006 graph query API 중재).
 *
 * kuzu-projector가 relationship.* 이벤트에서 굳힌 현재 상태를 읽는다: 플레이어와
 * 닿아 있는 엣지의 strength·stage·5차원. 미가용이면 available=false + 빈 엣지 —
 * 화면은 빈 상태를 보인다 (프로젝션은 최적화, 데모 데이터 하드코딩 없음).
 */

import { useEffect, useState } from "react";

import { PLAYER_ID } from "./config";

const GATEWAY_URL = process.env.NEXT_PUBLIC_LF_GATEWAY_URL ?? "http://localhost:8000";
const REFRESH_MS = 20_000;

export interface RelDimensions {
  trust: number;
  intimacy: number;
  respect: number;
  attraction: number;
  resentment: number;
}

export interface LiveRelEdge {
  actorId: string;
  strength: number; // 관계도 0..1 — 서버 단일 정의 (0.5·intimacy + 0.3·trust⁺ + 0.2·salience)
  stage: string;
  dimensions: RelDimensions;
}

interface EdgeRow {
  actor_id: string;
  strength: number;
  stage: string;
  dimensions?: Partial<RelDimensions>;
}

const ZERO_DIMS: RelDimensions = {
  trust: 0,
  intimacy: 0,
  respect: 0,
  attraction: 0,
  resentment: 0,
};

export function useRelationshipGraph(enabled: boolean): {
  edges: LiveRelEdge[];
  available: boolean;
} {
  const [state, setState] = useState<{ edges: LiveRelEdge[]; available: boolean }>({
    edges: [],
    available: false,
  });

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    const load = async () => {
      try {
        const response = await fetch(
          `${GATEWAY_URL}/graph/relationships?player_id=${PLAYER_ID}`,
        );
        if (!response.ok) throw new Error(`graph ${response.status}`);
        const body = (await response.json()) as { available: boolean; edges: EdgeRow[] };
        if (cancelled) return;
        setState({
          available: body.available,
          edges: body.edges.map((e) => ({
            actorId: e.actor_id,
            strength: e.strength,
            stage: e.stage,
            dimensions: { ...ZERO_DIMS, ...(e.dimensions ?? {}) },
          })),
        });
      } catch {
        if (!cancelled) setState((s) => ({ ...s, available: false }));
      }
    };

    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled]);

  return state;
}
