"use client";

/**
 * 액터 프로필 데이터 계층 — PG read 테이블 실배선 (ADR-003/008).
 *
 * 신념(actor_beliefs)과 에피소드(actor_episodes)가 곧 액터의 내면이다 —
 * Memory Fabric이 제품 화면에 드러나는 곳. 미가용이면 available=false,
 * 화면은 데모 서사를 유지한다 (라이브 피드·관계도와 같은 폴백 규약).
 */

import { useCallback, useEffect, useState } from "react";

import type { ActorIdentity } from "./actors";
import { PLAYER_ID } from "./config";
import { rangeTickBounds, type Range } from "./range";
import { currentTick } from "./world-clock";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";

/** 에피소드 한 페이지 크기 — 이보다 덜 오면 더 과거가 없다는 뜻 (커서를 닫는 근거) */
const EPISODE_PAGE_SIZE = 8;

export interface ActorBelief {
  kind: string;
  aboutId: string | null;
  statement: string;
  confidence: number;
  /** 같은 자리(kind, about)의 갱신 횟수 — 생각이 굳어간 흔적 */
  revisions: number;
}

/**
 * 철회된 신념의 잔불 상한 — 이 이하 확신은 근거가 무너져 철회문으로 갱신된 것이다
 * (engine reflection의 RETRACTED_CONFIDENCE(0.05) 계약, ADR-008 신념 폐기)
 */
export const FADED_BELIEF_MAX = 0.1;

export interface ActorEpisode {
  id: string;
  summary: string;
  importance: number;
  occurredAt: string;
  tags: string[];
}

/** Director가 그린 이번 시즌의 인생 방향 (ADR-013, plan/08 Life Journey) */
export interface ActorArc {
  stage: string;
  intention: string;
  plannedAt: string;
}

/** 인생 단계 코드 → 표시 라벨 (plan/08의 닫힌 어휘 — 미지 코드는 코드 그대로) */
export const ARC_STAGE_LABELS: Record<string, string> = {
  student: "학생기",
  newcomer: "사회 초년기",
  settling: "정착·방황기",
  prime: "전성기·침체기",
  elder: "원로기",
};

export interface ActorProfile {
  /** 이름·소개·목표 — read.actors (없으면 null, 화면은 식별자 폴백) */
  identity: ActorIdentity | null;
  /** 나(플레이어)에 대한 신념 — "그가 나를 어떻게 생각하는가" */
  aboutMe: ActorBelief[];
  beliefs: ActorBelief[];
  episodes: ActorEpisode[];
  /** 인생의 장 — read.actor_arcs (없으면 null, 그저 일상을 사는 중) */
  arc: ActorArc | null;
  /** 인생의 연대기 — 장의 흐름, 오래된 순 (append-only 이력) */
  arcHistory: ActorArc[];
}

/** feed-api GET /actors/{id}/profile 응답 (reads.py 계약) */
interface ProfileResponse {
  actor_id: string;
  identity: {
    actor_id: string;
    name: string;
    archetype: string;
    bio: string;
    goals: { description: string; priority: number }[];
  } | null;
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
    /** 더 과거 에피소드 커서 — 마지막 페이지에서도 남는다 (빈 페이지가 곧 끝) */
    next_cursor: string | null;
  };
  arc: { stage: string; intention: string; planned_at: string } | null;
  arc_history: { stage: string; intention: string; planned_at: string }[];
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

function toEpisode(e: ProfileResponse["episodes"]["items"][number]): ActorEpisode {
  return {
    id: e.event_id,
    summary: e.summary,
    importance: e.importance,
    occurredAt: e.occurred_at,
    tags: e.tags,
  };
}

/** 응답의 에피소드 커서 — 페이지가 덜 찼으면 더 과거가 없으니 여기서 닫는다 */
function episodesNextCursor(episodes: ProfileResponse["episodes"]): string | null {
  return episodes.items.length < EPISODE_PAGE_SIZE ? null : episodes.next_cursor;
}

function fromResponse(body: ProfileResponse): ActorProfile {
  const beliefs = body.beliefs.map((b) => ({
    kind: b.kind,
    aboutId: b.about_id,
    statement: b.statement,
    confidence: b.confidence,
    revisions: b.revisions,
  }));
  const id = body.identity;
  return {
    identity: id
      ? {
          actorId: id.actor_id,
          name: id.name,
          archetype: id.archetype,
          bio: id.bio,
          goals: id.goals ?? [],
        }
      : null,
    aboutMe: beliefs.filter((b) => b.aboutId === PLAYER_ID),
    beliefs,
    episodes: body.episodes.items.map(toEpisode),
    arc: body.arc
      ? { stage: body.arc.stage, intention: body.arc.intention, plannedAt: body.arc.planned_at }
      : null,
    arcHistory: (body.arc_history ?? []).map((c) => ({
      stage: c.stage,
      intention: c.intention,
      plannedAt: c.planned_at,
    })),
  };
}

