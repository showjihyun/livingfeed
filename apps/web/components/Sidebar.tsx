import { memo, useState } from "react";

import { ICON, NAV_DEFS } from "@/lib/data";
import { playerName } from "@/lib/config";
import { useLocale, useMessages, type Locale } from "@/lib/i18n";
import type { Tab } from "@/lib/types";
import { useWorldClock } from "@/lib/world-clock";
import { useDemoWorldTime } from "@/lib/world-clock-display";
import { COLOR, WEIGHT, RADIUS } from "@/lib/tokens";

import { Face } from "./Face";
import { Icon } from "./Icon";
import { Pressable } from "./Pressable";
import { SettingsPanel } from "./SettingsPanel";
import { WorldClockDial } from "./WorldClockDial";
import styles from "./lf.module.css";

const en = {
  nav: {
    feed: "World Feed",
    community: "Communities",
    profile: "Profile",
    dm: "Inbox",
    graph: "Relationship Graph",
    hidden: "Hidden Feed",
    studio: "Studio",
  } as Record<Tab, string>,
  hiddenLocked: "Unlocks with trust",
  creatorTools: "Creator tools",
  worldTime: "World time",
  speedNote: "Flowing at 4× real time",
  meddler: (n: number) => `Meddler · ${n} intervention${n === 1 ? "" : "s"}`,
  settings: "Settings",
};
const M: Record<Locale, typeof en> = {
  en,
  ko: {
    nav: {
      feed: "World Feed",
      community: "커뮤니티",
      profile: "프로필",
      dm: "받은 것",
      graph: "관계 그래프",
      hidden: "Hidden Feed",
      studio: "스튜디오",
    } as Record<Tab, string>,
    hiddenLocked: "신뢰로 언락",
    creatorTools: "창조자 도구",
    worldTime: "세계 시간",
    speedNote: "현실의 4배속으로 흐르는 중",
    meddler: (n) => `참견러 · 개입 ${n}회`,
    settings: "설정",
  },
};

interface SidebarProps {
  tab: Tab;
  onSelectTab: (tab: Tab) => void;
  dmBadge: string;
  hiddenUnlocked: boolean;
  interventions: number;
}

