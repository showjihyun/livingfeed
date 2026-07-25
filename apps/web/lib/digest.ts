"use client";

/**
 * 개인 다이제스트 데이터 계층 — "당신이 없는 동안" (plan/11 §D7·§휴면 복귀).
 *
 * 다이제스트는 따라잡기 부담을 리텐션 훅으로 뒤집는 장치다. 부재 구간
 * (lastSeen 이후)의 세 갈래를 전부 실측으로 요약한다 — 하드코딩 서사 없음:
 *  ① 나를 향한 것 — private 타임라인의 리시트·답장·DM (redis-projector 산출물)
 *  ② 세계의 마디 — world 피드의 debut/arc_transition/goal_achieved·세계 뉴스
 *  ③ 내가 빚은 인물 — created_by가 나인 포스트 (저자성 우선 노출, plan/03)
 *
 * lastSeen은 localStorage(기기별 허용 오차, MVP — 서버 저장 없음)이며,
 * 세션 종료 시가 아니라 **다이제스트를 확인했을 때만** 갱신한다.
 * ULID는 시간 정렬이라 event_id 비교가 곧 "언제 이후" 커트다 (timeline.py
 * 커서와 같은 좌표계). 문장 조합은 순수 함수 — 같은 입력이면 같은 문장
 * (데모·검증 가능성). 백엔드 미가용이면 null — 카드를 띄우지 않는다 (조용한 강등).
 */

import { authHeaders, PLAYER_ID } from "./config";
import { pickMessages, type Locale } from "./i18n";

const FEED_API_URL = process.env.NEXT_PUBLIC_LF_FEED_API_URL ?? "http://localhost:8001";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";

/** 부재 임계 (노브) — 실제 30분 = 세계 2시간(4배속). 이보다 짧으면 재회가 아니다 */
export const ABSENCE_THRESHOLD_MS = 30 * 60 * 1000;

/** 갈래별 조회 폭 — 이 밖의 과거분은 건수에 안 잡힌다 (다이제스트는 하한 요약) */
const FETCH_LIMIT = 50;

/** 대표 줄 수 상한 — 다이제스트는 요약이지 두 번째 피드가 아니다 */
const MAX_YOURS = 2;
const MAX_TO_YOU = 2;
const MAX_WORLD = 3;

/** 세계의 마디로 치는 태그 — 데뷔·장 전환·목표 완주 (engine/feed compose.py 어휘) */
const WORLD_MARK_TAGS = new Set(["debut", "arc_transition", "goal_achieved"]);

// ---------------------------------------------------------------------------
// lastSeen — localStorage 마킹/조회 (다이제스트 확인 시에만 갱신)
// ---------------------------------------------------------------------------

export interface LastSeen {
  /** 마지막 확인 시각 (epoch ms) — 부재 임계 판정의 기준 */
  ts: number;
  /** 확인 시점까지 본 가장 새로운 event_id (ULID) — "언제 이후" 커트의 좌표. 미상이면 null */
  ulid: string | null;
}

const lastSeenKey = () => `lf:lastSeen:${WORLD_ID}:${PLAYER_ID}`;

/** 저장된 lastSeen — 없거나 저장소 미가용이면 null (첫 방문 취급) */
export function readLastSeen(): LastSeen | null {
  try {
    const raw = window.localStorage.getItem(lastSeenKey());
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<LastSeen>;
    if (typeof parsed.ts !== "number") return null;
    return { ts: parsed.ts, ulid: typeof parsed.ulid === "string" ? parsed.ulid : null };
  } catch {
    return null; // 저장소 미가용 — 다이제스트 없이 지나간다 (조용한 강등)
  }
}

/**
 * 지금을 '확인함'으로 새긴다 — 다이제스트를 닫는 순간(또는 첫 입장 기준점).
 * ulid는 뒤로 가지 않는다 (ULID 사전순 = 시간순 — 더 새로운 쪽만 남긴다).
 */
