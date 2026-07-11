import { ICON, MINJI_TIMELINE } from "@/lib/data";

import { Face } from "./Face";
import { Icon } from "./Icon";
import styles from "./lf.module.css";

interface ProfileTabProps {
  following: boolean;
  onToggleFollow: () => void;
  goDm: () => void;
}

export function ProfileTab({ following, onToggleFollow, goDm }: ProfileTabProps) {
  return (
    <div style={{ flex: 1, overflowY: "auto" }}>
      <div style={{ height: 110, background: "#EDF3FD" }} />
      <div
        style={{
          padding: "0 40px 40px",
          display: "flex",
          flexDirection: "column",
          gap: 20,
        }}
      >
        <div style={{ display: "flex", alignItems: "flex-end", gap: 20, marginTop: -40 }}>
          <Face preset="profile88" />
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 5,
              flex: 1,
              paddingBottom: 4,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <div style={{ fontSize: 26, fontWeight: 900 }}>김민지</div>
              <div
                style={{
                  padding: "3px 12px",
                  background: "#EDF3FD",
                  color: "#5F7EC9",
                  borderRadius: 9999,
                  fontSize: 12,
                  fontWeight: 800,
                }}
              >
                당신과 친한 사이
              </div>
            </div>
            <div style={{ fontSize: 14, color: "#6B7691", fontWeight: 600 }}>
              28세 · 스타트업 마케터 · 밤에 글을 쓴다
            </div>
            <div
              style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600, fontStyle: "italic" }}
            >
              요즘 마음이 복잡해 보여요 — 회사 이야기를 꺼낼 때 말이 짧아져요
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, paddingBottom: 8 }}>
            <div
              onClick={onToggleFollow}
              className={styles.press95}
              style={{
                padding: "10px 20px",
                background: following ? "#F2F6FC" : "#6D8DD6",
                color: following ? "#5F7EC9" : "#ffffff",
                borderRadius: 9999,
                fontSize: 14,
                fontWeight: 800,
                cursor: "pointer",
              }}
            >
              {following ? "팔로잉" : "팔로우"}
            </div>
            <div
              onClick={goDm}
              className={styles.press95}
              style={{
                padding: "10px 20px",
                background: "#6D8DD6",
                color: "#fff",
                borderRadius: 9999,
                fontSize: 14,
                fontWeight: 800,
                cursor: "pointer",
              }}
            >
              DM 보내기
            </div>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "13px 18px",
            background: "#FFF6DE",
            borderRadius: 16,
          }}
        >
          <Icon d={ICON.journey} size={18} color="#A87F24" />
          <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
            <span style={{ fontWeight: 800, color: "#3A4256" }}>당신과의 역사</span> · 3월 2일 첫
            댓글 → 아는 사이 · 3월 8일 DM으로 조언 →{" "}
            <span style={{ fontWeight: 800, color: "#3A4256" }}>친한 사이</span> · 민지는 당신의
            조언을 기억하고 있어요
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {MINJI_TIMELINE.map((tp) => (
            <div
              key={tp.meta}
              style={{
                border: "1.5px solid #E2EAF6",
                borderRadius: 18,
                padding: "16px 20px",
                display: "flex",
                flexDirection: "column",
                gap: 8,
              }}
            >
              <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 700 }}>{tp.meta}</div>
              <div style={{ fontSize: 14, lineHeight: 1.65, fontWeight: 500 }}>{tp.text}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
