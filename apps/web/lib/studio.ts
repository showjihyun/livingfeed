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

const GATEWAY_URL = process.env.NEXT_PUBLIC_LF_GATEWAY_URL ?? "http://localhost:8000";

/* ── 닫힌 어휘 (gateway admin 계약) ── */

export type Lifestyle = "office_worker" | "student" | "teacher" | "night_worker" | "flexible";
export type Need = "achievement" | "belonging" | "security";

export const LIFESTYLE_LABELS: Record<Lifestyle, string> = {
  office_worker: "직장인",
  student: "학생",
  teacher: "교사",
  night_worker: "야간 근무",
  flexible: "자유 생활",
};

export const NEED_LABELS: Record<Need, string> = {
  achievement: "성취",
  belonging: "소속",
  security: "안정",
};

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

export const TRAIT_BAND_LABELS: Record<TraitBand, string> = {
  low: "낮음",
  mid: "중간",
  high: "높음",
};

const TRAIT_PHRASES: Record<keyof BigFive, Record<TraitBand, string>> = {
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
};

/** 슬라이더 값 조합 → 사람 문장 두 개 (개방·성실·외향 / 친화·신경) */
export function personalityPreview(f: BigFive): string {
  const o = TRAIT_PHRASES.openness[traitBand(f.openness)];
  const c = TRAIT_PHRASES.conscientiousness[traitBand(f.conscientiousness)];
  const e = TRAIT_PHRASES.extraversion[traitBand(f.extraversion)];
  const a = TRAIT_PHRASES.agreeableness[traitBand(f.agreeableness)];
  const n = TRAIT_PHRASES.neuroticism[traitBand(f.neuroticism)];
  return `${o}, ${c}, ${e} 사람. ${a}, ${n}.`;
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
  return {
    message: Object.keys(fieldErrors).length
      ? "몇 군데가 아직 세계의 규칙에 맞지 않아요 — 표시된 곳을 봐주세요"
      : "입력을 다시 확인해주세요",
    fieldErrors,
  };
}

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
        message: `저장이 거절되었어요 (gateway ${response.status})`,
        fieldErrors: {},
      };
    }
    const saved = (await response.json()) as PersonaDoc;
    return { ok: true, doc: saved };
  } catch {
    return {
      ok: false,
      offline: true,
      message: "게이트웨이에 닿지 못했어요 — 연결되면 다시 시도해주세요",
      fieldErrors: {},
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

  return { personas, available, reload, applyLocal };
}
