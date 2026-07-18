"use client";

/**
 * 페르소나 스튜디오 — 게임의 아바타 편집기처럼 인물을 빚어 세계에 풀어놓는
 * 창조자 도구. 관전 탭들(피드·프로필·관계)과 달리 여기는 세계 바깥의 작업대다 —
 * 따뜻한 공방 색(호박색)으로 톤을 가른다.
 *
 * 데이터는 lib/studio.ts(gateway /admin/personas)에서 — 미가용이면
 * "게이트웨이 연결이 필요해요" 폴백 (기존 칩 규약).
 */

import { useCallback, useMemo, useState } from "react";
import type { CSSProperties } from "react";

import { PLAYER_ID } from "@/lib/config";
import { ICON } from "@/lib/data";
import {
  BIG_FIVE_LABELS,
  GROUP_AXIS_LABELS,
  LIFESTYLE_LABELS,
  NEED_LABELS,
  TRAIT_BAND_LABELS,
  blankPersona,
  freshId,
  groupKeyOf,
  groupPersonas,
  personaMatches,
  personalityPreview,
  savePersona,
  traitBand,
  usePersonaRoster,
} from "@/lib/studio";
import type { BigFive, GroupAxis, Lifestyle, Need, NeedsBias, PersonaDoc } from "@/lib/studio";

import { Icon } from "./Icon";
import styles from "./lf.module.css";

/* ── 공방의 색 — 관전 탭(파랑·보라)과 구분되는 창조자 톤 ── */
const AMBER = {
  text: "#A97E2F",
  deep: "#7A5B1E",
  bg: "#F9F1DF",
  border: "#EAD9B0",
  solid: "#B08430",
};

const AVATAR_COLORS = ["#E8D5A8", "#F5C8B8", "#BFE3D0", "#AFC8F5", "#CBBDE8"];

function avatarColor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

const INPUT_STYLE: CSSProperties = {
  border: "1.5px solid #E2EAF6",
  borderRadius: 12,
  padding: "10px 14px",
  fontSize: 14,
  fontWeight: 600,
  color: "#3A4256",
  outline: "none",
  background: "#FDFDFE",
  width: "100%",
  fontFamily: "inherit",
};

const NEEDS_LABELS: Record<keyof NeedsBias, string> = {
  achievement: "성취",
  belonging: "소속",
  security: "안정",
};

/* ── 작은 조각들 ── */

function FieldError({ error }: { error?: string }) {
  if (!error) return null;
  return (
    <div style={{ fontSize: 12, fontWeight: 700, color: "#C05B76", lineHeight: 1.5 }}>{error}</div>
  );
}

function FieldLabel({ text }: { text: string }) {
  return <div style={{ fontSize: 12, fontWeight: 800, color: "#6B7691" }}>{text}</div>;
}

function SectionCard({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div
      style={{
        background: "#fff",
        border: "1.5px solid #EEF3FB",
        borderRadius: 20,
        padding: "20px 24px",
        display: "flex",
        flexDirection: "column",
        gap: 14,
        boxShadow: "0 4px 14px rgba(109,141,214,0.06)",
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        <div style={{ fontSize: 13, fontWeight: 800, color: "#8C97AF" }}>{title}</div>
        {hint && <div style={{ fontSize: 12, fontWeight: 600, color: "#A9B2C7", lineHeight: 1.55 }}>{hint}</div>}
      </div>
      {children}
    </div>
  );
}

function SliderRow({
  label,
  value,
  badge,
  accent,
  onChange,
}: {
  label: string;
  value: number;
  badge: string;
  accent: string;
  onChange: (v: number) => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <div style={{ width: 52, fontSize: 13, fontWeight: 800, color: "#6B7691", flexShrink: 0 }}>
        {label}
      </div>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ flex: 1, accentColor: accent, cursor: "pointer" }}
      />
      <div style={{ width: 44, textAlign: "right", fontSize: 12, fontWeight: 800, color: "#8C97AF", flexShrink: 0 }}>
        {badge}
      </div>
    </div>
  );
}

