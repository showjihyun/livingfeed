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
const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";
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

/** 세계 전체 관계망의 방향 간선 — feed-api /graph/world (액터↔액터 포함) */
export interface WorldRelEdge {
  fromId: string;
  toId: string;
  strength: number;
  /** 표시 가중치 — 유대(strength)와 긴장(resentment⁺)의 max (서버 단일 정의) */
  weight: number;
  stage: string;
  dimensions: RelDimensions;
}

export interface WorldGraphState {
  nodes: string[];
  edges: WorldRelEdge[];
  /** 간선 상한(cap)에 잘렸는가 — 화면은 "또렷한 인연만 추림"으로 알린다 */
  truncated: boolean;
  available: boolean;
}

export const EMPTY_WORLD_GRAPH: WorldGraphState = {
  nodes: [],
  edges: [],
  truncated: false,
  available: false,
};

interface WorldEdgeRow {
  from_id: string;
  to_id: string;
  strength: number;
  weight: number;
  stage: string;
  dimensions?: Partial<RelDimensions>;
}

/**
 * 세계의 관계망 실측 — 소셜 루프가 쌓아온 액터↔액터 간선까지 전부.
 * 바닥값·cap 기본은 서버(프로젝터 단일 정의)에 맡긴다. 미가용이면 빈 상태.
 */
export function useWorldGraph(enabled: boolean): WorldGraphState {
  const [state, setState] = useState<WorldGraphState>(EMPTY_WORLD_GRAPH);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    const load = async () => {
      try {
        const response = await fetch(`${FEED_API_URL}/graph/world?world_id=${WORLD_ID}`);
        if (!response.ok) throw new Error(`world graph ${response.status}`);
        const body = (await response.json()) as {
          available: boolean;
          truncated: boolean;
          nodes: string[];
          edges: WorldEdgeRow[];
        };
        if (cancelled) return;
        setState({
          available: body.available,
          truncated: body.truncated,
          nodes: body.nodes,
          edges: body.edges.map((e) => ({
            fromId: e.from_id,
            toId: e.to_id,
            strength: e.strength,
            weight: e.weight,
            stage: e.stage,
            dimensions: { ...ZERO_DIMS, ...(e.dimensions ?? {}) },
          })),
        });
      } catch {
        if (!cancelled) setState((s) => ({ ...s, available: false }));
      }
    };

    void load();
    // 배경 탭(document.hidden)에서는 폴링을 멈춘다 — 안 보이는 화면에 네트워크·리렌더
    // 낭비 금지. 최초 로드는 무조건, 이후 폴링만 가시성에 좌우된다.
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled]);

  return state;
}

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
    // 배경 탭(document.hidden)에서는 폴링을 멈춘다 — 안 보이는 화면에 네트워크·리렌더
    // 낭비 금지. 최초 로드는 무조건, 이후 폴링만 가시성에 좌우된다.
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled]);

  return state;
}
