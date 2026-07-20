import { TOPIC_LIST } from "@/lib/data";
import { useWorldClock } from "@/lib/world-clock";
import { useDemoWorldTime } from "@/lib/world-clock-display";
import { COLOR, WEIGHT, RADIUS } from "@/lib/tokens";

import { Face } from "./Face";
import { Pressable } from "./Pressable";
import styles from "./lf.module.css";

interface OnboardingProps {
  topics: string[];
  onToggleTopic: (label: string) => void;
  onEnter: () => void;
}

export function Onboarding({ topics, onToggleTopic, onEnter }: OnboardingProps) {
  // tick 앵커가 없으면(연결 전이 보통) 데모 폴백 시계 — 루트가 아닌 공용 스토어에서 온다
  const clock = useWorldClock(useDemoWorldTime());
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        zIndex: 40,
        background: "#F4F8FE",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          width: 560,
          background: COLOR.white,
          border: "1.5px solid #E2EAF6",
          borderRadius: 28,
          padding: "44px 48px",
          display: "flex",
          flexDirection: "column",
          gap: 24,
          boxShadow: "0 16px 48px rgba(109,141,214,0.18)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Face preset="logo34" />
          <div style={{ fontSize: 18, fontWeight: WEIGHT.heavy }}>LivingFeed</div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 30, fontWeight: WEIGHT.black, lineHeight: 1.25 }}>
            어떤 이야기가 끌리나요?
          </div>
          <div style={{ fontSize: 14, color: COLOR.muted, lineHeight: 1.6, fontWeight: WEIGHT.regular }}>
            최대 2개. 첫 피드를 고르는 데만 쓰여요. 질문은 이것뿐 — 세계는 이미 진행 중입니다.
          </div>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {TOPIC_LIST.map((label) => {
            const on = topics.includes(label);
            return (
              <Pressable
                key={label}
                onClick={() => onToggleTopic(label)}
                aria-pressed={on}
                style={{
                  padding: "10px 18px",
                  border: `1.5px solid ${on ? COLOR.primary : COLOR.border}`,
                  background: on ? COLOR.primarySoft : COLOR.white,
                  color: on ? COLOR.primaryDeep : COLOR.muted,
                  borderRadius: RADIUS.pill,
                  fontSize: 14,
                  fontWeight: WEIGHT.bold,
                  userSelect: "none",
                }}
              >
                {label}
              </Pressable>
            );
          })}
        </div>
        <div
          style={{
            background: "#F8FBFF",
            border: "1.5px solid #E2EAF6",
            borderRadius: RADIUS.md,
            padding: "14px 18px",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: COLOR.successBright,
              animation: "lf-blink 1.6s infinite",
            }}
          />
          <div style={{ fontSize: 13, color: COLOR.muted, fontWeight: WEIGHT.semibold }}>
            지금 세계 시간 {clock} · 세계는 당신 없이도 제 갈 길을 가는 중
          </div>
        </div>
        <Pressable
          onClick={onEnter}
          className={`${styles.enterBtn} ${styles.press97}`}
          style={{
            width: "100%",
            padding: "15px 0",
            color: COLOR.white,
            borderRadius: RADIUS.pill,
            fontSize: 16,
            fontWeight: WEIGHT.heavy,
            textAlign: "center",
            boxShadow: "0 6px 16px rgba(109,141,214,0.35)",
          }}
        >
          세계로 들어가기
        </Pressable>
      </div>
    </div>
  );
}
