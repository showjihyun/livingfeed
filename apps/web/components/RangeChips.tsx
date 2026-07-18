"use client";

/**
 * 조회 범위 칩 — 전체 / 오늘 / 이번 주 / 이번 달 (세계 시간 기준, lib/range).
 *
 * 어느 탭에서든 같은 자리(목록 상단)·같은 모양으로 쓴다 — 스튜디오 ChipSelect·
 * 검색과 같은 결의 알약 칩. 선택 색만 앱 공통 파랑이다 (범위는 탭 정체성이 아니라
 * 공용 조회 조건이므로 탭별 색을 입히지 않는다).
 */

import { RANGE_LABELS, RANGES, type Range } from "@/lib/range";

import styles from "./lf.module.css";

export function RangeChips({
  value,
  onChange,
}: {
  value: Range;
  onChange: (range: Range) => void;
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {RANGES.map((range) => {
        const selected = range === value;
        return (
          <div
            key={range}
            onClick={() => onChange(range)}
            className={styles.press95}
            style={{
              padding: "4px 12px",
              borderRadius: 9999,
              fontSize: 12,
              fontWeight: 800,
              cursor: "pointer",
              background: selected ? "#EDF3FD" : "#F2F6FC",
              color: selected ? "#5F7EC9" : "#8C97AF",
              border: `1.5px solid ${selected ? "#CFE0F8" : "transparent"}`,
            }}
          >
            {RANGE_LABELS[range]}
          </div>
        );
      })}
    </div>
  );
}
