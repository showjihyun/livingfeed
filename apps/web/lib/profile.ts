"use client";

/**
 * 액터 프로필 데이터 계층 — PG read 테이블 실배선 (ADR-003/008).
 *
 * 신념(actor_beliefs)과 에피소드(actor_episodes)가 곧 액터의 내면이다 —
 * Memory Fabric이 제품 화면에 드러나는 곳. 미가용이면 available=false,
 * 화면은 데모 서사를 유지한다 (라이브 피드·관계도와 같은 폴백 규약).
 */

import { useEffect, useState } from "react";

import { PLAYER_ID } from "./session";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";

export interface ActorBelief {
  kind: string;
  aboutId: string | null;
  statement: string;
  confidence: number;
  /** 같은 자리(kind, about)의 갱신 횟수 — 생각이 굳어간 흔적 */
  revisions: number;
}

export interface ActorEpisode {
  id: string;
  summary: string;
  importance: number;
  occurredAt: string;
  tags: string[];
}

export interface ActorProfile {
  /** 나(플레이어)에 대한 신념 — "그가 나를 어떻게 생각하는가" */
  aboutMe: ActorBelief[];
  beliefs: ActorBelief[];
  episodes: ActorEpisode[];
}

/** feed-api GET /actors/{id}/profile 응답 (reads.py 계약) */
interface ProfileResponse {
  beliefs: {
    kind: string;
    about_id: string | null;
    statement: string;
    confidence: number;
    revisions: number;
  }[];
  episodes: {
    items: {
      event_id: string;
      summary: string;
      importance: number;
      occurred_at: string;
      tags: string[];
    }[];
  };
}

/** 신념·기억 문장의 플레이어 id를 2인칭으로 — 액터는 id로 기억하지만 화면은 사람에게 말한다 */
export function humanize(text: string): string {
  return (
    text
      .replaceAll(`플레이어 ${PLAYER_ID}`, "당신")
      .replaceAll(PLAYER_ID, "당신")
      // id 뒤에 붙던 양쪽 조사가 "당신" 뒤에서는 하나로 정해진다
      .replaceAll("당신은(는)", "당신은")
      .replaceAll("당신이(가)", "당신이")
      .replaceAll("당신을(를)", "당신을")
      .replaceAll("당신과(와)", "당신과")
  );
}

function fromResponse(body: ProfileResponse): ActorProfile {
  const beliefs = body.beliefs.map((b) => ({
    kind: b.kind,
    aboutId: b.about_id,
    statement: b.statement,
    confidence: b.confidence,
    revisions: b.revisions,
  }));
  return {
    aboutMe: beliefs.filter((b) => b.aboutId === PLAYER_ID),
    beliefs,
    episodes: body.episodes.items.map((e) => ({
      id: e.event_id,
      summary: e.summary,
      importance: e.importance,
      occurredAt: e.occurred_at,
      tags: e.tags,
    })),
  };
}

export function useActorProfile(
  actorId: string,
  enabled: boolean,
): { profile: ActorProfile | null; available: boolean } {
  const [profile, setProfile] = useState<ActorProfile | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(
          `${FEED_API_URL}/actors/${actorId}/profile?episode_limit=8`,
        );
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as ProfileResponse;
        if (!cancelled) setProfile(fromResponse(body));
      } catch {
        // 미가용 — 데모 서사 유지 (조용한 강등, 라이브 피드와 같은 규약)
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [actorId, enabled]);

  // 실측이 "있다"고 말하려면 내용이 있어야 한다 — 빈 응답은 데모가 더 낫다
  const available =
    profile !== null && (profile.episodes.length > 0 || profile.beliefs.length > 0);
  return { profile: available ? profile : null, available };
}
