"use client";

/**
 * 설정 패널 — UI 언어 + LLM API 비용·레이트 상한.
 *
 * 사이드바 톱니에서 열리는 오버레이다 (온보딩과 같은 결). 언어는 이 브라우저에만
 * 남지만(localStorage), LLM 상한은 **세계에 걸리는 값**이다: gateway가 Redis에
 * 저장하고 ai-runtime의 예산 가드가 읽어 집행한다 (ADR-018 §3, ADR-020 §2).
 * 그래서 상한 쪽만 저장 버튼과 실패 표시가 있다 — 저장된 줄 알았는데 안 걸린
 * 상한이야말로 이 화면이 막으려는 사고다.
 */

import { useCallback, useEffect, useState } from "react";

import {
  DEFAULT_LIMITS,
  formatTokens,
  formatUsd,
  isNearCap,
  isOverCap,
  saveAiLimits,
  useAiLimits,
  usedRatio,
  type AiLimits,
  type AiUsage,
} from "@/lib/ai-limits";
import { ICON } from "@/lib/data";
import {
  LOCALES,
  LOCALE_NAMES,
  useLocale,
  useMessages,
  type Locale,
} from "@/lib/i18n";
import { COLOR, RADIUS, SHADOW, WEIGHT } from "@/lib/tokens";

import { Icon } from "./Icon";
import { Pressable } from "./Pressable";
import styles from "./lf.module.css";

const en = {
  title: "Settings",
  close: "Close",
  language: "Language",
  languageNote: "Applies to this browser only.",
  llm: "LLM API cost limits",
  llmNote:
    "Enforced by the AI runtime on every model call. Past the downgrade threshold, top-tier actors switch to the smaller model; past the cap, calls are refused and actors fall back to rule-based behaviour — the world keeps running, at lower fidelity.",
  enforce: "Enforce limits",
  enforceOff: "Limits are off — model calls are not capped.",
  spendToday: "Spent today",
  spendMonth: "Spent this month",
  callsToday: "Calls today",
  tokensToday: "Tokens today",
  rpmNow: "Calls this minute",
  noCap: "no cap",
  ofCap: (cap: string) => `of ${cap}`,
  dailyCap: "Daily cap (USD)",
  monthlyCap: "Monthly cap (USD)",
  rpm: "Requests per minute",
  degrade: "Downgrade threshold (%)",
  maxOutput: "Max response tokens",
  zeroIsOff: "0 = no limit",
  providerDefault: "0 = provider default",
  save: "Save limits",
  saving: "Saving…",
  saved: "Limits saved",
  unsaved: "Unsaved changes",
  reset: "Reset to defaults",
  saveFailed: (reason: string) => `Could not save: ${reason}`,
  offline:
    "Gateway not reachable — showing defaults. Limits can't be read or saved until it's up.",
  nearCap: "Downgrade threshold reached — top-tier calls are running on the smaller model.",
  overCap: "Cap reached — model calls are being refused until the window resets.",
  unpriced: (models: string) =>
    `No price on file for ${models} — billed at the top-tier rate when counting spend, so the cap may trigger early. Set LF_MODEL_PRICES to fix.`,
};

const M: Record<Locale, typeof en> = {
  en,
  ko: {
    title: "설정",
    close: "닫기",
    language: "언어",
    languageNote: "이 브라우저에만 적용됩니다.",
    llm: "LLM API 비용 상한",
    llmNote:
      "AI 런타임이 모든 모델 호출에 집행합니다. 강등 기준을 넘으면 상위 티어 액터가 소형 모델로 내려가고, 상한을 넘으면 호출이 거절되어 액터는 규칙 행동으로 돌아갑니다 — 세계는 품질을 낮춰서라도 계속 돕니다.",
    enforce: "상한 적용",
    enforceOff: "상한이 꺼져 있습니다 — 모델 호출에 제한이 없습니다.",
    spendToday: "오늘 지출",
    spendMonth: "이번 달 지출",
    callsToday: "오늘 호출",
    tokensToday: "오늘 토큰",
    rpmNow: "이번 분 호출",
    noCap: "상한 없음",
    ofCap: (cap) => `/ ${cap}`,
    dailyCap: "일 상한 (USD)",
    monthlyCap: "월 상한 (USD)",
    rpm: "분당 호출 수",
    degrade: "강등 기준 (%)",
    maxOutput: "응답 토큰 상한",
    zeroIsOff: "0 = 무제한",
    providerDefault: "0 = 프로바이더 기본값",
    save: "상한 저장",
    saving: "저장 중…",
    saved: "상한을 저장했습니다",
    unsaved: "저장하지 않은 변경",
    reset: "기본값으로",
    saveFailed: (reason) => `저장하지 못했습니다: ${reason}`,
    offline:
      "게이트웨이에 연결할 수 없습니다 — 기본값을 보이는 중입니다. 연결되기 전에는 상한을 읽거나 저장할 수 없습니다.",
    nearCap: "강등 기준에 도달했습니다 — 상위 티어 호출이 소형 모델로 나가고 있습니다.",
    overCap: "상한에 도달했습니다 — 모델 호출이 거절되고 있습니다.",
    unpriced: (models) =>
      `${models} 단가가 등재되지 않았습니다 — 최상위 티어 단가로 셈하므로 상한이 이르게 걸릴 수 있습니다. LF_MODEL_PRICES로 넣어주세요.`,
  },
};

