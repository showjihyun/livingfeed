"use client";

/**
 * SWR 공용 설정 — 읽기(폴링) 데이터 계층의 fetcher·기본 옵션.
 *
 * 수동 fetch + setInterval + document.hidden + cancelled 플래그를 SWR로 대체한다:
 * - refreshInterval: 폴링 주기
 * - refreshWhenHidden 기본 false → 배경 탭에선 폴링 정지 (기존 document.hidden 가드)
 * - 조건부 key(null)로 enabled 게이팅, 중복 요청 dedup·포커스 재검증은 SWR 몫
 *
 * 미가용은 오류가 아니라 조용한 강등이다 (하드코딩 데모 없음) — 호출측은
 * data ?? 빈값으로 빈 상태를 그린다. SWR 기본 재시도는 켜두되(에러 백오프),
 * 콘솔 소음을 줄이려 onError는 삼킨다.
 */

import { authHeaders } from "./config";

/** 공개 엔드포인트용 — 인증 헤더 없이 JSON 조회 (CORS 프리플라이트 회피) */
export async function jsonFetcher<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`fetch ${res.status}`);
  return (await res.json()) as T;
}

/** 사설 경로용 — Authorization: Bearer 동봉 (토큰 미설정 dev면 헤더 없음) */
export async function authedJsonFetcher<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw new Error(`fetch ${res.status}`);
  return (await res.json()) as T;
}

/** 폴링 읽기 훅의 기본 SWR 옵션 — 배경 탭 정지 + 실패해도 화면 유지 */
export const POLL_OPTIONS = {
  refreshWhenHidden: false,
  revalidateOnFocus: true,
  shouldRetryOnError: true,
  keepPreviousData: true,
} as const;
