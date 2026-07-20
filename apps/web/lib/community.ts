"use client";

/**
 * 커뮤니티 피드 데이터 계층 — 세계의 내집단(in-group) 렌즈 (ADR-014 Community).
 *
 * feed-api /communities → 커뮤니티 목록(표시 이름·소개), /feed?types=community&
 * community_id= → 그 커뮤니티에 속한 액터들의 글을 드라마 랭킹으로. 커뮤니티
 * 포스트는 world 가시성이라 세계 피드에도 실리지만, 여기서는 소속으로 걸러
 * "우리 동네의 소식"만 본다. SWR이 폴링·dedup·배경탭 정지를 맡는다(lib/swr).
 * 미가용/빈 경우 빈 목록 (하드코딩 데모 없음).
 */

import useSWR from "swr";

import { jsonFetcher, POLL_OPTIONS } from "./swr";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

export interface Community {
  id: string;
  name: string;
  description: string;
}

export interface CommunityPost {
  id: string;
  actorId: string | null;
  title: string;
  body: string;
  occurredAt: string;
}

interface PostDoc {
  event_id: string;
  actor_id: string | null;
  title: string;
  body: string;
  occurred_at: string;
}

/** 세계 커뮤니티 목록 — 탭의 이름표. 미가용이면 빈 목록(탭은 빈 상태를 보인다). */
export function useCommunities(enabled: boolean): Community[] {
  const { data } = useSWR<{ communities: Community[] }>(
    enabled ? `${FEED_API_URL}/communities` : null,
    jsonFetcher,
  );
  return data?.communities ?? [];
}

/**
 * 한 커뮤니티의 피드 — 소속 액터들의 글(드라마 랭킹). communityId가 null이거나
 * 탭 비활성이면 조회하지 않는다(조건부 key). 15초 폴링 — 세계는 멈추지 않는다.
 */
export function useCommunityFeed(
  communityId: string | null,
  enabled: boolean,
): { posts: CommunityPost[]; available: boolean } {
  const key =
    enabled && communityId
      ? `${FEED_API_URL}/feed?types=community&community_id=${communityId}&world_id=${WORLD_ID}&limit=30`
      : null;
  const { data, error } = useSWR<{ items: PostDoc[] }>(key, jsonFetcher, {
    refreshInterval: 15_000,
    ...POLL_OPTIONS,
  });
  const posts = (data?.items ?? []).map((d) => ({
    id: d.event_id,
    actorId: d.actor_id,
    title: d.title,
    body: d.body,
    occurredAt: d.occurred_at,
  }));
  return { posts, available: data !== undefined && !error };
}