function ChipSelect<T extends string>({
  options,
  labels,
  value,
  onChange,
  small,
}: {
  options: readonly T[];
  labels: Record<T, string>;
  value: T;
  onChange: (v: T) => void;
  small?: boolean;
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {options.map((option) => {
        const selected = option === value;
        return (
          <div
            key={option}
            onClick={() => onChange(option)}
            className={styles.press95}
            style={{
              padding: small ? "4px 12px" : "7px 16px",
              borderRadius: 9999,
              fontSize: small ? 12 : 13,
              fontWeight: 800,
              cursor: "pointer",
              background: selected ? AMBER.bg : "#F2F6FC",
              color: selected ? AMBER.text : "#8C97AF",
              border: `1.5px solid ${selected ? AMBER.border : "transparent"}`,
            }}
          >
            {labels[option]}
          </div>
        );
      })}
    </div>
  );
}

function ActiveToggle({ on, busy, onToggle }: { on: boolean; busy: boolean; onToggle: () => void }) {
  return (
    <div
      onClick={(e) => {
        e.stopPropagation();
        if (!busy) onToggle();
      }}
      style={{
        width: 44,
        height: 26,
        borderRadius: 9999,
        background: on ? "#5FBF95" : "#D8DEEA",
        position: "relative",
        cursor: "pointer",
        transition: "background 0.2s ease",
        opacity: busy ? 0.55 : 1,
        flexShrink: 0,
      }}
    >
      <div
        style={{
          position: "absolute",
          top: 3,
          left: on ? 21 : 3,
          width: 20,
          height: 20,
          borderRadius: "50%",
          background: "#fff",
          transition: "left 0.2s ease",
          boxShadow: "0 1px 3px rgba(58,66,86,0.25)",
        }}
      />
    </div>
  );
}

/* ── 편집/창조 화면 ── */

const LIFESTYLES = Object.keys(LIFESTYLE_LABELS) as Lifestyle[];
const NEEDS = Object.keys(NEED_LABELS) as Need[];