export function markSeen(newestUlid?: string | null): void {
  try {
    const prev = readLastSeen();
    const ulid =
      prev?.ulid && newestUlid
        ? (prev.ulid > newestUlid ? prev.ulid : newestUlid)
        : (newestUlid ?? prev?.ulid ?? null);
    window.localStorage.setItem(lastSeenKey(), JSON.stringify({ ts: Date.now(), ulid }));
  } catch {
    // 저장소 미가용 — 다음 입장에 다시 첫 방문 취급될 뿐이다
  }
}

// ---------------------------------------------------------------------------
// 피드 문서 — feed-api /feed 응답 아이템 (os-projector·redis-projector 공통 모양)
// ---------------------------------------------------------------------------

export interface DigestDoc {
  event_id: string;
  actor_id: string | null;
  title: string;
  body: string;
  occurred_at: string;
  tags: string[];
  /** 원본 이벤트 타입 — relationship.*(리시트) vs actor.message.sent(답장·DM) 구분 */
  source_event_type?: string;
  /** 이 인물을 빚은 플레이어 (데뷔 승계) — "내가 빚은 인물" 갈래의 키 */
  created_by?: string | null;
  drama_score?: number;
}

/** lastSeen 이후인가 — ulid가 있으면 ULID 비교(정밀), 없으면 발생 시각 비교 */
export function isAfterLastSeen(
  doc: Pick<DigestDoc, "event_id" | "occurred_at">,
  lastSeen: LastSeen,
): boolean {
  if (lastSeen.ulid !== null) return doc.event_id > lastSeen.ulid;
  const t = Date.parse(doc.occurred_at);
  return Number.isFinite(t) && t > lastSeen.ts;
}

// ---------------------------------------------------------------------------
// 다이제스트 조합 — 순수·결정적 (같은 입력 → 같은 결과)
// ---------------------------------------------------------------------------

export type DigestLine =
  /** 관계 리시트 — clause는 프로젝터의 정성 문장 꼬리 (수치 비노출 규약 그대로) */
  | { kind: "receipt"; actorId: string; clause: string }
  /** 액터의 답장·선제 DM (doc 수준에선 구분 표식이 없다 — 같은 문장으로 덮는다) */
  | { kind: "dm"; actorId: string; excerpt: string }
  /** 액터의 댓글 답글 */
  | { kind: "comment"; actorId: string; excerpt: string }
  /** 내가 빚은 인물의 소식 — debut이면 첫발 문장, 아니면 BE 내레이션 제목 */
  | { kind: "creation"; actorId: string; debut: boolean; title: string }
  /** 세계의 마디 — BE 내레이션 제목을 그대로 싣는다 (실측 서사) */
  | { kind: "world"; title: string };

export interface PersonalDigest {
  /** ③ 내가 빚은 인물 — 최우선 노출 */
  yours: DigestLine[];
  /** ① 나를 향한 것 — 대표 1~2줄 */
  toYou: DigestLine[];
  /** ① 중 대표 밖 잔여 건수 — "외 N건" */
  toYouMore: number;
  /** ② 세계의 마디 — 대표 2~3건 */
  world: DigestLine[];
  /** 조회분 전체의 최신 event_id — 닫을 때 lastSeen에 새길 좌표 */
  newestUlid: string | null;
  /** 실릴 것이 있는가의 판정 근거 — 0이면 카드를 띄우지 않는다 */
  total: number;
}

