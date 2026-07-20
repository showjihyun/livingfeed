import { memo } from "react";

import { ICON, NAV_DEFS } from "@/lib/data";
import type { Tab } from "@/lib/types";
import { useWorldClock } from "@/lib/world-clock";
import { useDemoWorldTime } from "@/lib/world-clock-display";

import { Face } from "./Face";
import { Icon } from "./Icon";
import { Pressable } from "./Pressable";
import styles from "./lf.module.css";

interface SidebarProps {
  tab: Tab;
  onSelectTab: (tab: Tab) => void;
  dmBadge: string;
  hiddenUnlocked: boolean;
  interventions: number;
}

// 항상 마운트되어 루트 리렌더마다 다시 그려졌다 — props가 안정적이라 memo가 잘 먹는다
export const Sidebar = memo(SidebarInner);

function SidebarInner({
  tab,
  onSelectTab,
  dmBadge,
  hiddenUnlocked,
  interventions,
}: SidebarProps) {
  // 세계 시간의 진실은 엔진 tick이다 — 앵커 관측 전(연결 전)에는 데모 폴백 시계
  const clock = useWorldClock(useDemoWorldTime());
  return (
    <div
      style={{
        width: 252,
        padding: "24px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        flexShrink: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "6px 10px",
          marginBottom: 12,
        }}
      >
        <Face preset="logo30" />
        <div style={{ fontSize: 16, fontWeight: 800 }}>LivingFeed</div>
      </div>

      {NAV_DEFS.map((nav) => {
        const active = tab === nav.key;
        const badge = nav.key === "dm" ? dmBadge : "";
        return (
          <Pressable
            key={nav.key}
            onClick={() => onSelectTab(nav.key)}
            aria-current={active ? "page" : undefined}
            className={styles.navItem}
            style={{
              width: "100%",
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "11px 14px",
              borderRadius: 14,
              background: active ? "#ffffff" : undefined,
              color: active ? "#5F7EC9" : "#6B7691",
              boxShadow: active ? "0 4px 12px rgba(109,141,214,0.12)" : "none",
            }}
          >
            <Icon d={nav.iconD} size={18} />
            <div style={{ fontSize: 15, fontWeight: 700, flex: 1 }}>{nav.label}</div>
            {badge && (
              <div
                style={{
                  minWidth: 20,
                  height: 20,
                  padding: "0 6px",
                  background: "#F5B8CB",
                  color: "#7D3D55",
                  borderRadius: 9999,
                  fontSize: 11,
                  fontWeight: 800,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {badge}
              </div>
            )}
          </Pressable>
        );
      })}

      {!hiddenUnlocked ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "11px 14px",
            borderRadius: 14,
            color: "#A9B2C7",
          }}
        >
          <Icon d={ICON.lock} size={18} />
          <div style={{ fontSize: 15, fontWeight: 600, flex: 1 }}>Hidden Feed</div>
          <div style={{ fontSize: 11, fontWeight: 700 }}>신뢰로 언락</div>
        </div>
      ) : (
        <Pressable
          onClick={() => onSelectTab("hidden")}
          aria-current={tab === "hidden" ? "page" : undefined}
          className={styles.navItem}
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "11px 14px",
            borderRadius: 14,
            background: tab === "hidden" ? "#ffffff" : undefined,
            color: tab === "hidden" ? "#7a68b3" : "#6B7691",
            boxShadow: tab === "hidden" ? "0 4px 12px rgba(122,104,179,0.14)" : "none",
          }}
        >
          <Icon d={ICON.eye} size={18} />
          <div style={{ fontSize: 15, fontWeight: 700, flex: 1 }}>Hidden Feed</div>
          <div
            style={{
              padding: "1px 8px",
              background: "#C9B8F0",
              color: "#4a3b7a",
              borderRadius: 9999,
              fontSize: 10,
              fontWeight: 800,
            }}
          >
            NEW
          </div>
        </Pressable>
      )}

      {/* 창조자 도구 — 관전 탭들과 결이 다르다: 세계를 보는 곳이 아니라 빚는 곳 (공방 톤) */}
      <div
        style={{
          margin: "10px 10px 4px",
          borderTop: "1.5px solid #E2EAF6",
          paddingTop: 10,
          fontSize: 11,
          fontWeight: 800,
          color: "#A9B2C7",
          letterSpacing: 0.5,
        }}
      >
        창조자 도구
      </div>
      <Pressable
        onClick={() => onSelectTab("studio")}
        aria-current={tab === "studio" ? "page" : undefined}
        className={styles.navItem}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "11px 14px",
          borderRadius: 14,
          background: tab === "studio" ? "#ffffff" : undefined,
          color: tab === "studio" ? "#A97E2F" : "#6B7691",
          boxShadow: tab === "studio" ? "0 4px 12px rgba(176,132,48,0.14)" : "none",
        }}
      >
        <Icon d={ICON.wrench} size={18} />
        <div style={{ fontSize: 15, fontWeight: 700, flex: 1 }}>스튜디오</div>
      </Pressable>

      <div style={{ marginTop: "auto", display: "flex", flexDirection: "column", gap: 12 }}>
        <div
          style={{
            background: "#fff",
            borderRadius: 16,
            padding: "14px 16px",
            display: "flex",
            flexDirection: "column",
            gap: 4,
            boxShadow: "0 4px 12px rgba(109,141,214,0.10)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Icon d={ICON.clock} size={14} color="#8C97AF" />
            <div style={{ fontSize: 12, fontWeight: 800, color: "#8C97AF" }}>세계 시간</div>
          </div>
          <div style={{ fontSize: 19, fontWeight: 800 }}>{clock}</div>
          <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
            현실의 4배속으로 흐르는 중
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 10px" }}>
          <Face preset="user30" />
          <div style={{ display: "flex", flexDirection: "column" }}>
            <div style={{ fontSize: 14, fontWeight: 800 }}>관찰자_0417</div>
            <div style={{ fontSize: 11, color: "#8C97AF", fontWeight: 600 }}>
              참견러 · 개입 {interventions}회
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
