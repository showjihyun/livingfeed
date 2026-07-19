"use client";

/**
 * 커뮤니티 피드 데이터 계층 — 세계의 내집단(in-group) 렌즈 (ADR-014 Community).
 *
 * feed-api /communities → 커뮤니티 목록(표시 이름·소개), /feed?types=community&
 * community_id= → 그 커뮤니티에 속한 액터들의 글을 드라마 랭킹으로. 커뮤니티
 * 포스트는 world 가시성이라 세계 피드에도 실리지만, 여기서는 소속으로 걸러
 * "우리 동네의 소식"만 본다. 백엔드 미가용/빈 경우 빈 목록 (하드코딩 데모 없음).
 */

import { useEffect, useState } from "react";

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
  const [communities, setCommunities] = useState<Community[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`${FEED_API_URL}/communities`);
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as { communities: Community[] };
        if (!cancelled) setCommunities(body.communities);
      } catch {
        if (!cancelled) setCommunities([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return communities;
}

/**
 * 한 커뮤니티의 피드 — 소속 액터들의 글(드라마 랭킹). communityId가 null이면
 * 조회하지 않는다(선택 전). 15초마다 재조회 — 세계는 멈추지 않는다.
 */
export function useCommunityFeed(
  communityId: string | null,
  enabled: boolean,
): { posts: CommunityPost[]; available: boolean } {
  const [state, setState] = useState<{ posts: CommunityPost[]; available: boolean }>({
    posts: [],
    available: false,
  });

  useEffect(() => {
    if (!enabled || !communityId) {
      setState({ posts: [], available: false });
      return;
    }
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch(
          `${FEED_API_URL}/feed?types=community&community_id=${communityId}&world_id=${WORLD_ID}&limit=30`,
        );
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as { items: PostDoc[] };
        if (cancelled) return;
        setState({
          available: true,
          posts: body.items.map((d) => ({
            id: d.event_id,
            actorId: d.actor_id,
            title: d.title,
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
  }, [communityId, enabled]);

  return state;
}
