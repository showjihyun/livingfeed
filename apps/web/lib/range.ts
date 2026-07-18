/**
 * 조회 범위 — 세계 시간의 창 (ADR-011 좌표계).
 *
 * 시간축은 세계 시간이다: 1 세계일 = 360 tick (240 세계초/tick × 360 = 86400초).
 * "오늘"은 현재 세계일의 시작 tick부터 — 시즌·일 경계와 같은 좌표계라
 * 서버(OS/Redis/PG의 tick 필드)와 어긋나지 않는다.
 *
 * 순수 모듈 — React도 world-clock도 모른다. 현재 tick은 호출부가
 * lib/world-clock의 currentTick()에서 가져와 넣는다 (node --test로 검증 가능).
 */

export type Range = "all" | "today" | "week" | "month";

/** 1 세계일 = 360 tick (ADR-011: 86400 세계초 ÷ 240 세계초/tick) */
export const TICKS_PER_WORLD_DAY = 360;

export const RANGES: readonly Range[] = ["all", "today", "week", "month"];

export const RANGE_LABELS: Record<Range, string> = {
  all: "전체",
  today: "오늘",
  week: "이번 주",
  month: "이번 달",
};

/** 범위가 덮는 세계일 수 — 창은 항상 일 경계에서 시작한다 ("오늘"과 같은 결) */
const RANGE_DAYS: Record<Exclude<Range, "all">, number> = {
  today: 1,
  week: 7,
  month: 30,
};

/**
 * 범위 → tick 하한 (포함). "전체"거나 현재 tick을 아직 모르면(앵커 전) null —
 * 호출부는 null을 "필터 없음"으로 다룬다.
 *
 * 오늘 = 현재 세계일의 시작(tick - tick % 360). 주/월은 오늘을 포함해
 * 7일/30일 창 — 시작점은 그 첫날의 일 경계다 (tick 0 밑으로는 내려가지 않는다).
 */
export function rangeTickBounds(
  range: Range,
  currentTick: number | null,
): { fromTick: number } | null {
  if (range === "all" || currentTick === null || !Number.isFinite(currentTick)) return null;
  const dayStart = currentTick - (currentTick % TICKS_PER_WORLD_DAY);
  const fromTick = dayStart - (RANGE_DAYS[range] - 1) * TICKS_PER_WORLD_DAY;
  return { fromTick: Math.max(0, fromTick) };
}