export function useActorProfile(
  actorId: string,
  enabled: boolean,
  range: Range = "all",
): {
  profile: ActorProfile | null;
  available: boolean;
  /** 더 과거 기억 페이지가 남아있는가 — 빈 페이지를 받으면 닫힌다 */
  hasMoreEpisodes: boolean;
  loadingEpisodes: boolean;
  loadMoreEpisodes: () => void;
} {
  const [profile, setProfile] = useState<ActorProfile | null>(null);
  const [episodeCursor, setEpisodeCursor] = useState<string | null>(null);
  // 이번 조회의 tick 하한 — 첫 페이지에서 굳혀 더보기 페이지에도 같은 창을 쓴다
  const [episodeFromTick, setEpisodeFromTick] = useState<number | null>(null);
  const [loadingEpisodes, setLoadingEpisodes] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    // 대상 액터·범위가 바뀌면 이전 커서는 무효 — 첫 페이지부터 다시
    setEpisodeCursor(null);
    // 겪은 일(에피소드)의 조회 범위 — 세계 tick 하한 (lib/range, ADR-011 좌표계)
    const bounds = rangeTickBounds(range, currentTick());
    const fromTick = bounds?.fromTick ?? null;
    setEpisodeFromTick(fromTick);
    const fromParam = fromTick === null ? "" : `&episode_from_tick=${fromTick}`;
    void (async () => {
      try {
        const response = await fetch(
          `${FEED_API_URL}/actors/${actorId}/profile?episode_limit=${EPISODE_PAGE_SIZE}${fromParam}`,
        );
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as ProfileResponse;
        if (cancelled) return;
        setProfile(fromResponse(body));
        setEpisodeCursor(episodesNextCursor(body.episodes));
      } catch {
        // 미가용 — 데모 서사 유지 (조용한 강등, 라이브 피드와 같은 규약)
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [actorId, enabled, range]);

  // 과거 기억 이어받기 — 같은 프로필 응답에서 에피소드 페이지만 취해 아래에 붙인다
  const loadMoreEpisodes = useCallback(() => {
    if (!episodeCursor || loadingEpisodes) return;
    setLoadingEpisodes(true);
    const fromParam = episodeFromTick === null ? "" : `&episode_from_tick=${episodeFromTick}`;
    void (async () => {
      try {
        const response = await fetch(
          `${FEED_API_URL}/actors/${actorId}/profile?episode_limit=${EPISODE_PAGE_SIZE}&episode_cursor=${episodeCursor}${fromParam}`,
        );
        if (!response.ok) throw new Error(`feed-api ${response.status}`);
        const body = (await response.json()) as ProfileResponse;
        const older = body.episodes.items.map(toEpisode);
        setProfile((prev) => {
          if (!prev) return prev;
          // 중복(event id) 제거 — 커서 경계에서 같은 행이 두 번 오면 한 번만 남긴다
          const seen = new Set(prev.episodes.map((e) => e.id));
          return { ...prev, episodes: [...prev.episodes, ...older.filter((e) => !seen.has(e.id))] };
        });
        // 빈 페이지가 곧 끝 — 커서를 닫아 버튼을 숨긴다
        setEpisodeCursor(older.length ? episodesNextCursor(body.episodes) : null);
      } catch {
        // 미가용 — 이미 보이는 기억은 유지, 커서도 남긴다 (재시도 가능)
      } finally {
        setLoadingEpisodes(false);
      }
    })();
  }, [actorId, episodeCursor, episodeFromTick, loadingEpisodes]);

  // 실측이 "있다"고 말하려면 내용이 있어야 한다 — 정체성·신념·기억·아크 중 하나라도
  const available =
    profile !== null &&
    (profile.identity !== null ||
      profile.episodes.length > 0 ||
      profile.beliefs.length > 0 ||
      profile.arc !== null);
  return {
    profile: available ? profile : null,
    available,
    hasMoreEpisodes: available && episodeCursor !== null,
    loadingEpisodes,
    loadMoreEpisodes,
  };
}
