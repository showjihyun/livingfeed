import { ICON, NAV_DEFS } from "@/lib/data";
import type { Tab } from "@/lib/types";

import { Face } from "./Face";
import { Icon } from "./Icon";
import styles from "./lf.module.css";

interface SidebarProps {
  tab: Tab;
  onSelectTab: (tab: Tab) => void;
  dmBadge: string;
  hiddenUnlocked: boolean;
  worldTime: string;
  interventions: number;
}

export function Sidebar({
  tab,
  onSelectTab,
  dmBadge,
  hiddenUnlocked,
  worldTime,
  interventions,
}: SidebarProps) {
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
          <div
            key={nav.key}
            onClick={() => onSelectTab(nav.key)}
            className={styles.navItem}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "11px 14px",
              borderRadius: 14,
              cursor: "pointer",
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
          </div>
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
        <div
          onClick={() => onSelectTab("hidden")}
          className={styles.navItem}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "11px 14px",
            borderRadius: 14,
            cursor: "pointer",
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
        </div>
      )}

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
          <div style={{ fontSize: 19, fontWeight: 800 }}>{worldTime}</div>
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