type SaveState = { kind: "idle" | "saving" | "saved" } | { kind: "error"; reason: string };

interface SettingsPanelProps {
  onClose: () => void;
}

export function SettingsPanel({ onClose }: SettingsPanelProps) {
  const t = useMessages(M);
  const { locale, setLocale } = useLocale();
  const { limits, usage, available, apply } = useAiLimits(true);
  // 편집 중인 사본 — null이면 서버 값을 따른다. 사용량 폴링(15초)이 편집을
  // 덮어쓰지 않게 하는 이음새다
  const [draft, setDraft] = useState<AiLimits | null>(null);
  const [save, setSave] = useState<SaveState>({ kind: "idle" });
  const effective = draft ?? limits;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const edit = useCallback(
    (changes: Partial<AiLimits>) => {
      setDraft((current) => ({ ...(current ?? limits), ...changes }));
      setSave({ kind: "idle" });
    },
    [limits],
  );

  const commit = useCallback(async () => {
    setSave({ kind: "saving" });
    const result = await saveAiLimits(effective);
    if (result.ok && result.payload) {
      apply(result.payload);
      setDraft(null); // 서버 값이 진실이 됐다
      setSave({ kind: "saved" });
    } else {
      setSave({ kind: "error", reason: result.error ?? "unknown" });
    }
  }, [apply, effective]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t.title}
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        background: "rgba(58,66,86,0.28)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        // 카드 안 클릭이 배경 닫기로 새지 않게 한다
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 620,
          maxHeight: "88vh",
          overflowY: "auto",
          background: COLOR.white,
          border: `1.5px solid ${COLOR.border}`,
          borderRadius: 26,
          padding: "28px 32px 32px",
          display: "flex",
          flexDirection: "column",
          gap: 22,
          boxShadow: "0 16px 48px rgba(109,141,214,0.18)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Icon d={ICON.gear} size={18} color={COLOR.primaryDeep} />
          <div style={{ fontSize: 20, fontWeight: WEIGHT.black, flex: 1 }}>{t.title}</div>
          <Pressable
            onClick={onClose}
            aria-label={t.close}
            className={styles.navItem}
            style={{
              width: 32,
              height: 32,
              borderRadius: RADIUS.pill,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: COLOR.faint,
            }}
          >
            <Icon d={ICON.close} size={16} />
          </Pressable>
        </div>

        {/* 언어 — 이 브라우저에만 남는다 (localStorage) */}
        <Section title={t.language} note={t.languageNote}>
          <div style={{ display: "flex", gap: 8 }}>
            {LOCALES.map((loc) => {
              const active = locale === loc;
              return (
                <Pressable
                  key={loc}
                  onClick={() => setLocale(loc)}
                  aria-pressed={active}
                  style={{
                    padding: "9px 16px",
                    display: "flex",
                    alignItems: "center",
                    gap: 7,
                    border: `1.5px solid ${active ? COLOR.primary : COLOR.border}`,
                    background: active ? COLOR.primarySoft : COLOR.white,
                    color: active ? COLOR.primaryDeep : COLOR.muted,
                    borderRadius: RADIUS.pill,
                    fontSize: 13,
                    fontWeight: WEIGHT.bold,
                  }}
                >
                  {LOCALE_NAMES[loc]}
                  {active && <Icon d={ICON.check} size={13} />}
                </Pressable>
              );
            })}
          </div>
        </Section>

        {/* LLM 상한 — 세계에 걸리는 값 (gateway → Redis → ai-runtime 가드) */}
        <Section title={t.llm} note={t.llmNote} icon={ICON.coin}>
          {!available && <Notice tone="warn" text={t.offline} />}

          <Toggle
            label={t.enforce}
            on={effective.enabled}
            onChange={(next) => edit({ enabled: next })}
          />
          {!effective.enabled && <Notice tone="warn" text={t.enforceOff} />}

          {usage && <UsageBlock t={t} usage={usage} limits={effective} />}

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <NumberField
              label={t.dailyCap}
              hint={t.zeroIsOff}
              value={effective.daily_usd}
              step={0.5}
              onChange={(v) => edit({ daily_usd: v })}
            />
            <NumberField
              label={t.monthlyCap}
              hint={t.zeroIsOff}
              value={effective.monthly_usd}
              step={5}
              onChange={(v) => edit({ monthly_usd: v })}
            />
            <NumberField
              label={t.rpm}
              hint={t.zeroIsOff}
              value={effective.rpm}
              step={5}
              onChange={(v) => edit({ rpm: Math.round(v) })}
            />
            <NumberField
              label={t.degrade}
              // 저장은 0~1 비율, 화면은 % — 사람이 읽는 단위로 보인다
              value={Math.round(effective.degrade_ratio * 100)}
              min={10}
              max={100}
              step={5}
              onChange={(v) =>
                edit({ degrade_ratio: Math.min(1, Math.max(0.1, v / 100)) })
              }
            />
            <NumberField
              label={t.maxOutput}
              hint={t.providerDefault}
              value={effective.max_output_tokens}
              step={128}
              onChange={(v) => edit({ max_output_tokens: Math.round(v) })}
            />
          </div>

          {usage && usage.unpriced_models.length > 0 && (
            <Notice tone="warn" text={t.unpriced(usage.unpriced_models.join(", "))} />
          )}

          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <Pressable
              onClick={() => void commit()}
              className={styles.press97}
              style={{
                padding: "11px 20px",
                borderRadius: RADIUS.pill,
                background: COLOR.primary,
                color: COLOR.white,
                fontSize: 13,
                fontWeight: WEIGHT.heavy,
                opacity: save.kind === "saving" ? 0.7 : 1,
              }}
            >
              {save.kind === "saving" ? t.saving : t.save}
            </Pressable>
            <Pressable
              onClick={() => edit(DEFAULT_LIMITS)}
              style={{
                padding: "11px 16px",
                borderRadius: RADIUS.pill,
                border: `1.5px solid ${COLOR.border}`,
                color: COLOR.muted,
                fontSize: 13,
                fontWeight: WEIGHT.bold,
              }}
            >
              {t.reset}
            </Pressable>
            <div style={{ fontSize: 12, fontWeight: WEIGHT.bold, flex: 1 }}>
              {save.kind === "error" ? (
                <span style={{ color: COLOR.pink }}>{t.saveFailed(save.reason)}</span>
              ) : save.kind === "saved" ? (
                <span style={{ color: COLOR.success }}>{t.saved}</span>
              ) : draft ? (
                <span style={{ color: COLOR.faint }}>{t.unsaved}</span>
              ) : null}
            </div>
          </div>
        </Section>
      </div>
    </div>
  );
}

