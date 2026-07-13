"use client";

/**
 * 액터 identity 데이터 계층 — read.actors 실배선 (ADR-003/012).
 *
 * 표시 이름·소개·목표는 백엔드가 정한다 (agents/personas → actor.identity.declared
 * → read.actors). FE는 여기서 읽고 하드코딩하지 않는다. 미가용이면 null —
 * 화면은 식별자 폴백(이니셜 등)만 쓴다.
 */

import { useEffect, useState } from "react";

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

/** 세계 액터 명단을 actor_id→identity 맵으로 — 피드/프로필/DM이 이름을 여기서 읽는다 */
export function useActorDirectory(enabled: boolean): {
  byId: ReadonlyMap<string, ActorIdentity>;
  available: boolean;
} {
  const [byId, setById] = useState<ReadonlyMap<string, ActorIdentity>>(new Map());

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`${FEED_API_URL}/actors?world_id=${WORLD_ID}`);
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as { actors: IdentityRow[] };
        if (cancelled) return;
        setById(new Map(body.actors.map((r) => [r.actor_id, fromRow(r)])));
      } catch {
        // 미가용 — 빈 맵, 화면은 식별자 폴백 (라이브 피드와 같은 규약)
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return { byId, available: byId.size > 0 };
}