function PersonaEditor({
  initial,
  isNew,
  onBack,
  onSaved,
}: {
  initial: PersonaDoc;
  isNew: boolean;
  onBack: () => void;
  onSaved: (doc: PersonaDoc) => void;
}) {
  const [draft, setDraft] = useState<PersonaDoc>(initial);
  const [saving, setSaving] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formMessage, setFormMessage] = useState<string | null>(null);

  const patch = useCallback((p: Partial<PersonaDoc>) => setDraft((d) => ({ ...d, ...p })), []);

  // 신규 id 입력 — a_ 접두는 고정 어댑터로 붙고, 본문은 로마자 소문자·숫자·밑줄만 남는다
  const idSuffix = draft.id.startsWith("a_") ? draft.id.slice(2) : draft.id;
  const onIdInput = (raw: string) => {
    let s = raw.toLowerCase();
    if (s.startsWith("a_")) s = s.slice(2);
    s = s.replace(/[^a-z0-9_]/g, "");
    patch({ id: s ? `a_${s}` : "" });
  };

  const handleSave = useCallback(async () => {
    if (saving) return;
    // 서버까지 갈 필요 없는 최소 관문 — id는 URL이 되고, 이름은 세계가 부를 말이다
    const guards: Record<string, string> = {};
    if (!draft.name.trim()) guards.name = "이름이 있어야 세계가 이 사람을 부를 수 있어요";
    if (isNew && !/^a_[a-z0-9_]+$/.test(draft.id))
      guards.id = "로마자 소문자·숫자·밑줄로 된 id가 필요해요";
    if (Object.keys(guards).length > 0) {
      setFieldErrors(guards);
      setFormMessage("몇 군데가 아직 비어 있어요 — 표시된 곳을 봐주세요");
      return;
    }
    setSaving(true);
    setFormMessage(null);
    const result = await savePersona({ ...draft, name: draft.name.trim() });
    setSaving(false);
    if (result.ok) {
      setFieldErrors({});
      onSaved(result.doc);
    } else {
      setFieldErrors(result.fieldErrors);
      setFormMessage(result.message);
    }
  }, [draft, isNew, saving, onSaved]);

  // 화면 자리가 없는 검증 에러 — 배너에서라도 그대로 보여준다 (조용히 삼키지 않는다)
  const shownKeys = new Set<string>([
    "name",
    "id",
    "archetype",
    "lifestyle",
    "identity_core",
    ...Object.keys(draft.big_five).map((k) => `big_five.${k}`),
    ...Object.keys(draft.needs_bias).map((k) => `needs_bias.${k}`),
    ...draft.goals.flatMap((_, i) => [`goals.${i}.description`, `goals.${i}.priority`, `goals.${i}.need`]),
    ...draft.secrets.map((_, i) => `secrets.${i}.description`),
  ]);
  const leftoverErrors = Object.entries(fieldErrors).filter(([key]) => !shownKeys.has(key));

  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "16px 32px",
          borderBottom: "1.5px solid #EEF3FB",
        }}
      >
        <div
          onClick={onBack}
          className={styles.press95}
          style={{
            padding: "7px 14px",
            borderRadius: 9999,
            background: "#F2F6FC",
            color: "#6B7691",
            fontSize: 13,
            fontWeight: 800,
            cursor: "pointer",
            flexShrink: 0,
          }}
        >
          ← 명단
        </div>
        <div style={{ fontSize: 18, fontWeight: 900, flex: 1 }}>
          {isNew ? "새 인물 빚기" : `${draft.name || "인물"} 다듬기`}
        </div>
        <div
          onClick={() => void handleSave()}
          className={styles.press95}
          style={{
            padding: "10px 22px",
            borderRadius: 9999,
            background: saving ? "#D8DEEA" : AMBER.solid,
            color: "#fff",
            fontSize: 14,
            fontWeight: 800,
            cursor: saving ? "default" : "pointer",
            flexShrink: 0,
          }}
        >
          {saving ? "세계에 새기는 중…" : isNew ? "세계에 풀어놓기" : "변화를 새기기"}
        </div>
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "24px 32px", background: "#FDFBF6" }}>
        <div
          style={{
            maxWidth: 760,
            margin: "0 auto",
            display: "flex",
            flexDirection: "column",
            gap: 16,
          }}
        >
          {formMessage && (
            <div
              style={{
                background: "#FBECF0",
                border: "1.5px solid #F2CBD7",
                borderRadius: 16,
                padding: "13px 18px",
                fontSize: 13,
                fontWeight: 700,
                color: "#B24E6B",
                lineHeight: 1.6,
              }}
            >
              {formMessage}
              {leftoverErrors.map(([key, msg]) => (
                <div key={key} style={{ fontWeight: 600 }}>
                  {key} — {msg}
                </div>
              ))}
            </div>
          )}

          <SectionCard title="정체성" hint="세계가 이 사람을 무엇으로 부르고, 어떤 리듬으로 살게 할지">
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <FieldLabel text="이름" />
              <input
                style={INPUT_STYLE}
                value={draft.name}
                placeholder="예: 박하늘"
                onChange={(e) => patch({ name: e.target.value })}
              />
              <FieldError error={fieldErrors.name} />
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <FieldLabel text="id" />
              {isNew ? (
                <>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div
                      style={{
                        padding: "10px 12px",
                        borderRadius: 12,
                        background: AMBER.bg,
                        border: `1.5px solid ${AMBER.border}`,
                        fontSize: 14,
                        fontWeight: 800,
                        color: AMBER.text,
                        flexShrink: 0,
                      }}
                    >
                      a_
                    </div>
                    <input
                      style={INPUT_STYLE}
                      value={idSuffix}
                      placeholder="haneul_park"
                      onChange={(e) => onIdInput(e.target.value)}
                    />
                  </div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "#A9B2C7" }}>
                    로마자 소문자·숫자·밑줄 — 세계가 쓰는 내부 이름이라 나중에 못 바꿔요
                  </div>
                </>
              ) : (
                <div style={{ fontSize: 13, fontWeight: 700, color: "#8C97AF" }}>{draft.id}</div>
              )}
              <FieldError error={fieldErrors.id} />
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <FieldLabel text="아키타입" />
              <input
                style={INPUT_STYLE}
                value={draft.archetype}
                placeholder="예: 야심가, 몽상가, 조용한 중재자"
                onChange={(e) => patch({ archetype: e.target.value })}
              />
              <FieldError error={fieldErrors.archetype} />
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <FieldLabel text="생활 리듬" />
              <ChipSelect
                options={LIFESTYLES}
                labels={LIFESTYLE_LABELS}
                value={draft.lifestyle}
                onChange={(lifestyle) => patch({ lifestyle })}
              />
              <FieldError error={fieldErrors.lifestyle} />
            </div>
          </SectionCard>

          <SectionCard title="성격 — 다섯 개의 결" hint="슬라이더를 움직이면 아래 문장에서 사람이 먼저 보여요">
            {(Object.keys(BIG_FIVE_LABELS) as (keyof BigFive)[]).map((trait) => (
              <div key={trait} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <SliderRow
                  label={BIG_FIVE_LABELS[trait]}
                  value={draft.big_five[trait]}
                  badge={TRAIT_BAND_LABELS[traitBand(draft.big_five[trait])]}
                  accent={AMBER.solid}
                  onChange={(v) => patch({ big_five: { ...draft.big_five, [trait]: v } })}
                />
                <FieldError error={fieldErrors[`big_five.${trait}`]} />
              </div>
            ))}
            <div
              style={{
                background: AMBER.bg,
                border: `1.5px solid ${AMBER.border}`,
                borderRadius: 16,
                padding: "14px 18px",
                display: "flex",
                gap: 12,
                alignItems: "flex-start",
              }}
            >
              <Icon d={ICON.sparkles} size={16} color={AMBER.text} style={{ marginTop: 3 }} />
              <div style={{ fontSize: 14, lineHeight: 1.65, fontWeight: 700, color: AMBER.deep }}>
                {personalityPreview(draft.big_five)}
              </div>
            </div>
          </SectionCard>

          <SectionCard title="욕구 편향" hint="이 사람을 움직이는 힘의 배합 — 무엇이 이 사람을 밤에도 깨어 있게 하는지">
            {(Object.keys(NEEDS_LABELS) as (keyof NeedsBias)[]).map((need) => (
              <div key={need} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <SliderRow
                  label={NEEDS_LABELS[need]}
                  value={draft.needs_bias[need]}
                  badge={`${Math.round(draft.needs_bias[need] * 100)}%`}
                  accent="#6D8DD6"
                  onChange={(v) => patch({ needs_bias: { ...draft.needs_bias, [need]: v } })}
                />
                <FieldError error={fieldErrors[`needs_bias.${need}`]} />
              </div>
            ))}
          </SectionCard>

          <SectionCard title="목표" hint="세계 속에서 이 사람이 향해 가는 곳들">
            {draft.goals.map((goal, i) => (
              <div
                key={goal.id}
                style={{
                  border: "1.5px solid #EEF3FB",
                  borderRadius: 14,
                  padding: "14px 16px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 10,
                  background: "#FBFCFE",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <input
                    style={INPUT_STYLE}
                    value={goal.description}
                    placeholder="예: 올해 안에 첫 전시를 연다"
                    onChange={(e) =>
                      patch({
                        goals: draft.goals.map((g) =>
                          g.id === goal.id ? { ...g, description: e.target.value } : g,
                        ),
                      })
                    }
                  />
                  <div
                    onClick={() => patch({ goals: draft.goals.filter((g) => g.id !== goal.id) })}
                    className={styles.press92}
                    style={{
                      width: 30,
                      height: 30,
                      borderRadius: 10,
                      background: "#F2F6FC",
                      color: "#8C97AF",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 16,
                      fontWeight: 800,
                      cursor: "pointer",
                      flexShrink: 0,
                    }}
                  >
                    ×
                  </div>
                </div>
                <FieldError error={fieldErrors[`goals.${i}.description`]} />
                <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
                  <div style={{ flex: 1, minWidth: 220 }}>
                    <SliderRow
                      label="비중"
                      value={goal.priority}
                      badge={`${Math.round(goal.priority * 100)}%`}
                      accent="#6D8DD6"
                      onChange={(v) =>
                        patch({
                          goals: draft.goals.map((g) => (g.id === goal.id ? { ...g, priority: v } : g)),
                        })
                      }
                    />
                  </div>
                  <ChipSelect
                    options={NEEDS}
                    labels={NEED_LABELS}
                    value={goal.need}
                    small
                    onChange={(need) =>
                      patch({
                        goals: draft.goals.map((g) => (g.id === goal.id ? { ...g, need } : g)),
                      })
                    }
                  />
                </div>
                <FieldError error={fieldErrors[`goals.${i}.priority`]} />
                <FieldError error={fieldErrors[`goals.${i}.need`]} />
              </div>
            ))}
            <div
              onClick={() =>
                patch({
                  goals: [
                    ...draft.goals,
                    { id: freshId("g_"), description: "", priority: 0.5, need: "achievement" },
                  ],
                })
              }
              className={styles.press97}
              style={{
                border: "1.5px dashed #D8DEEA",
                borderRadius: 14,
                padding: "12px 16px",
                textAlign: "center",
                fontSize: 13,
                fontWeight: 800,
                color: "#8C97AF",
                cursor: "pointer",
              }}
            >
              ＋ 목표 추가
            </div>
          </SectionCard>

          <SectionCard
            title="비밀"
            hint="세계에 바로 드러나지 않아요 — 서사가 무르익을 때 새어 나옵니다"
          >
            {draft.secrets.map((secret, i) => (
              <div key={secret.id} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <Icon d={ICON.lock} size={15} color="#A9B2C7" />
                  <input
                    style={INPUT_STYLE}
                    value={secret.description}
                    placeholder="예: 사실은 회사를 그만두고 싶어 한다"
                    onChange={(e) =>
                      patch({
                        secrets: draft.secrets.map((s) =>
                          s.id === secret.id ? { ...s, description: e.target.value } : s,
                        ),
                      })
                    }
                  />
                  <div
                    onClick={() => patch({ secrets: draft.secrets.filter((s) => s.id !== secret.id) })}
                    className={styles.press92}
                    style={{
                      width: 30,
                      height: 30,
                      borderRadius: 10,
                      background: "#F2F6FC",
                      color: "#8C97AF",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 16,
                      fontWeight: 800,
                      cursor: "pointer",
                      flexShrink: 0,
                    }}
                  >
                    ×
                  </div>
                </div>
                <FieldError error={fieldErrors[`secrets.${i}.description`]} />
              </div>
            ))}
            <div
              onClick={() =>
                patch({ secrets: [...draft.secrets, { id: freshId("s_"), description: "" }] })
              }
              className={styles.press97}
              style={{
                border: "1.5px dashed #D8DEEA",
                borderRadius: 14,
                padding: "12px 16px",
                textAlign: "center",
                fontSize: 13,
                fontWeight: 800,
                color: "#8C97AF",
                cursor: "pointer",
              }}
            >
              ＋ 비밀 추가
            </div>
          </SectionCard>

          <SectionCard title="내면" hint="이 사람의 안쪽 — 세계가 이 사람의 목소리를 빚을 때 읽는 원문">
            <textarea
              style={{ ...INPUT_STYLE, minHeight: 130, resize: "vertical", lineHeight: 1.7 }}
              value={draft.identity_core}
              placeholder="이 사람의 내면 — 나이·직업·갈등·밤의 생각"
              onChange={(e) => patch({ identity_core: e.target.value })}
            />
            <FieldError error={fieldErrors.identity_core} />
          </SectionCard>

          <div style={{ height: 8 }} />
        </div>
      </div>
    </>
  );
}

