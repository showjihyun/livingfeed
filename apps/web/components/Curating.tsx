import { useWorldClock } from "@/lib/world-clock";
import { useDemoWorldTime } from "@/lib/world-clock-display";
import { COLOR, WEIGHT, RADIUS } from "@/lib/tokens";

import { Face } from "./Face";

const TITLES = ["취향을 반영하는 중", "가장 갈등 밀도 높은 사건을 찾는 중", "찾았어요 — 갈등의 한가운데"];
const SUBS = [
  "빈 피드는 없어요. 진행 중인 사건의 한가운데로 들어갑니다.",
  "당신이 고른 취향 기준",
  "지금 누군가 글을 쓰고 있어요. 실시간으로 함께 봐요.",
];
const PROGRESS = ["18%", "62%", "100%"];

interface CuratingProps {
  step: number;
}

export function Curating({ step }: CuratingProps) {
  const worldTime = useWorldClock(useDemoWorldTime());
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
          width: 480,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 26,
          textAlign: "center",
        }}
      >
        <div style={{ display: "flex", gap: 12, alignItems: "flex-end" }}>
          <Face preset="curateA" style={{ animation: "lf-pop 0.4s ease-out" }} />
          <Face preset="curateB" style={{ animation: "lf-pop 0.5s ease-out" }} />
          <Face preset="curateC" style={{ animation: "lf-pop 0.6s ease-out" }} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 22, fontWeight: WEIGHT.black }}>{TITLES[step]}</div>
          <div style={{ fontSize: 14, color: COLOR.muted, fontWeight: WEIGHT.semibold }}>{SUBS[step]}</div>
        </div>
        <div
          style={{
            width: 260,
            height: 8,
            background: COLOR.border,
            borderRadius: RADIUS.pill,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: PROGRESS[step],
              height: "100%",
              background: COLOR.primary,
              borderRadius: RADIUS.pill,
              transition: "width 0.6s ease",
            }}
          />
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 16px",
            background: COLOR.white,
            border: "1.5px solid #E2EAF6",
            borderRadius: RADIUS.pill,
          }}
        >
          <div
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: COLOR.successBright,
              animation: "lf-blink 1.4s infinite",
            }}
          />
          <div style={{ fontSize: 12, color: COLOR.muted, fontWeight: WEIGHT.bold }}>
            그동안에도 세계 시간은 흐르고 있어요 · {worldTime}
          </div>
        </div>
      </div>
    </div>
  );
}