/* ── 조각들 ── */

function Section({
  title,
  note,
  icon,
  children,
}: {
  title: string;
  note?: string;
  icon?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
        {icon && <Icon d={icon} size={14} color={COLOR.faint} />}
        <div
          style={{
            fontSize: 11,
            fontWeight: WEIGHT.heavy,
            color: COLOR.fainter,
            letterSpacing: 0.5,
            textTransform: "uppercase",
          }}
        >
          {title}
        </div>
      </div>
      {note && (
        <div style={{ fontSize: 12, color: COLOR.faint, lineHeight: 1.65, fontWeight: WEIGHT.regular }}>
          {note}
        </div>
      )}
      {children}
    </div>
  );
}

function Toggle({
  label,
  on,
  onChange,
}: {
  label: string;
  on: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <Pressable
      onClick={() => onChange(!on)}
      role="switch"
      aria-checked={on}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "10px 14px",
        borderRadius: RADIUS.sm,
        border: `1.5px solid ${on ? COLOR.primary : COLOR.border}`,
        background: on ? COLOR.primarySoft : COLOR.white,
      }}
    >
      <div
        style={{
          width: 34,
          height: 20,
          borderRadius: RADIUS.pill,
          background: on ? COLOR.primary : COLOR.borderMuted,
          padding: 2,
          display: "flex",
          justifyContent: on ? "flex-end" : "flex-start",
          transition: "background 140ms",
        }}
      >
        <div
          style={{
            width: 16,
            height: 16,
            borderRadius: "50%",
            background: COLOR.white,
            boxShadow: SHADOW.card,
          }}
        />
      </div>
      <div
        style={{
          fontSize: 13,
          fontWeight: WEIGHT.bold,
          color: on ? COLOR.primaryDeep : COLOR.muted,
        }}
      >
        {label}
      </div>
    </Pressable>
  );
}

