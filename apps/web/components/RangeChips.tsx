"use client";

/**
 * 조회 범위 칩 — 전체 / 오늘 / 이번 주 / 이번 달 (세계 시간 기준, lib/range).
 *
 * 어느 탭에서든 같은 자리(목록 상단)·같은 모양으로 쓴다 — 스튜디오 ChipSelect·
 * 검색과 같은 결의 알약 칩. 선택 색만 앱 공통 파랑이다 (범위는 탭 정체성이 아니라
 * 공용 조회 조건이므로 탭별 색을 입히지 않는다).
 */

import { useMessages, type Locale } from "@/lib/i18n";
import { RANGES, type Range } from "@/lib/range";
import { COLOR, RADIUS, WEIGHT } from "@/lib/tokens";

import { Pressable } from "./Pressable";
import styles from "./lf.module.css";

const en: Record<Range, string> = {
  all: "All",
  today: "Today",
  week: "This week",
  month: "This month",
};
const M: Record<Locale, typeof en> = {
  en,
  ko: { all: "전체", today: "오늘", week: "이번 주", month: "이번 달" },
};

export function RangeChips({
  value,
  onChange,
}: {
  value: Range;
  onChange: (range: Range) => void;
}) {
  const t = useMessages(M);
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {RANGES.map((range) => {
        const selected = range === value;
        return (
          <Pressable
            key={range}
            onClick={() => onChange(range)}
            aria-pressed={selected}
            className={styles.press95}
            style={{
              padding: "4px 12px",
              borderRadius: RADIUS.pill,
              fontSize: 12,
              fontWeight: WEIGHT.heavy,
              background: selected ? COLOR.primarySoft : COLOR.surface,
              color: selected ? COLOR.primaryDeep : COLOR.faint,
              border: `1.5px solid ${selected ? "#CFE0F8" : "transparent"}`,
            }}
          >
            {t[range]}
          </Pressable>
        );
      })}
    </div>
  );
}
