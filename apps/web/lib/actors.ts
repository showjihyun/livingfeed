"use client";

/**
 * 액터 identity 데이터 계층 — read.actors 실배선 (ADR-003/012).
 *
 * 표시 이름·소개·목표는 백엔드가 정한다 (agents/personas → actor.identity.declared
 * → read.actors). FE는 여기서 읽고 하드코딩하지 않는다. 미가용이면 null —
 * 화면은 식별자 폴백(이니셜 등)만 쓴다.
 */

import { useMemo } from "react";
import useSWR from "swr";

import { jsonFetcher } from "./swr";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

export interface ActorGoal {
  description: string;
  priority: number;
}

export interface ActorIdentity {
  actorId: string;
  name: string;
  archetype: string;
  bio: string;
  goals: ActorGoal[];
}

interface IdentityRow {
  actor_id: string;
  name: string;
  archetype: string;
  bio: string;
  goals: ActorGoal[];
}

function fromRow(row: IdentityRow): ActorIdentity {
  return {
    actorId: row.actor_id,
    name: row.name,
    archetype: row.archetype,
    bio: row.bio,
    goals: row.goals ?? [],
  };
}

/** 세계 액터 명단을 actor_id→identity 맵으로 — 피드/프로필/DM이 이름을 여기서 읽는다.
 *  SWR이 포커스·재접속 시 재검증해 스튜디오 신생/은퇴 인물을 자연히 반영한다. */
export function useActorDirectory(enabled: boolean): {
  byId: ReadonlyMap<string, ActorIdentity>;
  available: boolean;
} {
  const { data } = useSWR<{ actors: IdentityRow[] }>(
    enabled ? `${FEED_API_URL}/actors?world_id=${WORLD_ID}` : null,
    jsonFetcher,
  );
  // useMemo로 맵 참조를 안정화 — byId에 의존하는 authorName/identityOf 콜백이 헛돌지 않게
  const byId = useMemo<ReadonlyMap<string, ActorIdentity>>(
    () => new Map((data?.actors ?? []).map((r) => [r.actor_id, fromRow(r)])),
    [data],
  );
  return { byId, available: byId.size > 0 };
}
