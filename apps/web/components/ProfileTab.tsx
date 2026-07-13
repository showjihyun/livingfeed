import { ICON } from "@/lib/data";
import { relativeTime } from "@/lib/live-feed";
import type { ActorProfile } from "@/lib/profile";
import { humanize } from "@/lib/profile";

import { Face } from "./Face";
import { Icon } from "./Icon";
import styles from "./lf.module.css";

interface ProfileTabProps {
  following: boolean;
  onToggleFollow: () => void;
  goDm: () => void;
  /** pg-projector 실측 내면 (ADR-003/008) — null이면 데모 서사를 유지한다 */
  profile: ActorProfile | null;
}

export function ProfileTab({ following, onToggleFollow, goDm, profile }: ProfileTabProps) {
  const aboutMe = profile?.aboutMe ?? [];
  const episodes = profile?.episodes ?? [];
  const identity = profile?.identity ?? null;
  // 표시 이름·소개·목표는 라이브 identity에서 — 없으면 중립 문구 (특정 인물 하드코딩 금지)
  const displayName = identity?.name ?? "프로필";
  const displayBio = identity?.bio ?? "아직 소개가 연결되지 않았어요";
  const topGoal = identity?.goals.slice().sort((a, b) => b.priority - a.priority)[0];
  const tagline = topGoal ? `요즘 몰두하는 것 — ${topGoal.description}` : "";
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
              <div style={{ fontSize: 26, fontWeight: 900 }}>{displayName}</div>
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
                당신이 지켜보는 사람
              </div>
            </div>
            <div style={{ fontSize: 14, color: "#6B7691", fontWeight: 600 }}>{displayBio}</div>
            {tagline && (
              <div
                style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600, fontStyle: "italic" }}
              >
                {tagline}
              </div>
            )}
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
          {aboutMe.length > 0 ? (
            // 실측 — 민지가 나에 대해 실제로 형성한 신념 (reflection, ADR-008)
            <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
              <span style={{ fontWeight: 800, color: "#3A4256" }}>민지의 마음속</span>
              {aboutMe.map((b) => (
                <span key={b.kind}>
                  {" · "}
                  {humanize(b.statement)}{" "}
                  <span style={{ color: "#A87F24", fontWeight: 700 }}>
                    (확신 {Math.round(b.confidence * 100)}%
                    {b.revisions > 1 ? ` · ${b.revisions}번 곱씹음` : ""})
                  </span>
                </span>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
              <span style={{ fontWeight: 800, color: "#3A4256" }}>당신과의 역사</span> · 3월 2일 첫
              댓글 → 아는 사이 · 3월 8일 DM으로 조언 →{" "}
              <span style={{ fontWeight: 800, color: "#3A4256" }}>친한 사이</span> · 민지는 당신의
              조언을 기억하고 있어요
            </div>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontSize: 14, fontWeight: 800 }}>민지의 기억</div>
          <div
            style={{
              fontSize: 11,
              fontWeight: 800,
              color: episodes.length > 0 ? "#3E8A66" : "#8C97AF",
            }}
          >
            {episodes.length > 0 ? "기억 실측 연결됨" : "아직 비어 있음"}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {episodes.length > 0 ? (
            episodes.map((ep) => (
              <div
                key={ep.id}
                style={{
                  border: "1.5px solid #E2EAF6",
                  borderRadius: 18,
                  padding: "16px 20px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                }}
              >
                <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 700 }}>
                  {relativeTime(ep.occurredAt)} · 마음에 남은 정도 {Math.round(ep.importance * 100)}%
                </div>
                <div style={{ fontSize: 14, lineHeight: 1.65, fontWeight: 500 }}>
                  {humanize(ep.summary)}
                </div>
              </div>
            ))
          ) : (
            <div
              style={{
                border: "1.5px dashed #D8E1F0",
                borderRadius: 18,
                padding: "24px 20px",
                textAlign: "center",
                color: "#8C97AF",
                fontSize: 14,
                fontWeight: 600,
              }}
            >
              아직 쌓인 기억이 없어요 — 함께 겪은 일이 생기면 여기에 남습니다.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
