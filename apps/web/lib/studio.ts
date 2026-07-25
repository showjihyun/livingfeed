"use client";

/**
 * 페르소나 스튜디오 데이터 계층 — gateway /admin/personas 실배선.
 *
 * 게임의 아바타 편집기처럼 인물을 빚어 세계에 풀어놓는 창조자 도구의 데이터 경로.
 * personas는 세계의 원본 인물 문서(agents/personas 업서트)이고, PUT 저장 이후의
 * 다음 tick부터 세계가 이 사람을 받아들인다. 문서 타입은 wire 계약 그대로 둔다 —
 * GET으로 받은 것을 고쳐 PUT으로 되돌려보내는 왕복 편집 표면이라 매핑이 오히려
 * 어긋날 자리를 만든다. 미가용이면 available=false — 화면은 "게이트웨이 연결이
 * 필요해요" 폴백을 보인다 (라이브 피드·프로필과 같은 조용한 강등 규약).
 */

import { useCallback, useEffect, useState } from "react";

import { PLAYER_ID } from "./config";
import { pickMessages, type Locale } from "./i18n";

const GATEWAY_URL = process.env.NEXT_PUBLIC_LF_GATEWAY_URL ?? "http://localhost:8000";

/* ── 닫힌 어휘 (gateway admin 계약) ──
   enum 값이 데이터이고 표시 어휘는 UI 언어를 따른다 — 라벨은 호출 시점에 고른다. */

export type Lifestyle = "office_worker" | "student" | "teacher" | "night_worker" | "flexible";
export type Need = "achievement" | "belonging" | "security";

export const LIFESTYLES: readonly Lifestyle[] = [
  "office_worker",
  "student",
  "teacher",
  "night_worker",
  "flexible",
];
export const NEEDS: readonly Need[] = ["achievement", "belonging", "security"];

const LIFESTYLE_MESSAGES: Record<Locale, Record<Lifestyle, string>> = {
  en: {
    office_worker: "Office worker",
    student: "Student",
    teacher: "Teacher",
    night_worker: "Night shift",
    flexible: "Free-form life",
  },
  ko: {
    office_worker: "직장인",
    student: "학생",
    teacher: "교사",
    night_worker: "야간 근무",
    flexible: "자유 생활",
  },
};

const NEED_MESSAGES: Record<Locale, Record<Need, string>> = {
  en: { achievement: "Achievement", belonging: "Belonging", security: "Security" },
  ko: { achievement: "성취", belonging: "소속", security: "안정" },
};

/** 생활 리듬 표시 어휘 (현재 UI 언어) */
export function lifestyleLabels(): Record<Lifestyle, string> {
  return pickMessages(LIFESTYLE_MESSAGES);
}

/** 욕구 표시 어휘 (현재 UI 언어) */
export function needLabels(): Record<Need, string> {
  return pickMessages(NEED_MESSAGES);
}

/* ── 문서 형태 (wire 계약 그대로 — PUT 본문이 곧 이 타입) ── */

export interface BigFive {
  openness: number;
  conscientiousness: number;
  extraversion: number;
  agreeableness: number;
  neuroticism: number;
}

export interface NeedsBias {
  achievement: number;
  belonging: number;
  security: number;
}

export interface PersonaGoal {
  id: string;
  description: string;
  priority: number;
  need: Need;
}

export interface PersonaSecret {
  id: string;
  description: string;
}

export interface PersonaDoc {
  id: string;
  name: string;
  archetype: string;
  lifestyle: Lifestyle;
  active: boolean;
  /** 창조자 — 생성 시에만 유효하고 수정으로 못 바꾼다 (gateway가 지킨다) */
  created_by: string;
  big_five: BigFive;
  needs_bias: NeedsBias;
  goals: PersonaGoal[];
  secrets: PersonaSecret[];
  identity_core: string;
}