/* ── 명단 화면 + 방생 화면 ── */

type View =
  | { kind: "roster" }
  | { kind: "edit"; initial: PersonaDoc; isNew: boolean }
  | { kind: "released"; name: string };

interface StudioTabProps {
  enabled: boolean;
  /** 방생 화면의 "World Feed에서 데뷔를 지켜보세요" — 피드 탭으로 전환 */
  onGoWorldFeed: () => void;
}

export function StudioTab({ enabled, onGoWorldFeed }: StudioTabProps) {
  const { personas, available, applyLocal } = usePersonaRoster(enabled);
  const [view, setView] = useState<View>({ kind: "roster" });
  // 수정 저장 직후 명단 위에 뜨는 확인 — "세계가 받아들입니다" 결
  const [savedName, setSavedName] = useState<string | null>(null);
  const [togglingIds, setTogglingIds] = useState<ReadonlySet<string>>(new Set());
  const [rosterError, setRosterError] = useState<string | null>(null);
  // 명단 조회 — 검색어, 그룹 기준(성격의 결·아키타입·생활 리듬), 선택 그룹(null=전체)
  const [query, setQuery] = useState("");
  const [axis, setAxis] = useState<GroupAxis>("trait");
  const [group, setGroup] = useState<string | null>(null);

  const activeCount = personas.filter((p) => p.active).length;

  const groups = useMemo(() => groupPersonas(personas, axis), [personas, axis]);
  const visible = useMemo(
    () =>
      personas.filter(
        (p) => personaMatches(p, query) && (group === null || groupKeyOf(p, axis) === group),
      ),
    [personas, query, group, axis],
  );
  // 전체 + 검색 없음일 때만 그룹 섹션으로 펼친다 — 필터 중엔 평평한 결과가 읽기 쉽다
  const sectioned = group === null && !query.trim();

  const toggleActive = useCallback(
    (persona: PersonaDoc) => {
      setRosterError(null);
      const next = { ...persona, active: !persona.active };
      applyLocal(next); // 낙관 반영 — 실패하면 되돌린다
      setTogglingIds((prev) => new Set(prev).add(persona.id));
      void savePersona(next).then((result) => {
        setTogglingIds((prev) => {
          const s = new Set(prev);
          s.delete(persona.id);
          return s;
        });
        if (result.ok) {
          applyLocal(result.doc);
        } else {
          applyLocal(persona);
          setRosterError(result.message);
        }
      });
    },
    [applyLocal],
  );

  const handleSaved = useCallback(
    (doc: PersonaDoc, isNew: boolean) => {
      applyLocal(doc);
      if (isNew) {
        setView({ kind: "released", name: doc.name });
      } else {
        setSavedName(doc.name);
        setView({ kind: "roster" });
      }
    },
    [applyLocal],
  );

  if (view.kind === "edit") {
    return (
      <PersonaEditor
        key={view.initial.id || "new"}
        initial={view.initial}
        isNew={view.isNew}
        onBack={() => setView({ kind: "roster" })}
        onSaved={(doc) => handleSaved(doc, view.isNew)}
      />
    );
  }

  if (view.kind === "released") {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", padding: 40, background: "#FDFBF6" }}>
        <div
          style={{
            maxWidth: 480,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            textAlign: "center",
            gap: 18,
            animation: "lf-pop 0.35s ease-out",
          }}
        >
          <div
            style={{
              width: 72,
              height: 72,
              borderRadius: 24,
              background: AMBER.bg,
              border: `1.5px solid ${AMBER.border}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Icon d={ICON.sparkles} size={30} color={AMBER.text} />
          </div>
          <div style={{ fontSize: 22, fontWeight: 900, lineHeight: 1.4 }}>
            세계가 다음 순간 이 사람을 받아들입니다
          </div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#6B7691", lineHeight: 1.7 }}>
            이제 {view.name || "이 사람"}의 시간이 세계 속에서 흐르기 시작해요.
            <br />첫 마디, 첫 만남 — 데뷔는 언제나 World Feed에 먼저 닿습니다.
          </div>
          <div style={{ display: "flex", gap: 10, marginTop: 6 }}>
            <div
              onClick={onGoWorldFeed}
              className={styles.press95}
              style={{
                padding: "11px 22px",
                borderRadius: 9999,
                background: "#6D8DD6",
                color: "#fff",
                fontSize: 14,
                fontWeight: 800,
                cursor: "pointer",
              }}
            >
              World Feed에서 데뷔를 지켜보세요
            </div>
            <div
              onClick={() => setView({ kind: "roster" })}
              className={styles.press95}
              style={{
                padding: "11px 22px",
                borderRadius: 9999,
                background: "#F2F6FC",
                color: "#6B7691",
                fontSize: 14,
                fontWeight: 800,
                cursor: "pointer",
              }}
            >
              명단으로
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "18px 32px",
          borderBottom: "1.5px solid #EEF3FB",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ fontSize: 20, fontWeight: 800 }}>스튜디오</div>
          {available ? (
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 12px",
                background: AMBER.bg,
                color: AMBER.text,
                borderRadius: 9999,
                fontSize: 12,
                fontWeight: 800,
              }}
            >
              <Icon d={ICON.wrench} size={12} /> 창조자 도구 · 실측 연결됨
            </div>
          ) : (
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 12px",
                background: "#F2F6FC",
                color: "#8C97AF",
                borderRadius: 9999,
                fontSize: 12,
                fontWeight: 800,
              }}
            >
              스튜디오는 게이트웨이 연결이 필요해요
            </div>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
            활성 {activeCount} / 전체 {personas.length}
          </div>
          <div
            onClick={() => setView({ kind: "edit", initial: blankPersona(), isNew: true })}
            className={styles.press95}
            style={{
              padding: "9px 18px",
              borderRadius: 9999,
              background: AMBER.solid,
              color: "#fff",
              fontSize: 13,
              fontWeight: 800,
              cursor: "pointer",
            }}
          >
            ＋ 새 인물 빚기
          </div>
        </div>
      </div>

      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "24px 32px",
          display: "flex",
          flexDirection: "column",
          gap: 14,
          background: "#FDFBF6",
        }}
      >
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600, lineHeight: 1.6 }}>
          이곳은 세계 바깥의 작업대예요. 인물을 빚어 풀어놓으면, 세계는 다음 순간부터 그 사람의
          시간을 흘려보냅니다.
        </div>

        {savedName && (
          <div
            style={{
              background: "#E3F5EC",
              border: "1.5px solid #BFE3D0",
              borderRadius: 16,
              padding: "13px 18px",
              display: "flex",
              alignItems: "center",
              gap: 12,
              animation: "lf-pop 0.35s ease-out",
            }}
          >
            <Icon d="M20 6 9 17l-5-5" size={16} color="#3E8A66" />
            <div style={{ flex: 1, fontSize: 13, fontWeight: 700, color: "#3E8A66", lineHeight: 1.6 }}>
              세계가 다음 순간 이 사람을 받아들입니다 — {savedName}의 변화가 스며들고 있어요
            </div>
            <div
              onClick={() => setSavedName(null)}
              style={{ color: "#3E8A66", cursor: "pointer", fontSize: 15, fontWeight: 800, padding: 4 }}
            >
              ×
            </div>
          </div>
        )}

        {rosterError && (
          <div
            style={{
              background: "#FBECF0",
              border: "1.5px solid #F2CBD7",
              borderRadius: 16,
              padding: "13px 18px",
              fontSize: 13,
              fontWeight: 700,
              color: "#B24E6B",
              lineHeight: 1.6,
            }}
          >
            {rosterError}
          </div>
        )}

        {!available && personas.length === 0 ? (
          <div
            style={{
              border: "1.5px dashed #E4D9BE",
              borderRadius: 20,
              padding: "40px 24px",
              textAlign: "center",
              color: "#8C97AF",
              fontSize: 14,
              fontWeight: 600,
              lineHeight: 1.7,
              background: "#fff",
            }}
          >
            스튜디오는 게이트웨이 연결이 필요해요.
            <br />
            세계의 문이 열리면, 인물들이 이 작업대 위에 나타납니다.
          </div>
        ) : personas.length === 0 ? (
          <div
            style={{
              border: "1.5px dashed #E4D9BE",
              borderRadius: 20,
              padding: "40px 24px",
              textAlign: "center",
              color: "#8C97AF",
              fontSize: 14,
              fontWeight: 600,
              lineHeight: 1.7,
              background: "#fff",
            }}
          >
            아직 이 세계에 인물이 없어요.
            <br />첫 사람을 빚어 세계의 시간을 깨워보세요.
          </div>
        ) : (
          <>
            {/* 조회 도구 — 검색 + 성격 그룹 칩. 그룹 어휘는 명단에서 파생된다 (고정 목록 없음) */}
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <input
                style={{ ...INPUT_STYLE, maxWidth: 420 }}
                value={query}
                placeholder="이름·아키타입·내면·목표로 검색"
                onChange={(e) => setQuery(e.target.value)}
              />
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <div style={{ fontSize: 12, fontWeight: 800, color: "#6B7691", flexShrink: 0 }}>
                  그룹 기준
                </div>
                <ChipSelect
                  options={["trait", "archetype", "lifestyle"] as const}
                  labels={GROUP_AXIS_LABELS}
                  value={axis}
                  small
                  onChange={(next) => {
                    setAxis(next);
                    setGroup(null); // 기준이 바뀌면 이전 축의 그룹명은 무의미하다
                  }}
                />
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {[null, ...groups.map((g) => g.key)].map((key) => {
                  const selected = group === key;
                  const count =
                    key === null
                      ? personas.length
                      : (groups.find((g) => g.key === key)?.personas.length ?? 0);
                  return (
                    <div
                      key={key ?? "__all"}
                      onClick={() => setGroup(key)}
                      className={styles.press95}
                      style={{
                        padding: "4px 12px",
                        borderRadius: 9999,
                        fontSize: 12,
                        fontWeight: 800,
                        cursor: "pointer",
                        background: selected ? AMBER.bg : "#F2F6FC",
                        color: selected ? AMBER.text : "#8C97AF",
                        border: `1.5px solid ${selected ? AMBER.border : "transparent"}`,
                      }}
                    >
                      {key ?? "전체"} · {count}
                    </div>
                  );
                })}
              </div>
            </div>

            {visible.length === 0 ? (
              <div
                style={{
                  border: "1.5px dashed #E4D9BE",
                  borderRadius: 20,
                  padding: "40px 24px",
                  textAlign: "center",
                  color: "#8C97AF",
                  fontSize: 14,
                  fontWeight: 600,
                  lineHeight: 1.7,
                  background: "#fff",
                }}
              >
                이 결에 닿는 사람이 아직 없어요.
                <br />
                검색어나 그룹을 바꿔보세요.
              </div>
            ) : (
              (sectioned ? groups : [{ key: null, personas: visible }]).map(
                (section: { key: string | null; personas: PersonaDoc[] }) => (
                  <div
                    key={section.key ?? "__flat"}
                    style={{ display: "flex", flexDirection: "column", gap: 14 }}
                  >
                    {section.key !== null ? (
                      <div style={{ fontSize: 13, fontWeight: 800, color: AMBER.text, marginTop: 4 }}>
                        {section.key} · {section.personas.length}명
                      </div>
                    ) : (
                      <div style={{ fontSize: 12, fontWeight: 700, color: "#8C97AF" }}>
                        {visible.length}명이 조건에 닿았어요
                      </div>
                    )}
                    {section.personas.map((persona) => {
            const oneLiner =
              persona.identity_core.split("\n")[0]?.trim() ||
              persona.archetype ||
              "아직 내면이 비어 있어요";
            const mine = persona.created_by === PLAYER_ID;
            return (
              <div
                key={persona.id}
                onClick={() => setView({ kind: "edit", initial: persona, isNew: false })}
                className={styles.press97}
                style={{
                  background: "#fff",
                  border: `1.5px solid ${mine ? AMBER.border : "#EEF3FB"}`,
                  borderRadius: 20,
                  padding: "16px 22px",
                  display: "flex",
                  alignItems: "center",
                  gap: 16,
                  cursor: "pointer",
                  boxShadow: "0 4px 14px rgba(176,132,48,0.06)",
                }}
              >
                <div
                  style={{
                    width: 44,
                    height: 44,
                    borderRadius: "50%",
                    background: avatarColor(persona.id),
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 17,
                    fontWeight: 800,
                    color: "#3A4256",
                    flexShrink: 0,
                    opacity: persona.active ? 1 : 0.55,
                  }}
                >
                  {(persona.name || persona.id).slice(0, 1).toUpperCase()}
                </div>
                <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <div style={{ fontSize: 15, fontWeight: 800 }}>{persona.name || persona.id}</div>
                    {mine && (
                      <div
                        style={{
                          padding: "2px 10px",
                          background: AMBER.bg,
                          color: AMBER.text,
                          borderRadius: 9999,
                          fontSize: 11,
                          fontWeight: 800,
                        }}
                      >
                        당신이 빚은 인물
                      </div>
                    )}
                    <div
                      style={{
                        padding: "2px 10px",
                        background: "#EDF3FD",
                        color: "#5F7EC9",
                        borderRadius: 9999,
                        fontSize: 11,
                        fontWeight: 800,
                      }}
                    >
                      {LIFESTYLE_LABELS[persona.lifestyle] ?? persona.lifestyle}
                    </div>
                  </div>
                  <div
                    style={{
                      fontSize: 13,
                      color: "#6B7691",
                      fontWeight: 600,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {oneLiner}
                  </div>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 700, color: persona.active ? "#3E8A66" : "#A9B2C7" }}>
                    {persona.active ? "세계에 살고 있음" : "잠들어 있음"}
                  </div>
                  <ActiveToggle
                    on={persona.active}
                    busy={togglingIds.has(persona.id)}
                    onToggle={() => toggleActive(persona)}
                  />
                </div>
              </div>
            );
                    })}
                  </div>
                ),
              )
            )}
          </>
        )}
      </div>
    </>
  );
}