/** 본문 발췌 — 한 줄 요약 길이로 자른다 (개행은 공백으로) */
function excerptOf(text: string, max = 42): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, max)}…`;
}

/** 리시트 body("사유 — 정성 문장.")의 꼬리 문장만 — 다이제스트는 더 압축한다 */
function receiptClause(body: string): string {
  const segments = body.split(" — ");
  return segments[segments.length - 1].replace(/\.$/, "").trim();
}

/** private 타임라인 doc → 나를 향한 것 한 줄 */
function toYouLine(doc: DigestDoc): DigestLine {
  const actorId = doc.actor_id ?? "?";
  if ((doc.source_event_type ?? "").startsWith("relationship.")) {
    return { kind: "receipt", actorId, clause: receiptClause(doc.body) };
  }
  const kind = doc.tags.includes("comment") ? "comment" : "dm";
  return { kind, actorId, excerpt: excerptOf(doc.body) };
}

/** ULID 내림차순(최신 먼저) 정렬 사본 */
function newestFirst(docs: DigestDoc[]): DigestDoc[] {
  return [...docs].sort((a, b) => b.event_id.localeCompare(a.event_id));
}

/**
 * 부재 구간의 다이제스트를 짓는다 — 순수 함수 (fetch 없음, Date.now 없음).
 * privateDocs/worldDocs는 feed-api 응답 아이템 그대로 (커트는 여기서 한다).
 */
export function composeDigest(
  privateDocs: DigestDoc[],
  worldDocs: DigestDoc[],
  playerId: string,
  lastSeen: LastSeen,
): PersonalDigest {
  const newPrivate = newestFirst(privateDocs.filter((d) => isAfterLastSeen(d, lastSeen)));
  const newWorld = newestFirst(worldDocs.filter((d) => isAfterLastSeen(d, lastSeen)));

  // ③ 내가 빚은 인물 — 저자성이 최우선이다 (plan/03: 실세의 보상은 저자성)
  const yoursDocs = newWorld.filter((d) => (d.created_by ?? null) === playerId).slice(0, MAX_YOURS);
  const yoursIds = new Set(yoursDocs.map((d) => d.event_id));
  const yours: DigestLine[] = yoursDocs.map((d) => ({
    kind: "creation",
    actorId: d.actor_id ?? "?",
    debut: d.tags.includes("debut"),
    title: d.title,
  }));

  // ① 나를 향한 것 — 액터별 최신 1건으로 대표 줄을 뽑고 나머지는 건수로
  const seenActors = new Set<string>();
  const toYou: DigestLine[] = [];
  for (const doc of newPrivate) {
    if (toYou.length >= MAX_TO_YOU) break;
    const actorId = doc.actor_id ?? "?";
    if (seenActors.has(actorId)) continue;
    seenActors.add(actorId);
    toYou.push(toYouLine(doc));
  }
  const toYouMore = newPrivate.length - toYou.length;

  // ② 세계의 마디 — 마디 태그 또는 세계 뉴스(작성 액터 없음), 큰 사건 먼저
  const world: DigestLine[] = newWorld
    .filter(
      (d) =>
        !yoursIds.has(d.event_id) &&
        (d.actor_id === null || d.tags.some((t) => WORLD_MARK_TAGS.has(t))),
    )
    .sort(
      (a, b) =>
        (b.drama_score ?? 0) - (a.drama_score ?? 0) || b.event_id.localeCompare(a.event_id),
    )
    .slice(0, MAX_WORLD)
    .map((d) => ({ kind: "world", title: d.title }));

  // 닫는 순간 '여기까지 봤다'가 되는 좌표 — 커트 밖 포함, 조회분 전체의 최신
  const newestUlid = [...privateDocs, ...worldDocs].reduce<string | null>(
    (max, d) => (max === null || d.event_id > max ? d.event_id : max),
    null,
  );

  return {
    yours,
    toYou,
    toYouMore,
    world,
    newestUlid,
    total: yours.length + toYou.length + toYouMore + world.length,
  };
}

/**
 * 다이제스트 한 줄 → 서사 문장 — 순수·결정적. 원시 id는 싣지 않는다:
 * name은 디렉터리 해석 함수(모르면 "누군가")를 받는다 (54번 표준).
 *
 * 카테고리별 문장 규칙:
 *  - receipt:  "{이름}의 마음이 움직였어요 — 신뢰가 조금 자랐다"
 *  - dm:       "{이름}에게서 메시지가 와 있어요 — “먼저 연락해봤어…”"
 *  - comment:  "{이름}에게서 답글이 와 있어요 — “고마워, 그 말 덕분에…”"
 *  - creation: "당신이 빚은 {이름} — 세계에 첫발을 딛었어요" (데뷔)
 *              "당신이 빚은 인물의 소식 — {BE 제목}" (그 외 마디)
 *  - world:    BE 내레이션 제목 그대로 ("김민지, 인생의 장이 넘어가다")
 */
export function digestSentence(line: DigestLine, name: (actorId: string) => string): string {
  const t = pickMessages(SENTENCES);
  switch (line.kind) {
    case "receipt":
      return t.receipt(name(line.actorId), line.clause);
    case "dm":
      return t.dm(name(line.actorId), line.excerpt);
    case "comment":
      return t.comment(name(line.actorId), line.excerpt);
    case "creation":
      return line.debut ? t.debut(name(line.actorId)) : t.creationNews(line.title);
    case "world":
      return line.title;
  }
}

const sentencesEn = {
  receipt: (name: string, clause: string) => `${name}'s feelings shifted — ${clause}`,
  dm: (name: string, excerpt: string) => `A message from ${name} is waiting — “${excerpt}”`,
  comment: (name: string, excerpt: string) => `A reply from ${name} is waiting — “${excerpt}”`,
  debut: (name: string) => `${name}, whom you shaped, took their first steps into the world`,
  creationNews: (title: string) => `News of a character you shaped — ${title}`,
};
const SENTENCES: Record<Locale, typeof sentencesEn> = {
  en: sentencesEn,
  ko: {
    receipt: (name, clause) => `${name}의 마음이 움직였어요 — ${clause}`,
    dm: (name, excerpt) => `${name}에게서 메시지가 와 있어요 — “${excerpt}”`,
    comment: (name, excerpt) => `${name}에게서 답글이 와 있어요 — “${excerpt}”`,
    debut: (name) => `당신이 빚은 ${name} — 세계에 첫발을 딛었어요`,
    creationNews: (title) => `당신이 빚은 인물의 소식 — ${title}`,
  },
};

