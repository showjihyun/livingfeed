/**
 * rangeTickBounds 경계 검증 — 일 경계 정확성 (ADR-011: 1 세계일 = 360 tick).
 *
 * 실행: node --test lib/range.test.ts  (Node 22.18+/23.6+ 타입 스트리핑 —
 * 별도 러너 없이 순수 모듈만 검증한다. tsc --noEmit에도 함께 걸린다.)
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { rangeTickBounds, TICKS_PER_WORLD_DAY } from "./range.ts";

test("전체·앵커 없음은 필터가 아니다", () => {
  assert.equal(rangeTickBounds("all", 5400), null);
  assert.equal(rangeTickBounds("today", null), null);
  assert.equal(rangeTickBounds("week", Number.NaN), null);
});

test("오늘 = 현재 세계일의 시작 tick", () => {
  // 15일째 한가운데 (tick 5400 = 15.0일 경계, +123은 그 날 안)
  assert.deepEqual(rangeTickBounds("today", 5400 + 123), { fromTick: 5400 });
  // 일 경계 정각 — 그 tick 자신이 하한이다
  assert.deepEqual(rangeTickBounds("today", 5400), { fromTick: 5400 });
  // 일 경계 직전 — 아직 전날이다
  assert.deepEqual(rangeTickBounds("today", 5399), { fromTick: 5400 - TICKS_PER_WORLD_DAY });
});

test("주 = 오늘 포함 7일, 첫날의 일 경계에서 시작", () => {
  assert.deepEqual(rangeTickBounds("week", 5400 + 123), { fromTick: 5400 - 6 * 360 });
  // 창 너비: 하한부터 다음 일 경계까지가 정확히 7일이다
  const bounds = rangeTickBounds("week", 5400);
  assert.equal(5400 + 360 - bounds!.fromTick, 7 * TICKS_PER_WORLD_DAY);
});

test("월 = 오늘 포함 30일", () => {
  assert.deepEqual(rangeTickBounds("month", 12000), {
    fromTick: 12000 - 12000 % 360 - 29 * 360,
  });
});

test("세계의 첫 주 — tick 0 밑으로 내려가지 않는다", () => {
  assert.deepEqual(rangeTickBounds("week", 720), { fromTick: 0 });
  assert.deepEqual(rangeTickBounds("month", 100), { fromTick: 0 });
  assert.deepEqual(rangeTickBounds("today", 0), { fromTick: 0 });
});