// 항상 마운트되어 루트 리렌더마다 다시 그려졌다 — props가 안정적이라 memo가 잘 먹는다
// (locale 변경은 컨텍스트 경유라 memo를 지나 리렌더된다)
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
  const t = useMessages(M);
  const { locale } = useLocale();  // 표시명이 UI 언어를 따른다 (언어 전환은 설정 패널)
  const [settingsOpen, setSettingsOpen] = useState(false);
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
        <div style={{ fontSize: 16, fontWeight: WEIGHT.heavy }}>LivingFeed</div>
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
              borderRadius: RADIUS.sm,
              background: active ? COLOR.white : undefined,
              color: active ? COLOR.primaryDeep : COLOR.muted,
              boxShadow: active ? "0 4px 12px rgba(109,141,214,0.12)" : "none",
            }}
          >
            <Icon d={nav.iconD} size={18} />
            <div style={{ fontSize: 15, fontWeight: WEIGHT.bold, flex: 1 }}>{t.nav[nav.key]}</div>
            {badge && (
              <div
                style={{
                  minWidth: 20,
                  height: 20,
                  padding: "0 6px",
                  background: "#F5B8CB",
                  color: "#7D3D55",
                  borderRadius: RADIUS.pill,
                  fontSize: 11,
                  fontWeight: WEIGHT.heavy,
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
            borderRadius: RADIUS.sm,
            color: COLOR.fainter,
          }}
        >
          <Icon d={ICON.lock} size={18} />
          <div style={{ fontSize: 15, fontWeight: WEIGHT.semibold, flex: 1 }}>Hidden Feed</div>
          <div style={{ fontSize: 11, fontWeight: WEIGHT.bold }}>{t.hiddenLocked}</div>
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
            borderRadius: RADIUS.sm,
            background: tab === "hidden" ? COLOR.white : undefined,
            color: tab === "hidden" ? COLOR.violet : COLOR.muted,
            boxShadow: tab === "hidden" ? "0 4px 12px rgba(122,104,179,0.14)" : "none",
          }}
        >
          <Icon d={ICON.eye} size={18} />
          <div style={{ fontSize: 15, fontWeight: WEIGHT.bold, flex: 1 }}>Hidden Feed</div>
          <div
            style={{
              padding: "1px 8px",
              background: "#C9B8F0",
              color: "#4a3b7a",
              borderRadius: RADIUS.pill,
              fontSize: 10,
              fontWeight: WEIGHT.heavy,
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
          fontWeight: WEIGHT.heavy,
          color: COLOR.fainter,
          letterSpacing: 0.5,
        }}
      >
        {t.creatorTools}
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
          borderRadius: RADIUS.sm,
          background: tab === "studio" ? COLOR.white : undefined,
          color: tab === "studio" ? "#A97E2F" : COLOR.muted,
          boxShadow: tab === "studio" ? "0 4px 12px rgba(176,132,48,0.14)" : "none",
        }}
      >
        <Icon d={ICON.wrench} size={18} />
        <div style={{ fontSize: 15, fontWeight: WEIGHT.bold, flex: 1 }}>{t.nav.studio}</div>
      </Pressable>

      <div style={{ marginTop: "auto", display: "flex", flexDirection: "column", gap: 12 }}>
        <div
          style={{
            background: COLOR.white,
            borderRadius: RADIUS.md,
            padding: "14px 16px",
            display: "flex",
            flexDirection: "column",
            gap: 4,
            boxShadow: "0 4px 12px rgba(109,141,214,0.10)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {/* 멈춘 시계 아이콘 대신 도는 바늘 — 숫자는 세계 1분(실시간 15초)마다
                한 번 바뀌어서, 그 사이 흐름이 보이지 않았다 */}
            <WorldClockDial size={15} />
            <div style={{ fontSize: 12, fontWeight: WEIGHT.heavy, color: COLOR.faint }}>
              {t.worldTime}
            </div>
          </div>
          {/* key로 분이 바뀔 때마다 다시 마운트해 강세를 재생한다 — 넘어간 순간이 보인다 */}
          <div
            key={clock}
            className="lf-tickover"
            style={{ fontSize: 19, fontWeight: WEIGHT.heavy }}
          >
            {clock}
          </div>
          <div style={{ fontSize: 12, color: COLOR.faint, fontWeight: WEIGHT.semibold }}>
            {t.speedNote}
          </div>
        </div>

        {/* 설정 — 언어(이 브라우저)와 LLM API 비용 상한(세계에 걸리는 값)은 한
            패널에서 다룬다. 사이드바 폭(252px)에 숫자 입력·사용량 게이지를 넣을
            자리가 없어 오버레이다 (온보딩과 같은 결) */}
        {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
        <Pressable
          onClick={() => setSettingsOpen((open) => !open)}
          aria-expanded={settingsOpen}
          className={styles.navItem}
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "9px 14px",
            borderRadius: RADIUS.sm,
            background: settingsOpen ? COLOR.white : undefined,
            color: settingsOpen ? COLOR.primaryDeep : COLOR.muted,
            boxShadow: settingsOpen ? "0 4px 12px rgba(109,141,214,0.12)" : "none",
          }}
        >
          <Icon d={ICON.gear} size={16} />
          <div style={{ fontSize: 13, fontWeight: WEIGHT.bold, flex: 1 }}>{t.settings}</div>
        </Pressable>

        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 10px" }}>
          <Face preset="user30" />
          <div style={{ display: "flex", flexDirection: "column" }}>
            <div style={{ fontSize: 14, fontWeight: WEIGHT.heavy }}>{playerName(locale)}</div>
            <div style={{ fontSize: 11, color: COLOR.faint, fontWeight: WEIGHT.semibold }}>
              {t.meddler(interventions)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
