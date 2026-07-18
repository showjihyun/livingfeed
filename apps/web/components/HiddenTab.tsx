import { ICON } from "@/lib/data";
import type { HiddenItem } from "@/lib/hidden";
import { relativeTime } from "@/lib/live-feed";
import type { Range } from "@/lib/range";

import { Icon } from "./Icon";
import { RangeChips } from "./RangeChips";

const AVATAR_COLORS = ["#CBBDE8", "#AFC8F5", "#F2B8CF", "#BFE3CF", "#E8D5A8"];

function avatarColor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

interface HiddenTabProps {
  items: HiddenItem[];
  /** actor_id → 표시 이름 (라이브 identity, 하드코딩 금지) */
  nameOf: (actorId: string) => string;
  /** 조회 범위(세계 시간) — 전체/오늘/이번 주/이번 달 (lib/range) */
  range: Range;
  onRangeChange: (range: Range) => void;
}

export function HiddenTab({ items, nameOf, range, onRangeChange }: HiddenTabProps) {
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
          <div style={{ fontSize: 20, fontWeight: 800 }}>Hidden Feed</div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              background: "#F1EDFB",
              color: "#7a68b3",
              borderRadius: 9999,
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            <Icon d={ICON.eye} size={12} /> 당신에게만 보여요
          </div>
        </div>
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
          당신에게만 닿은 이야기 · {items.length}개
        </div>
      </div>

      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "24px 32px",
          display: "flex",
          flexDirection: "column",
          gap: 16,
          background: "#FBFAFE",
        }}
      >
        {/* 조회 범위 — 세계 시간의 창 (목록 상단, 모든 탭 공통 자리) */}
        <RangeChips value={range} onChange={onRangeChange} />

        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600, lineHeight: 1.6 }}>
          세계에는 항상 보이는 것보다 많은 일이 일어나요. 액터가 당신에게만 비공개로 건넨
          이야기입니다 — 여기서 알게 된 것을 어떻게 쓸지는 당신의 선택이에요.
        </div>

        {items.length === 0 ? (
          <div
            style={{
              border: "1.5px dashed #D9D2EC",
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
            {range !== "all" ? (
              <>이 기간엔 당신에게만 닿은 이야기가 없어요 — 범위를 넓히면 지난 비밀이 보여요.</>
            ) : (
              <>
                아직 당신에게만 닿은 이야기가 없어요.
                <br />
                신뢰를 쌓으면, 액터가 남들에게 못 하는 말을 당신에게 건넵니다.
              </>
            )}
          </div>
        ) : (
          items.map((item) => {
            const name = nameOf(item.actorId);
            return (
              <div
                key={item.id}
                style={{
                  border: "1.5px solid #E0D8F5",
                  background: "#fff",
                  borderRadius: 20,
                  padding: "20px 24px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 12,
                  boxShadow: "0 8px 24px rgba(122,104,179,0.10)",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div
                      style={{
                        width: 44,
                        height: 44,
                        borderRadius: "50%",
                        background: avatarColor(item.actorId),
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        fontSize: 17,
                        fontWeight: 800,
                        color: "#3A4256",
                        flexShrink: 0,
                      }}
                    >
                      {name.slice(0, 1).toUpperCase()}
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ fontSize: 15, fontWeight: 800 }}>{name}</div>
                        <div
                          style={{
                            padding: "2px 10px",
                            background: "#F1EDFB",
                            color: "#7a68b3",
                            borderRadius: 9999,
                            fontSize: 11,
                            fontWeight: 800,
                          }}
                        >
                          비공개 · 당신만
                        </div>
                      </div>
                      <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
                        {relativeTime(item.occurredAt)}
                      </div>
                    </div>
                  </div>
                  <Icon d={ICON.lockOpen} size={18} color="#7a68b3" />
                </div>
                <div style={{ fontSize: 15, lineHeight: 1.65, fontWeight: 500 }}>{item.body}</div>
              </div>
            );
          })
        )}
      </div>
    </>
  );
}