/** 하위 id(목표 g_, 비밀 s_) 자동 발급 — 시각+난수 base36, 편집 세션 안에서 충돌 없음 */
export function freshId(prefix: "g_" | "s_"): string {
  return `${prefix}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

/** 새 인물의 빈 문서 — 모든 축을 가운데(0.5)에 두고 창조자만 새긴다 */
export function blankPersona(): PersonaDoc {
  return {
    id: "",
    name: "",
    archetype: "",
    lifestyle: "office_worker",
    active: true,
    created_by: PLAYER_ID,
    big_five: {
      openness: 0.5,
      conscientiousness: 0.5,
      extraversion: 0.5,
      agreeableness: 0.5,
      neuroticism: 0.5,
    },
    needs_bias: { achievement: 0.5, belonging: 0.5, security: 0.5 },
    goals: [],
    secrets: [],
    identity_core: "",
  };
}

/* ── 성격 정성 미리보기 — 수치가 아니라 사람이 보이게 ──
   축별 3구간(낮음/중간/높음) 템플릿의 결정적 조합. 같은 슬라이더 값이면
   언제나 같은 문장이 나온다 — 창조자가 손맛으로 조율할 수 있는 근거다. */

export type TraitBand = "low" | "mid" | "high";

/** 0..1 값 → 3구간. 경계(0.35/0.65)는 미리보기와 슬라이더 배지가 함께 쓴다 */
export function traitBand(value: number): TraitBand {
  if (value < 0.35) return "low";
  if (value > 0.65) return "high";
  return "mid";
}

const TRAIT_BAND_MESSAGES: Record<Locale, Record<TraitBand, string>> = {
  en: { low: "Low", mid: "Mid", high: "High" },
  ko: { low: "낮음", mid: "중간", high: "높음" },
};

/** 슬라이더 배지 어휘 (현재 UI 언어) */
export function traitBandLabels(): Record<TraitBand, string> {
  return pickMessages(TRAIT_BAND_MESSAGES);
}

type TraitPhrases = Record<keyof BigFive, Record<TraitBand, string>>;

const PREVIEW_MESSAGES: Record<Locale, { phrases: TraitPhrases; compose: (p: string[]) => string }> =
  {
    en: {
      phrases: {
        openness: {
          high: "drawn to the unfamiliar",
          mid: "moving between the familiar and the new",
          low: "at ease in a familiar world",
        },
        conscientiousness: {
          high: "settled only once there is a plan",
          mid: "planning only when it matters",
          low: "happier improvising than planning",
        },
        extraversion: {
          high: "gathering strength among people",
          mid: "drifting between company and solitude",
          low: "gathering strength in solitude",
        },
        agreeableness: {
          high: "sooner accommodating than clashing",
          mid: "yielding where it helps, holding firm where it counts",
          low: "sooner clashing than giving in",
        },
        neuroticism: {
          high: "small things linger long in their mind",
          mid: "shaken at times, but quick to settle",
          low: "little shakes them",
        },
      },
      // 두 문장 — 뒷문장은 새 문장이라 첫 글자를 세운다
      compose: ([o, c, e, a, n]) =>
        `Someone ${o}, ${c}, ${e}. ${a.charAt(0).toUpperCase()}${a.slice(1)}, ${n}.`,
    },
    ko: {
      phrases: {
        openness: {
          high: "낯선 것에 끌리고",
          mid: "익숙함과 새로움 사이를 오가고",
          low: "익숙한 세계가 편안하고",
        },
        conscientiousness: {
          high: "계획이 서야 마음이 놓이며",
          mid: "필요할 때만 계획을 세우며",
          low: "계획보다 즉흥이 편하며",
        },
        extraversion: {
          high: "사람들 속에서 힘을 얻는",
          mid: "혼자와 함께 사이를 오가는",
          low: "혼자만의 시간에서 힘을 얻는",
        },
        agreeableness: {
          high: "부딪히기보다 맞춰주는 편이고",
          mid: "맞출 때는 맞추되 물러서지 않을 때는 버티고",
          low: "져주기보다 부딪히는 편이고",
        },
        neuroticism: {
          high: "작은 일도 마음에 오래 남는다",
          mid: "흔들려도 금세 제자리를 찾는다",
          low: "웬만한 일에는 흔들리지 않는다",
        },
      },
      compose: ([o, c, e, a, n]) => `${o}, ${c}, ${e} 사람. ${a}, ${n}.`,
    },
  };

/** 슬라이더 값 조합 → 사람 문장 두 개 (개방·성실·외향 / 친화·신경) */
export function personalityPreview(f: BigFive): string {
  const { phrases, compose } = pickMessages(PREVIEW_MESSAGES);
  return compose(TRAIT_ORDER.map((trait) => phrases[trait][traitBand(f[trait])]));
}

/* ── 명단 조회 — 성격 그룹(아키타입)별 묶기 + 검색 ──
   그룹 어휘는 고정 목록이 아니라 명단 데이터에서 파생된다 — 창조자가 새
   아키타입을 빚으면 그룹도 함께 태어난다. */

const UNGROUPED_MESSAGES: Record<Locale, string> = { en: "Uncharted", ko: "결 미정" };

/** 아키타입이 비어 있는 인물의 그룹 표기 — 표시 어휘일 뿐 문서를 바꾸지 않는다 */
export function ungroupedArchetype(): string {
  return pickMessages(UNGROUPED_MESSAGES);
}

/** 그룹 판정 키 — 카드·칩·묶기가 같은 규칙을 쓴다 */
export function archetypeOf(persona: PersonaDoc): string {
  return persona.archetype.trim() || ungroupedArchetype();
}

const BIG_FIVE_MESSAGES: Record<Locale, Record<keyof BigFive, string>> = {
  en: {
    openness: "Openness",
    conscientiousness: "Conscientiousness",
    extraversion: "Extraversion",
    agreeableness: "Agreeableness",
    neuroticism: "Neuroticism",
  },
  ko: {
    openness: "개방성",
    conscientiousness: "성실성",
    extraversion: "외향성",
    agreeableness: "친화성",
    neuroticism: "신경성",
  },
};

/** Big Five 표기 — 편집기 슬라이더와 성격 그룹명이 같은 어휘를 쓴다 */
export function bigFiveLabels(): Record<keyof BigFive, string> {
  return pickMessages(BIG_FIVE_MESSAGES);
}

/** 결정적 순회 순서 — 동률일 때도 같은 인물은 언제나 같은 그룹에 선다 */
export const TRAIT_ORDER: readonly (keyof BigFive)[] = [
  "openness",
  "conscientiousness",
  "extraversion",
  "agreeableness",
  "neuroticism",
];

/** 성격의 결 — 가운데(0.5)에서 가장 멀리 나간 축이 그 사람의 결이다.
    전 축이 중간이면 "고른 결". traitBand와 같은 경계(0.35/0.65)를 쓴다. */
export function traitGroup(f: BigFive): string {
  let dominant: keyof BigFive | null = null;
  let distance = 0;
  for (const trait of TRAIT_ORDER) {
    const d = Math.abs(f[trait] - 0.5);
    if (d > distance) {
      distance = d;
      dominant = trait;
    }
  }
  const t = pickMessages(TRAIT_GROUP_MESSAGES);
  if (dominant === null || traitBand(f[dominant]) === "mid") return t.even;
  return t.leaning(bigFiveLabels()[dominant], f[dominant] > 0.5);
}

const TRAIT_GROUP_MESSAGES: Record<
  Locale,
  { even: string; leaning: (trait: string, high: boolean) => string }
> = {
  en: {
    even: "Evenly balanced",
    leaning: (trait, high) => `${high ? "High" : "Low"} ${trait.toLowerCase()}`,
  },
  ko: {
    even: "고른 결",
    leaning: (trait, high) => `${trait} ${high ? "높은" : "낮은"} 결`,
  },
};

/** 그룹 기준 — 성격의 결(Big Five 파생)·아키타입·생활 리듬 세 축 */
export type GroupAxis = "trait" | "archetype" | "lifestyle";

export const GROUP_AXES: readonly GroupAxis[] = ["trait", "archetype", "lifestyle"];

const GROUP_AXIS_MESSAGES: Record<Locale, Record<GroupAxis, string>> = {
  en: { trait: "Personality", archetype: "Archetype", lifestyle: "Life rhythm" },
  ko: { trait: "성격의 결", archetype: "아키타입", lifestyle: "생활 리듬" },
};

/** 그룹 축 표시 어휘 (현재 UI 언어) */
export function groupAxisLabels(): Record<GroupAxis, string> {
  return pickMessages(GROUP_AXIS_MESSAGES);
}

/** 카드·칩·묶기가 공유하는 그룹 판정 — 축이 무엇이든 표시 어휘로 판정한다 */
export function groupKeyOf(persona: PersonaDoc, axis: GroupAxis): string {
  if (axis === "archetype") return archetypeOf(persona);
  if (axis === "lifestyle") return lifestyleLabels()[persona.lifestyle] ?? persona.lifestyle;
  return traitGroup(persona.big_five);
}

export interface PersonaGroup {
  key: string;
  personas: PersonaDoc[];
}

/** 명단 → 그룹. 인원 많은 순, 동수면 이름순 — 결정적 정렬 */
export function groupPersonas(personas: PersonaDoc[], axis: GroupAxis): PersonaGroup[] {
  const map = new Map<string, PersonaDoc[]>();
  for (const persona of personas) {
    const key = groupKeyOf(persona, axis);
    const list = map.get(key);
    if (list) list.push(persona);
    else map.set(key, [persona]);
  }
  return [...map.entries()]
    .map(([key, list]) => ({ key, personas: list }))
    .sort(
      (a, b) => b.personas.length - a.personas.length || a.key.localeCompare(b.key, "ko"),
    );
}

/** 검색 — 이름·id·아키타입·생활 리듬·내면·목표·비밀을 대소문자 무시 부분일치로 훑는다.
    비밀도 검색된다: 스튜디오는 창조자의 작업대라 편집기에서 이미 보이는 재료다. */
export function personaMatches(persona: PersonaDoc, rawQuery: string): boolean {
  const query = rawQuery.trim().toLowerCase();
  if (!query) return true;
  const haystack = [
    persona.name,
    persona.id,
    persona.archetype,
    lifestyleLabels()[persona.lifestyle] ?? persona.lifestyle,
    persona.identity_core,
    ...persona.goals.map((g) => g.description),
    ...persona.secrets.map((s) => s.description),
  ]
    .join("\n")
    .toLowerCase();
  return haystack.includes(query);
}

/* ── 저장 (PUT 업서트) ── */

export type SaveResult =
  | { ok: true; doc: PersonaDoc }
  | {
      ok: false;
      /** true면 게이트웨이 자체에 닿지 못한 것 — 검증 실패와 구분해 보여준다 */
      offline: boolean;
      message: string;
      /** 필드 경로("name", "big_five.openness", "goals.0.description") → 서버 검증 메시지 */
      fieldErrors: Record<string, string>;
    };

/** FastAPI 422 detail → 필드 경로별 메시지. 형태를 모르는 detail은 요약문으로 강등 */
function parseValidationDetail(detail: unknown): {
  message: string;
  fieldErrors: Record<string, string>;
} {
  if (typeof detail === "string") return { message: detail, fieldErrors: {} };
  const fieldErrors: Record<string, string> = {};
  if (Array.isArray(detail)) {
    for (const item of detail) {
      const entry = item as { loc?: (string | number)[]; msg?: string };
      if (!entry.msg) continue;
      const loc = entry.loc ?? [];
      // 선두의 "body"는 위치 표기일 뿐 — 필드 경로에서 걷어낸다
      const path = (loc[0] === "body" ? loc.slice(1) : loc).join(".");
      if (path) fieldErrors[path] = entry.msg;
    }
  }
  const t = pickMessages(GATEWAY_MESSAGES);
  return {
    message: Object.keys(fieldErrors).length ? t.someFieldsInvalid : t.checkInput,
    fieldErrors,
  };
}

/* ── 게이트웨이 응답 문구 — 창조자에게 보이는 결과 알림 (UI 크롬) ── */
const GATEWAY_MESSAGES: Record<
  Locale,
  {
    someFieldsInvalid: string;
    checkInput: string;
    saveRejected: (status: number) => string;
    unreachable: string;
    alreadyLeft: string;
    notInRoster: string;
    releaseFailed: (status: number) => string;
    alreadyLiving: string;
    notInArchive: string;
    restoreFailed: (status: number) => string;
    purgeFailed: (status: number) => string;
  }
> = {
  en: {
    someFieldsInvalid: "A few things do not fit the world's rules yet — check the marked fields",
    checkInput: "Please check your input again",
    saveRejected: (status) => `The save was rejected (gateway ${status})`,
    unreachable: "Could not reach the gateway — try again once it is connected",
    alreadyLeft: "This person has already left the world",
    notInRoster: "This person is not on the world's roster",
    releaseFailed: (status) => `Could not let them go (gateway ${status})`,
    alreadyLiving: "This person is already living in the world",
    notInArchive: "The archive has no record of this person",
    restoreFailed: (status) => `Could not bring them back (gateway ${status})`,
    purgeFailed: (status) => `Could not erase them for good (gateway ${status})`,
  },
  ko: {
    someFieldsInvalid: "몇 군데가 아직 세계의 규칙에 맞지 않아요 — 표시된 곳을 봐주세요",
    checkInput: "입력을 다시 확인해주세요",
    saveRejected: (status) => `저장이 거절되었어요 (gateway ${status})`,
    unreachable: "게이트웨이에 닿지 못했어요 — 연결되면 다시 시도해주세요",
    alreadyLeft: "이 사람은 이미 세계를 떠났어요",
    notInRoster: "이 사람은 세계의 명단에 없어요",
    releaseFailed: (status) => `떠나보내지 못했어요 (gateway ${status})`,
    alreadyLiving: "이 사람은 이미 세계에 살고 있어요",
    notInArchive: "보관함에 이 사람의 기록이 없어요",
    restoreFailed: (status) => `다시 불러오지 못했어요 (gateway ${status})`,
    purgeFailed: (status) => `영구 삭제하지 못했어요 (gateway ${status})`,
  },
};

export async function savePersona(doc: PersonaDoc): Promise<SaveResult> {
  try {
    const response = await fetch(`${GATEWAY_URL}/admin/personas/${encodeURIComponent(doc.id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    if (response.status === 422) {
      const body = (await response.json()) as { detail?: unknown };
      return { ok: false, offline: false, ...parseValidationDetail(body.detail) };
    }
    if (!response.ok) {
      return {
        ok: false,
        offline: false,
        message: pickMessages(GATEWAY_MESSAGES).saveRejected(response.status),
        fieldErrors: {},
      };
    }
    const saved = (await response.json()) as PersonaDoc;
    return { ok: true, doc: saved };
  } catch {
    return {
      ok: false,
      offline: true,
      message: pickMessages(GATEWAY_MESSAGES).unreachable,
      fieldErrors: {},
    };
  }
}

/* ── 떠나보내기 (DELETE) — 세계의 역사에 은퇴가 기록되고, 화면들이 이 사람을 놓아준다 ── */

export type DeleteResult =
  | { ok: true; name: string }
  | {
      ok: false;
      /** true면 게이트웨이 자체에 닿지 못한 것 — 서버의 거절과 구분해 보여준다 */
      offline: boolean;
      message: string;
    };

/** 인물을 세계에서 떠나보낸다 — 되돌릴 수 없다 (호출 전 확인은 화면의 몫) */
export async function deletePersona(id: string): Promise<DeleteResult> {
  try {
    const query = new URLSearchParams({ retired_by: PLAYER_ID });
    const response = await fetch(
      `${GATEWAY_URL}/admin/personas/${encodeURIComponent(id)}?${query.toString()}`,
      { method: "DELETE" },
    );
    if (!response.ok) {
      const t = pickMessages(GATEWAY_MESSAGES);
      const message =
        response.status === 410
          ? t.alreadyLeft
          : response.status === 404
            ? t.notInRoster
            : t.releaseFailed(response.status);
      return { ok: false, offline: false, message };
    }
    const body = (await response.json()) as { actor_id: string; name: string };
    return { ok: true, name: body.name };
  } catch {
    return {
      ok: false,
      offline: true,
      message: pickMessages(GATEWAY_MESSAGES).unreachable,
    };
  }
}

/* ── 떠난 사람들 (복원) — 은퇴의 역방향: 보관함 조회와 다시 불러오기 ── */

export interface RetiredPersona {
  id: string;
  name: string;
  archetype: string;
  /** 보관본 파일명 — 같은 id가 여러 번 떠났으면 -2, -3…으로 구분된다 (목록의 키) */
  filename: string;
}

/** 떠난 사람들의 보관함 목록 — 미가용이면 빈 목록 (조용한 강등: 섹션이 아예 안 보인다) */
export async function fetchRetired(): Promise<RetiredPersona[]> {
  try {
    const response = await fetch(`${GATEWAY_URL}/admin/personas/retired`);
    if (!response.ok) return [];
    const body = (await response.json()) as { retired?: RetiredPersona[] };
    return body.retired ?? [];
  } catch {
    return [];
  }
}

export type RestoreResult =
  | { ok: true; name: string }
  | {
      ok: false;
      /** true면 게이트웨이 자체에 닿지 못한 것 — 서버의 거절과 구분해 보여준다 */
      offline: boolean;
      message: string;
    };

/** 떠난 인물을 세계로 다시 불러온다 — 글·관계의 귀환은 세계(프로젝터)가 집행한다 */
export async function restorePersona(id: string): Promise<RestoreResult> {
  try {
    const query = new URLSearchParams({ returned_by: PLAYER_ID });
    const response = await fetch(
      `${GATEWAY_URL}/admin/personas/${encodeURIComponent(id)}/restore?${query.toString()}`,
      { method: "POST" },
    );
    if (!response.ok) {
      const t = pickMessages(GATEWAY_MESSAGES);
      const message =
        response.status === 409
          ? t.alreadyLiving
          : response.status === 404
            ? t.notInArchive
            : t.restoreFailed(response.status);
      return { ok: false, offline: false, message };
    }
    const body = (await response.json()) as { actor_id: string; name: string };
    return { ok: true, name: body.name };
  } catch {
    return {
      ok: false,
      offline: true,
      message: pickMessages(GATEWAY_MESSAGES).unreachable,
    };
  }
}

/* ── 영구 삭제 (보관본 purge) — 은퇴의 끝: 다시 부를 수 없게 보관본을 지운다 ── */

export type PurgeResult =
  | { ok: true; name: string }
  | {
      ok: false;
      /** true면 게이트웨이 자체에 닿지 못한 것 — 서버의 거절과 구분해 보여준다 */
      offline: boolean;
      message: string;
    };

/** 보관된 인물을 영구 삭제한다 — 되돌릴 수 없다 (호출 전 확인은 화면의 몫).
    filename이 키다: 같은 id의 보관본(-2, -3…) 중 정확히 하나만 지운다. */
export async function purgeRetired(filename: string): Promise<PurgeResult> {
  try {
    const response = await fetch(
      `${GATEWAY_URL}/admin/personas/retired/${encodeURIComponent(filename)}`,
      { method: "DELETE" },
    );
    if (!response.ok) {
      const t = pickMessages(GATEWAY_MESSAGES);
      const message =
        response.status === 404 ? t.notInArchive : t.purgeFailed(response.status);
      return { ok: false, offline: false, message };
    }
    const body = (await response.json()) as { filename: string; name: string };
    return { ok: true, name: body.name };
  } catch {
    return {
      ok: false,
      offline: true,
      message: pickMessages(GATEWAY_MESSAGES).unreachable,
    };
  }
}

/* ── 명단 훅 ── */

export function usePersonaRoster(enabled: boolean): {
  personas: PersonaDoc[];
  /** 명단 실측이 붙었는가 — false면 화면은 게이트웨이 폴백 안내를 보인다 */
  available: boolean;
  reload: () => void;
  /** 저장 성공본을 목록에 즉시 반영 (재조회 없이) — 신규는 맨 앞에 선다 */
  applyLocal: (doc: PersonaDoc) => void;
  /** 떠나보낸 인물을 목록에서 즉시 내린다 (재조회 없이) */
  removeLocal: (id: string) => void;
} {
  const [personas, setPersonas] = useState<PersonaDoc[]>([]);
  const [available, setAvailable] = useState(false);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`${GATEWAY_URL}/admin/personas`);
        if (!response.ok) throw new Error(`gateway ${response.status}`);
        const body = (await response.json()) as { personas: PersonaDoc[] };
        if (cancelled) return;
        setPersonas(body.personas ?? []);
        setAvailable(true);
      } catch {
        // 미가용 — 이미 보이는 명단은 유지하되 연결 표식만 내린다 (조용한 강등)
        if (!cancelled) setAvailable(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled, generation]);

  const reload = useCallback(() => setGeneration((g) => g + 1), []);

  const applyLocal = useCallback((doc: PersonaDoc) => {
    setPersonas((list) =>
      list.some((p) => p.id === doc.id)
        ? list.map((p) => (p.id === doc.id ? doc : p))
        : [doc, ...list],
    );
  }, []);

  const removeLocal = useCallback((id: string) => {
    setPersonas((list) => list.filter((p) => p.id !== id));
  }, []);

  return { personas, available, reload, applyLocal, removeLocal };
}