function NumberField({
  label,
  hint,
  value,
  onChange,
  min = 0,
  max,
  step = 1,
}: {
  label: string;
  hint?: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span style={{ fontSize: 12, fontWeight: WEIGHT.bold, color: COLOR.muted }}>{label}</span>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        // 빈 입력·NaN은 0으로 — 상한 필드에 undefined가 들어가면 저장이 422로 막힌다
        onChange={(e) => {
          const parsed = Number.parseFloat(e.target.value);
          onChange(Number.isFinite(parsed) ? Math.max(min, parsed) : 0);
        }}
        style={{
          width: "100%",
          padding: "10px 12px",
          border: `1.5px solid ${COLOR.border}`,
          borderRadius: RADIUS.xs,
          fontSize: 14,
          fontWeight: WEIGHT.bold,
          color: COLOR.ink,
          background: COLOR.surfaceAlt,
          outline: "none",
        }}
      />
      {hint && (
        <span style={{ fontSize: 11, color: COLOR.fainter, fontWeight: WEIGHT.semibold }}>
          {hint}
        </span>
      )}
    </label>
  );
}

function Notice({ tone, text }: { tone: "warn" | "info"; text: string }) {
  const warn = tone === "warn";
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        padding: "10px 13px",
        borderRadius: RADIUS.xs,
        background: warn ? COLOR.pinkSoft : COLOR.primarySoft,
        color: warn ? "#8C4761" : COLOR.primaryDeep,
        fontSize: 12,
        fontWeight: WEIGHT.semibold,
        lineHeight: 1.6,
      }}
    >
      <Icon d={ICON.info} size={14} />
      <div style={{ flex: 1 }}>{text}</div>
    </div>
  );
}

/** 실측 사용량 — 상한 대비 소진율이 한눈에 보여야 상한을 조정할 수 있다 */
function UsageBlock({
  t,
  usage,
  limits,
}: {
  t: typeof en;
  usage: AiUsage;
  limits: AiLimits;
}) {
  const over = isOverCap(usage.day_usd, limits.daily_usd);
  const near = !over && isNearCap(usage.day_usd, limits.daily_usd, limits.degrade_ratio);
  return (
    <div
      style={{
        background: COLOR.surface,
        borderRadius: RADIUS.md,
        padding: "16px 18px",
        display: "flex",
        flexDirection: "column",
        gap: 14,
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <div style={{ fontSize: 12, fontWeight: WEIGHT.heavy, color: COLOR.faint, flex: 1 }}>
          {t.spendToday}
        </div>
        <div style={{ fontSize: 22, fontWeight: WEIGHT.black, color: over ? COLOR.pink : COLOR.ink }}>
          {formatUsd(usage.day_usd)}
        </div>
        <div style={{ fontSize: 12, fontWeight: WEIGHT.bold, color: COLOR.faint }}>
          {limits.daily_usd > 0 ? t.ofCap(formatUsd(limits.daily_usd)) : t.noCap}
        </div>
      </div>

      {limits.daily_usd > 0 && (
        <div style={{ height: 6, borderRadius: RADIUS.pill, background: COLOR.border }}>
          <div
            style={{
              width: `${usedRatio(usage.day_usd, limits.daily_usd) * 100}%`,
              height: "100%",
              borderRadius: RADIUS.pill,
              background: over ? COLOR.pink : near ? "#D9A13B" : COLOR.successBright,
              transition: "width 240ms",
            }}
          />
        </div>
      )}

      <div style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
        <Stat
          label={t.spendMonth}
          value={formatUsd(usage.month_usd)}
          sub={limits.monthly_usd > 0 ? t.ofCap(formatUsd(limits.monthly_usd)) : t.noCap}
        />
        <Stat label={t.callsToday} value={String(usage.calls_today)} />
        <Stat label={t.tokensToday} value={formatTokens(usage.tokens_today)} />
        <Stat
          label={t.rpmNow}
          value={String(usage.rpm_current)}
          sub={limits.rpm > 0 ? t.ofCap(String(limits.rpm)) : t.noCap}
        />
      </div>

      {over && <Notice tone="warn" text={t.overCap} />}
      {near && <Notice tone="warn" text={t.nearCap} />}
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <div style={{ fontSize: 11, fontWeight: WEIGHT.heavy, color: COLOR.fainter }}>{label}</div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 5 }}>
        <div style={{ fontSize: 15, fontWeight: WEIGHT.heavy, color: COLOR.ink }}>{value}</div>
        {sub && (
          <div style={{ fontSize: 11, fontWeight: WEIGHT.bold, color: COLOR.fainter }}>{sub}</div>
        )}
      </div>
    </div>
  );
}
