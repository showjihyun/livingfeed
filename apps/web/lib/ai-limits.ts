"use client";

/**
 * LLM API 비용·레이트 한도 데이터 계층 — gateway /admin/ai-limits 실배선.
 *
 * 이 화면이 고치는 값은 장식이 아니다: ai-runtime의 예산 가드가 같은 문서를 읽어
 * 집행한다 (ADR-018 §3, ADR-020 §2). 상한을 넘기면 추론이 명시적 오류로 거절되고
 * 액터는 규칙 행동으로 폴백한다 — 세계는 저품질로나마 계속 돈다.
 *
 * 미가용이면 available=false — 화면은 "게이트웨이 연결이 필요해요" 폴백을 보인다
 * (스튜디오·라이브 피드와 같은 조용한 강등 규약). 단, 저장 실패는 조용하지 않다:
 * 저장된 줄 알았는데 안 걸린 상한이야말로 이 기능이 막으려는 사고다.
 */

import { useCallback, useEffect, useState } from "react";

import { authHeaders } from "./config";

const GATEWAY_URL = process.env.NEXT_PUBLIC_LF_GATEWAY_URL ?? "http://localhost:8000";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

/** 사용량 갱신 주기 — 지출은 분 단위로 움직인다 (창 카운터는 1분) */
const REFRESH_MS = 15_000;

/* ── wire 계약 (gateway ai_limits.py AiLimitsDoc 그대로) ── */

export interface AiLimits {
  enabled: boolean;
  /** 분당 호출 상한 (0 = 무제한) */
  rpm: number;
  /** 일 지출 상한 USD (0 = 무제한) */
  daily_usd: number;
  /** 월 지출 상한 USD (0 = 무제한) */
  monthly_usd: number;
  /** 이 비율을 넘으면 hot 티어를 warm 모델로 강등한다 */
  degrade_ratio: number;
  /** 응답 토큰 상한 (0 = 프로바이더 기본값) */
  max_output_tokens: number;
}

export interface AiUsage {
  day: string;
  month: string;
  day_usd: number;
  month_usd: number;
  calls_today: number;
  tokens_today: number;
  rpm_current: number;
  /** 단가 미등재 모델 — 보수적 단가로 셈되고 있다 (설정 경고) */
  unpriced_models: string[];
}

interface AiLimitsPayload {
  limits: AiLimits;
  usage: AiUsage | null;
  available: boolean;
}

/**
 * 게이트웨이 응답 전의 표시값이자 "기본값으로" 버튼의 목표.
 * 서버 기본값(gateway AiLimitsDoc = ai-runtime AiLimits)과 같아야 한다 —
 * 세 곳이 갈리면 화면이 보여주는 값과 집행되는 값이 어긋난다.
 */
export const DEFAULT_LIMITS: AiLimits = {
  enabled: true,
  rpm: 60,
  daily_usd: 5,
  monthly_usd: 0,
  degrade_ratio: 0.8,
  max_output_tokens: 0,
};

/** 상한 대비 소진율 (0 = 상한 없음이면 0) */
export function usedRatio(spent: number, cap: number): number {
  return cap > 0 ? Math.min(1, spent / cap) : 0;
}

/** 상한의 몇 %에서 강등이 걸리는지 넘었는가 — 화면의 주의 색 기준 */
export function isNearCap(spent: number, cap: number, degradeRatio: number): boolean {
  return cap > 0 && spent >= cap * degradeRatio;
}

export function isOverCap(spent: number, cap: number): boolean {
  return cap > 0 && spent >= cap;
}

/** $0.0042처럼 작은 값도 0으로 보이지 않게 — 소진 초기가 가장 알고 싶은 구간이다 */
export function formatUsd(value: number): string {
  if (value === 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

async function requestLimits(init?: RequestInit): Promise<AiLimitsPayload | null> {
  const query = new URLSearchParams({ world_id: WORLD_ID });
  const response = await fetch(`${GATEWAY_URL}/admin/ai-limits?${query.toString()}`, {
    ...init,
    headers: { ...authHeaders(), ...(init?.headers ?? {}) },
  });
  if (!response.ok) throw new Error(`gateway ${response.status}`);
  return (await response.json()) as AiLimitsPayload;
}

export interface SaveResult {
  ok: boolean;
  /** 실패 사유 — 화면에 그대로 보인다 (조용한 실패 금지) */
  error?: string;
  payload?: AiLimitsPayload;
}

export async function saveAiLimits(limits: AiLimits): Promise<SaveResult> {
  try {
    const payload = await requestLimits({
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(limits),
    });
    return payload ? { ok: true, payload } : { ok: false, error: "empty response" };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/** 한도·사용량 훅 — enabled(패널 열림) 동안만 폴링한다 */
export function useAiLimits(enabled: boolean): {
  limits: AiLimits;
  usage: AiUsage | null;
  /** 게이트웨이 실측이 붙었는가 — false면 화면은 연결 안내를 보인다 */
  available: boolean;
  loading: boolean;
  /** 서버 응답으로 로컬 상태를 덮는다 (저장 직후) */
  apply: (payload: AiLimitsPayload) => void;
  reload: () => void;
} {
  const [limits, setLimits] = useState<AiLimits>(DEFAULT_LIMITS);
  const [usage, setUsage] = useState<AiUsage | null>(null);
  const [available, setAvailable] = useState(false);
  const [loading, setLoading] = useState(false);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | undefined;

    const pull = async (withSpinner: boolean) => {
      if (withSpinner) setLoading(true);
      try {
        const payload = await requestLimits();
        if (cancelled || !payload) return;
        setLimits(payload.limits);
        setUsage(payload.usage);
        setAvailable(payload.available);
      } catch {
        if (!cancelled) setAvailable(false); // 조용한 강등 — 기본값을 보인다
      } finally {
        if (!cancelled && withSpinner) setLoading(false);
      }
    };

    void pull(true);
    // 사용량만 갱신하면 되는 폴링이라 스피너는 첫 조회에만 (편집 중 깜빡임 방지)
    timer = setInterval(() => void pull(false), REFRESH_MS);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [enabled, generation]);

  const apply = useCallback((payload: AiLimitsPayload) => {
    setLimits(payload.limits);
    setUsage(payload.usage);
    setAvailable(payload.available);
  }, []);

  const reload = useCallback(() => setGeneration((n) => n + 1), []);
  return { limits, usage, available, loading, apply, reload };
}
