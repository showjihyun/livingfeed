"use client";

/**
 * 세계 시계 — 엔진 tick이 진실이다 (ADR-011, engine/tick/src/lf_tick/clock.py와 동형).
 *
 * 세계시각 = genesis + tick × 240초. FE는 tick을 만들지 않는다 — SSE 봉투와
 * /feed 초기 문서에서 관측한 최신 tick이 시계의 앵커다 (lib/live-feed.ts가 배선).
 *
 * 앵커 규칙: {tick, observedAtMs(로컬)} — 더 큰 tick만 승계한다 (늦게 도착한
 * 과거 이벤트가 시계를 되돌리지 못한다).
 *
 * 클램프 규칙: worldNow()는 앵커 tick의 세계시각에서 로컬 경과 × 4배속만큼
 * 전진하되 다음 tick 경계(+240초)를 넘지 않는다 — 엔진이 미루기 정책으로
 * tick을 늦추면 시계도 함께 기다린다. 엔진이 진실이고 FE는 보간만 한다.
 *
 * 계산 예 (genesis=2026-03-01T00:00Z, 240s/tick, 4배속):
 *   observeTick(5400) 직후      → 5400×240s = 15일 → "3월 16일 00:00"
 *   로컬 30초 뒤                → +30×4 = 120s 전진 → "3월 16일 00:02"
 *   로컬 90초 뒤 (tick 지연 중) → 240s 경계 클램프 → "3월 16일 00:04"에서 대기
 *   observeTick(5401) 도착      → 앵커 승계, "3월 16일 00:04"부터 다시 흐른다
 *
 * 앵커가 없으면(연결 전 온보딩 화면) worldNow()는 null — 호출부는 기존
 * 데모 시계(lib/data.ts formatWorldTime)로 폴백한다.
 */

import { useEffect, useState } from "react";

import { formatWorldDate } from "./data";
import { useLocale } from "./i18n";

/** tick 0의 세계시각 — 배포 seam (기본값은 ADR-011 genesis) */
export const GENESIS_UTC_MS = Date.parse(
  process.env.NEXT_PUBLIC_LF_GENESIS ?? "2026-03-01T00:00:00Z",
);
/** 1 tick = 세계시간 240초 (engine/tick clock.py WORLD_SECONDS_PER_TICK_DEFAULT) */
export const WORLD_SECONDS_PER_TICK = 240;
/** 세계는 실시간의 4배속 (240 세계초 / 60 실초) */
export const WORLD_SPEED = 4;

export interface TickAnchor {
  /** 관측한 세계 tick (봉투/색인 문서의 tick) */
  tick: number;
  /** 그 tick을 관측한 로컬 시각 (Date.now() ms) */
  observedAtMs: number;
}

let anchor: TickAnchor | null = null;

/** 최신 tick 관측 — 더 큰 tick만 앵커를 승계한다 */
export function observeTick(tick: number, observedAtMs: number = Date.now()): void {
  if (!Number.isFinite(tick) || tick < 0) return;
  if (anchor !== null && tick <= anchor.tick) return;
  anchor = { tick, observedAtMs };
}

/**
 * 순수 계산 — 앵커 주입 가능 (검증용). 앵커 tick의 세계시각에서 로컬 경과×배속을
 * 더하되, 다음 tick 경계(+240 세계초)까지만 전진한다.
 */
export function worldTimeAt(a: TickAnchor, nowMs: number): number {
  const base = GENESIS_UTC_MS + a.tick * WORLD_SECONDS_PER_TICK * 1000;
  const advanceMs = Math.max(0, nowMs - a.observedAtMs) * WORLD_SPEED;
  return base + Math.min(advanceMs, WORLD_SECONDS_PER_TICK * 1000);
}

/** 현재 세계시각(UTC ms) — 앵커가 없으면 null (호출부가 기존 방식으로 폴백) */
export function worldNow(nowMs: number = Date.now()): number | null {
  return anchor === null ? null : worldTimeAt(anchor, nowMs);
}

/** tick 앵커가 관측됐는가 — 데모 폴백 시계가 멈출 시점 판단용 (world-clock-display). */
export function hasAnchor(): boolean {
  return anchor !== null;
}

/**
 * 현재 세계 tick — 앵커 그대로 (엔진이 진실, 보간은 표시용이다).
 * 조회 범위(lib/range)의 "오늘" 경계 계산이 이 좌표를 쓴다. 앵커 전엔 null.
 */
export function currentTick(): number | null {
  return anchor?.tick ?? null;
}

/** 세계 1분 = 60,000 세계ms. 4배속이라 실시간으로는 15초다 (스윕 한 바퀴). */
export const WORLD_MS_PER_MINUTE = 60_000;

/**
 * 지금 세계 '분'의 진행도 [0,1) — 시계 바늘이 한 바퀴 도는 좌표다.
 *
 * worldNow()에서 파생되므로 클램프를 그대로 물려받는다: 엔진이 미루기 정책으로
 * tick을 늦추면 바늘도 그 자리에 선다. 바늘이 계속 돌면 "세계가 흐르고 있다"는
 * 거짓말이 되므로, 멈춤도 사실대로 보인다.
 *
 * 앵커 전(연결 전)에는 null — 그때는 보간할 진실이 없다.
 */
export function worldMinuteProgress(nowMs: number = Date.now()): number | null {
  const now = worldNow(nowMs);
  return now === null ? null : (now % WORLD_MS_PER_MINUTE) / WORLD_MS_PER_MINUTE;
}

/** 표시 형식은 기존 데모 시계(data.ts)와 동일한 결 — UI 언어를 따른다 */
export function formatWorldClock(utcMs: number): string {
  const d = new Date(utcMs);
  const h = String(d.getUTCHours()).padStart(2, "0");
  const m = String(d.getUTCMinutes()).padStart(2, "0");
  return formatWorldDate(d.getUTCMonth() + 1, d.getUTCDate(), `${h}:${m}`);
}

/**
 * tick 앵커 시계 구독 — 앵커가 관측되면 엔진 기준 세계시각을, 아니면 fallback
 * (기존 prop 시계)을 돌려준다. 초기 렌더는 항상 fallback — SSR/hydration 안전.
 */
export function useWorldClock(fallback: string): string {
  const [live, setLive] = useState<string | null>(null);
  // 표기 언어가 바뀌면 다음 초를 기다리지 않고 곧바로 다시 포맷한다
  const { locale } = useLocale();

  useEffect(() => {
    const refresh = () => {
      const now = worldNow();
      setLive(now === null ? null : formatWorldClock(now));
    };
    refresh();
    const id = window.setInterval(refresh, 1000);
    return () => window.clearInterval(id);
  }, [locale]);

  return live ?? fallback;
}
