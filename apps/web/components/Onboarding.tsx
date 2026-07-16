import { TOPIC_LIST } from "@/lib/data";

import { Face } from "./Face";
import styles from "./lf.module.css";

interface OnboardingProps {
  topics: string[];
  onToggleTopic: (label: string) => void;
  worldTime: string;
  onEnter: () => void;
}

export function Onboarding({ topics, onToggleTopic, worldTime, onEnter }: OnboardingProps) {
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
          background: "#fff",
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
          <div style={{ fontSize: 18, fontWeight: 800 }}>LivingFeed</div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 30, fontWeight: 900, lineHeight: 1.25 }}>
            어떤 이야기가 끌리나요?
          </div>
          <div style={{ fontSize: 14, color: "#6B7691", lineHeight: 1.6, fontWeight: 500 }}>
            최대 2개. 첫 피드를 고르는 데만 쓰여요. 질문은 이것뿐 — 세계는 이미 진행 중입니다.
          </div>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {TOPIC_LIST.map((label) => {
            const on = topics.includes(label);
            return (
              <div
                key={label}
                onClick={() => onToggleTopic(label)}
                style={{
                  padding: "10px 18px",
                  border: `1.5px solid ${on ? "#6D8DD6" : "#E2EAF6"}`,
                  background: on ? "#EDF3FD" : "#ffffff",
                  color: on ? "#5F7EC9" : "#6B7691",
                  borderRadius: 9999,
                  fontSize: 14,
                  fontWeight: 700,
                  cursor: "pointer",
                  userSelect: "none",
                }}
              >
                {label}
              </div>
            );
          })}
        </div>
        <div
          style={{
            background: "#F8FBFF",
            border: "1.5px solid #E2EAF6",
            borderRadius: 16,
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
              background: "#5FBF95",
              animation: "lf-blink 1.6s infinite",
            }}
          />
          <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
            지금 세계 시간 {worldTime} · 세계는 당신 없이도 제 갈 길을 가는 중
          </div>
        </div>
        <div
          onClick={onEnter}
          className={`${styles.enterBtn} ${styles.press97}`}
          style={{
            padding: "15px 0",
            color: "#fff",
            borderRadius: 9999,
            fontSize: 16,
            fontWeight: 800,
            textAlign: "center",
            cursor: "pointer",
            boxShadow: "0 6px 16px rgba(109,141,214,0.35)",
          }}
        >
          세계로 들어가기
        </div>
      </div>
    </div>
  );
}