// ---------------------------------------------------------------------------
// 조회 — 부재 구간 재료를 feed-api에서 (미가용이면 null, 조용한 강등)
// ---------------------------------------------------------------------------

async function fetchDocs(
  url: string,
  headers?: Record<string, string>,
): Promise<DigestDoc[] | null> {
  try {
    const response = await fetch(url, headers ? { headers } : undefined);
    if (!response.ok) throw new Error(`feed-api ${response.status}`);
    const body = (await response.json()) as { items: DigestDoc[] };
    return body.items;
  } catch {
    return null;
  }
}

/**
 * 부재 구간의 다이제스트 — private 타임라인 + world 피드(recent)를 모아 짓는다.
 * 두 경로 모두 미가용이면 null(카드 없음), 한쪽만 살아 있으면 그만큼만 싣는다.
 */
export async function loadDigest(lastSeen: LastSeen): Promise<PersonalDigest | null> {
  const [privateDocs, worldDocs] = await Promise.all([
    // private 타임라인은 사설 경로 — 토큰 게이트 뒤에 선다 (world는 공개, 헤더 불필요)
    fetchDocs(
      `${FEED_API_URL}/feed?types=private&player_id=${PLAYER_ID}&world_id=${WORLD_ID}&limit=${FETCH_LIMIT}`,
      authHeaders(),
    ),
    fetchDocs(
      `${FEED_API_URL}/feed?types=world&world_id=${WORLD_ID}&sort=recent&limit=${FETCH_LIMIT}`,
    ),
  ]);
  if (privateDocs === null && worldDocs === null) return null;
  return composeDigest(privateDocs ?? [], worldDocs ?? [], PLAYER_ID, lastSeen);
}
