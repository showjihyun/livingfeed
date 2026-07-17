"use client";

/**
 * 개인 다이제스트 오버레이 — "당신이 없는 동안" (plan/11 §D7·§휴면 복귀).
 *
 * 입장 직후 부재가 임계(lib/digest ABSENCE_THRESHOLD_MS) 이상이면 피드 위에
 * 변한 세계의 증명을 한 장 띄운다 — 재시작이 아니라 재회다. 사과·보상·죄책감
 * 문구는 싣지 않는다 (plan/11 §알림 정책 다크 패턴 금지).
 * 실릴 것이 하나도 없거나 백엔드 미가용이면 아무것도 띄우지 않고,
 * "세계로 돌아가기"로 닫는 순간에만 lastSeen을 갱신한다.
 */

import { useEffect, useState } from "react";

import type { DigestLine, PersonalDigest } from "@/lib/digest";
import {
  ABSENCE_THRESHOLD_MS,
  digestSentence,
  loadDigest,
  markSeen,
  readLastSeen,
} from "@/lib/digest";

/** 갈래별 결 — 저자성(보라)·나를 향한 것(분홍)·세계의 마디(파랑), 기존 팔레트 그대로 */
const SECTION_STYLE = {
  yours: { label: "당신이 빚은 인물", color: "#8A63D2", bg: "#FBFAFE", border: "#E9E1F8" },
  toYou: { label: "당신에게 닿은 것", color: "#C76F93", bg: "#FDF7FA", border: "#F4DEE8" },
  world: { label: "세계의 마디", color: "#6D8DD6", bg: "#F7FAFE", border: "#E2EAF6" },
} as const;

function Section({
  kind,
  lines,
  more,
  nameOf,
}: {
  kind: keyof typeof SECTION_STYLE;
  lines: DigestLine[];
  more?: number;
  nameOf: (actorId: string) => string;
}) {
  if (lines.length === 0) return null;
  const s = SECTION_STYLE[kind];
  return (
    <div
      style={{
        background: s.bg,
        border: `1.5px solid ${s.border}`,
        borderRadius: 16,
        padding: "14px 18px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ fontSize: 11, fontWeight: 800, color: s.color, letterSpacing: 0.4 }}>
        {s.label}
      </div>
      {lines.map((line, i) => (
        <div key={i} style={{ display: "flex", gap: 9, alignItems: "flex-start" }}>
          <div
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: s.color,
              marginTop: 7,
              flexShrink: 0,
            }}
          />
          <div style={{ fontSize: 14, lineHeight: 1.6, fontWeight: 600, color: "#3A4256" }}>
            {digestSentence(line, nameOf)}
          </div>
        </div>
      ))}
      {(more ?? 0) > 0 && (
        <div style={{ fontSize: 12.5, fontWeight: 600, color: "#8C97AF", paddingLeft: 15 }}>
          외 {more}건이 더 있어요
        </div>
      )}
    </div>
  );
}

export function Digest({ nameOf }: { nameOf: (actorId: string) => string }) {
  const [digest, setDigest] = useState<PersonalDigest | null>(null);

  useEffect(() => {
    const lastSeen = readLastSeen();
    if (lastSeen === null) {
      // 첫 입장 — 보여줄 부재가 없다. 다음 재회를 위한 기준점만 심는다.
      markSeen();
      return;
    }
    if (Date.now() - lastSeen.ts < ABSENCE_THRESHOLD_MS) return; // 잠깐 자리 비움은 재회가 아니다
    let cancelled = false;
    void loadDigest(lastSeen).then((d) => {
      if (cancelled || d === null) return;
      if (d.total === 0) {
        markSeen(d.newestUlid); // 새 마디가 없다 — 카드 없이 기준점만 옮긴다
        return;
      }
      setDigest(d);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (digest === null) return null;

  const close = () => {
    markSeen(digest.newestUlid); // '확인'의 순간이 곧 lastSeen이다
    setDigest(null);
  };

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        zIndex: 45,
        background: "rgba(58,66,86,0.38)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          width: 560,
          maxHeight: "84vh",
          overflowY: "auto",
          background: "#fff",
          border: "1.5px solid #E2EAF6",
          borderRadius: 28,
          padding: "36px 40px",
          display: "flex",
          flexDirection: "column",
          gap: 18,
          boxShadow: "0 16px 48px rgba(109,141,214,0.28)",
          animation: "lf-pop 0.35s ease-out",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ fontSize: 26, fontWeight: 900, lineHeight: 1.3 }}>당신이 없는 동안</div>
          <div style={{ fontSize: 14, color: "#6B7691", lineHeight: 1.6, fontWeight: 500 }}>
            세계는 계속 흐르고 있었어요 — 그동안의 마디를 모아왔어요.
          </div>
        </div>

        <Section kind="yours" lines={digest.yours} nameOf={nameOf} />
        <Section kind="toYou" lines={digest.toYou} more={digest.toYouMore} nameOf={nameOf} />
        <Section kind="world" lines={digest.world} nameOf={nameOf} />

        <div
          onClick={close}
          style={{
            marginTop: 4,
            padding: "13px 18px",
            background: "#6D8DD6",
            color: "#fff",
            borderRadius: 9999,
            fontSize: 15,
            fontWeight: 800,
            textAlign: "center",
            cursor: "pointer",
            userSelect: "none",
          }}
        >
          세계로 돌아가기
        </div>
      </div>
    </div>
  );
}
