import type { CSSProperties } from "react";
import { COLOR } from "@/lib/tokens";

/** 디자인 핸드오프의 블롭 얼굴 아바타 — 눈/입/눈썹/볼터치를 절대 배치로 그린다.
 *  수치는 프로토타입(docs/design-handoff)의 픽셀 값을 그대로 옮긴 프리셋이다. */

type Mouth =
  | { kind: "smile" | "frown"; w: number; h: number; stroke: number; r: number; top: number }
  | { kind: "open"; w: number; h: number; r: number; top: number }
  | { kind: "neutral"; w: number; h: number; r: number; top: number };

interface FaceSpec {
  size: number;
  radius: string;
  bg: string;
  border?: string;
  eye: { size: number; x: number; top: number };
  mouth: Mouth;
  brows?: { w: number; h: number; x: number; top: number; deg: number };
  blush?: { w: number; h: number; x: number; top: number };
}

const INK = COLOR.ink;

export const FACE = {
  /** 온보딩 헤더 로고 (34px 둥근 사각형) */
  logo34: {
    size: 34,
    radius: "12px",
    bg: COLOR.accent,
    eye: { size: 4, x: 9, top: 13 },
    mouth: { kind: "smile", w: 8, h: 4, stroke: 2, r: 8, top: 20 },
  },
  /** 사이드바 로고 (30px 둥근 사각형) */
  logo30: {
    size: 30,
    radius: "10px",
    bg: COLOR.accent,
    eye: { size: 3.5, x: 8, top: 11 },
    mouth: { kind: "smile", w: 7, h: 3.5, stroke: 2, r: 7, top: 18 },
  },
  /** 큐레이션 트리오 */
  curateA: {
    size: 52,
    radius: "50%",
    bg: COLOR.accent,
    eye: { size: 5.5, x: 14, top: 20 },
    mouth: { kind: "frown", w: 9, h: 4, stroke: 2, r: 8, top: 32 },
  },
  curateB: {
    size: 62,
    radius: "50%",
    bg: "#FFE9A8",
    eye: { size: 6.5, x: 17, top: 23 },
    mouth: { kind: "open", w: 13, h: 6, r: 11, top: 37 },
  },
  curateC: {
    size: 52,
    radius: "50%",
    bg: "#F5B8CB",
    eye: { size: 5.5, x: 14, top: 20 },
    mouth: { kind: "smile", w: 9, h: 5, stroke: 2, r: 8, top: 31 },
  },
  /** 사이드바 하단 플레이어 (관찰자_0417) */
  user30: {
    size: 30,
    radius: "50%",
    bg: "#D9E2F2",
    eye: { size: 3.5, x: 8, top: 12 },
    mouth: { kind: "neutral", w: 6, h: 2, r: 2, top: 19 },
  },
  /** DM 헤더 아바타 38px */
  dmHeader38: {
    size: 38,
    radius: "50%",
    bg: COLOR.accent,
    eye: { size: 4.5, x: 10, top: 14 },
    mouth: { kind: "frown", w: 8, h: 4, stroke: 2, r: 8, top: 23 },
  },
  /** DM 말풍선 아바타 30px */
  dm30: {
    size: 30,
    radius: "50%",
    bg: COLOR.accent,
    eye: { size: 3.5, x: 8, top: 11 },
    mouth: { kind: "smile", w: 6, h: 3, stroke: 1.5, r: 6, top: 18 },
  },
  /** 프로필 히어로 88px — 흰 테두리 */
  profile88: {
    size: 88,
    radius: "50%",
    bg: COLOR.accent,
    border: "5px solid #fff",
    eye: { size: 8, x: 24, top: 34 },
    mouth: { kind: "frown", w: 15, h: 7, stroke: 3, r: 13, top: 52 },
    brows: { w: 10, h: 3, x: 20, top: 24, deg: 13 },
  },
} satisfies Record<string, FaceSpec>;

export type FacePreset = keyof typeof FACE;

interface FaceProps {
  preset: FacePreset;
  /** 프리셋 배경색 오버라이드 (댓글 아바타 등) */
  bg?: string;
  onClick?: () => void;
  style?: CSSProperties;
}

function mouthStyle(mouth: Mouth): CSSProperties {
  const base: CSSProperties = {
    position: "absolute",
    left: "50%",
    top: mouth.top,
    transform: "translateX(-50%)",
    width: mouth.w,
    height: mouth.h,
  };
  switch (mouth.kind) {
    case "smile":
      return {
        ...base,
        borderBottom: `${mouth.stroke}px solid ${INK}`,
        borderRadius: `0 0 ${mouth.r}px ${mouth.r}px`,
      };
    case "frown":
      return {
        ...base,
        borderTop: `${mouth.stroke}px solid ${INK}`,
        borderRadius: `${mouth.r}px ${mouth.r}px 0 0`,
      };
    case "open":
      return { ...base, background: INK, borderRadius: `0 0 ${mouth.r}px ${mouth.r}px` };
    case "neutral":
      return { ...base, background: INK, borderRadius: mouth.r };
  }
}

export function Face({ preset, bg, onClick, style }: FaceProps) {
  const spec: FaceSpec = FACE[preset];
  const eye: CSSProperties = {
    position: "absolute",
    top: spec.eye.top,
    width: spec.eye.size,
    height: spec.eye.size,
    borderRadius: "50%",
    background: INK,
  };
  return (
    <div
      onClick={onClick}
      style={{
        position: "relative",
        width: spec.size,
        height: spec.size,
        borderRadius: spec.radius,
        background: bg ?? spec.bg,
        border: spec.border,
        flexShrink: 0,
        cursor: onClick ? "pointer" : undefined,
        ...style,
      }}
    >
      <div style={{ ...eye, left: spec.eye.x }} />
      <div style={{ ...eye, right: spec.eye.x }} />
      <div style={mouthStyle(spec.mouth)} />
      {spec.brows && (
        <>
          <div
            style={{
              position: "absolute",
              left: spec.brows.x,
              top: spec.brows.top,
              width: spec.brows.w,
              height: spec.brows.h,
              background: INK,
              borderRadius: 2,
              transform: `rotate(${spec.brows.deg}deg)`,
            }}
          />
          <div
            style={{
              position: "absolute",
              right: spec.brows.x,
              top: spec.brows.top,
              width: spec.brows.w,
              height: spec.brows.h,
              background: INK,
              borderRadius: 2,
              transform: `rotate(-${spec.brows.deg}deg)`,
            }}
          />
        </>
      )}
      {spec.blush && (
        <>
          <div
            style={{
              position: "absolute",
              left: spec.blush.x,
              top: spec.blush.top,
              width: spec.blush.w,
              height: spec.blush.h,
              borderRadius: "50%",
              background: "rgba(245,184,203,0.75)",
            }}
          />
          <div
            style={{
              position: "absolute",
              right: spec.blush.x,
              top: spec.blush.top,
              width: spec.blush.w,
              height: spec.blush.h,
              borderRadius: "50%",
              background: "rgba(245,184,203,0.75)",
            }}
          />
        </>
      )}
    </div>
  );
}
