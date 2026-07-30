"use client";

/**
 * 데모 폴백 세계시각 — tick 앵커가 오기 전(온보딩·연결 전) 흐르는 표시값.
 * 현실 4배속: 3초마다 +4분. (앵커가 서면 lib/world-clock의 실 tick 시각이 이긴다.)
 *
 * 단일 외부 스토어 + useSyncExternalStore. 이 값을 루트 컴포넌트 state로 두면
 * 3초 틱이 앱 전체를 리렌더한다 — 그래서 스토어로 빼내, useDemoWorldTime()을
 * 부른 컴포넌트(사이드바·온보딩 등)만 리렌더되게 한다. 앵커가 관측되면 데모
 * 틱을 멈춰(실 시계가 인수) 불필요한 리렌더도 없앤다.
 */

import { useSyncExternalStore } from "react";

import { formatWorldTime, WORLD_MIN_START } from "./data";
import { useLocale } from "./i18n";
import { hasAnchor } from "./world-clock";

const TICK_MS = 3000;
const MIN_PER_TICK = 4;

let worldMin = WORLD_MIN_START;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function stop(): void {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

function ensureTicking(): void {
  if (timer !== null || typeof window === "undefined") return;
  timer = setInterval(() => {
    if (hasAnchor()) {
      stop(); // 실 tick 시계가 인수했다 — 데모 틱은 멈춘다 (리렌더 절약)
      return;
    }
    worldMin += MIN_PER_TICK;
    for (const notify of listeners) notify();
  }, TICK_MS);
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  ensureTicking();
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0) stop();
  };
}

const getSnapshot = (): number => worldMin;
const getServerSnapshot = (): number => WORLD_MIN_START; // SSR 결정적 값

/**
 * 데모 폴백 시계의 '분' 진행도 [0,1) — 시계 바늘의 앵커 전 좌표다.
 *
 * 이 시계는 3초마다 4분씩 **건너뛴다**. 그래서 바늘도 건너뛴다 — 앵커 전에
 * 매끄럽게 돌리면 화면의 숫자와 다른 속도로 흐르는 셈이라, 없는 정밀도를
 * 지어내게 된다 (world-clock의 클램프 규율과 같은 이유).
 */
export function demoMinuteProgress(): number {
  return (worldMin % 60) / 60;
}

/** 데모 폴백 세계시각 문자열. useWorldClock(fallback)의 fallback 인자로 쓴다. */
export function useDemoWorldTime(): string {
  const min = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  // 표기는 UI 언어를 따른다 — 구독해 둬야 언어를 바꾼 그 순간 다시 그려진다
  // (formatWorldTime이 읽는 모듈 캐시만으로는 다음 틱까지 이전 언어가 남는다)
  useLocale();
  return formatWorldTime(min);
}
