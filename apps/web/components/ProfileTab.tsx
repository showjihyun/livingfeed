import { ICON } from "@/lib/data";
import { relativeTime } from "@/lib/live-feed";
import type { ActorProfile } from "@/lib/profile";
import { ARC_STAGE_LABELS, FADED_BELIEF_MAX, humanize } from "@/lib/profile";
import type { Range } from "@/lib/range";

import { Face } from "./Face";
import { Icon } from "./Icon";
import { RangeChips } from "./RangeChips";
import styles from "./lf.module.css";

interface ProfileTabProps {
  following: boolean;
  onToggleFollow: () => void;
  goDm: () => void;
  /** pg-projector 실측 내면 (ADR-003/008) — null이면 데모 서사를 유지한다 */
  profile: ActorProfile | null;
  /** 겪은 일(에피소드)의 조회 범위 — 세계 시간의 창 (lib/range) */
  episodeRange: Range;
  onEpisodeRangeChange: (range: Range) => void;
  /** 더 과거 기억 페이지가 남아있는가 — 없으면 버튼을 숨긴다 */
  hasMoreEpisodes: boolean;
  loadingEpisodes: boolean;
  onLoadMoreEpisodes: () => void;
}

export function ProfileTab({
  following,
  onToggleFollow,
  goDm,
  profile,
  episodeRange,
  onEpisodeRangeChange,
  hasMoreEpisodes,
  loadingEpisodes,
  onLoadMoreEpisodes,
}: ProfileTabProps) {
  const aboutMe = profile?.aboutMe ?? [];
  const episodes = profile?.episodes ?? [];
  const identity = profile?.identity ?? null;
  const arc = profile?.arc ?? null;
  // 지난 장들 (연대기, 오래된 순) — 마지막 항목은 현재 장이라 카드가 이미 보여준다
  const pastChapters = (profile?.arcHistory ?? []).slice(0, -1);
  // 표시 이름·소개·목표는 라이브 identity에서 — 없으면 중립 문구 (특정 인물 하드코딩 금지)
  // 정체성 실측이 아직 없으면 중립 지칭 — 문장 속에서도 어색하지 않아야 한다
  const displayName = identity?.name ?? "이 사람";
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
            // 실측 — 이 액터가 나에 대해 실제로 형성한 신념 (reflection, ADR-008)
            <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
              <span style={{ fontWeight: 800, color: "#3A4256" }}>{displayName}의 마음속</span>
              {aboutMe.map((b) =>
                b.confidence <= FADED_BELIEF_MAX ? (
                  // 철회된 신념 — 잔불로 남은 흔적 (ADR-008 신념 폐기)
                  <span key={b.kind} style={{ color: "#B7C2D8", fontStyle: "italic" }}>
                    {" · "}
                    {humanize(b.statement)}{" "}
                    <span style={{ fontWeight: 700 }}>(흐려진 믿음)</span>
                  </span>
                ) : (
                  <span key={b.kind}>
                    {" · "}
                    {humanize(b.statement)}{" "}
                    <span style={{ color: "#A87F24", fontWeight: 700 }}>
                      (확신 {Math.round(b.confidence * 100)}%
                      {b.revisions > 1 ? ` · ${b.revisions}번 곱씹음` : ""})
                    </span>
                  </span>
                ),
              )}
            </div>
          ) : (
            // 실측이 없으면 없다고 말한다 — 가공의 역사를 그리지 않는다
            <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
              <span style={{ fontWeight: 800, color: "#3A4256" }}>당신과의 역사</span> · 아직
              형성된 마음이 없어요 — 댓글이나 DM이 쌓이면 {displayName}이(가) 당신을
              곱씹기 시작합니다
            </div>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontSize: 14, fontWeight: 800 }}>인생의 장</div>
          <div
            style={{
              fontSize: 11,
              fontWeight: 800,
              color: arc ? "#3E8A66" : "#8C97AF",
            }}
          >
            {arc ? "아크 실측 연결됨" : "아직 비어 있음"}
          </div>
        </div>

        {arc ? (
          // Director가 그린 이번 시즌의 인생 방향 (ADR-013, plan/08) — 명령이 아니라 배경
          <div
            style={{
              border: "1.5px solid #E2EAF6",
              borderRadius: 18,
              padding: "16px 20px",
              display: "flex",
              flexDirection: "column",
              gap: 8,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
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
                {ARC_STAGE_LABELS[arc.stage] ?? arc.stage}
              </div>
              <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 700 }}>
                {relativeTime(arc.plannedAt)} 그려진 방향
              </div>
            </div>
            <div style={{ fontSize: 14, lineHeight: 1.65, fontWeight: 500 }}>
              {humanize(arc.intention)}
            </div>
            {pastChapters.length > 0 && (
              // 인생의 연대기 — 여기까지 온 장들의 흔적 (오래된 순)
              <div
                style={{
                  borderTop: "1px solid #EEF2FA",
                  paddingTop: 10,
                  display: "flex",
                  flexDirection: "column",
                  gap: 5,
                }}
              >
                {pastChapters.map((chapter, i) => (
                  <div
                    key={`${chapter.plannedAt}-${i}`}
                    style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}
                  >
                    <span style={{ color: "#7B62C9", fontWeight: 800 }}>
                      {ARC_STAGE_LABELS[chapter.stage] ?? chapter.stage}
                    </span>
                    {" — "}
                    {humanize(chapter.intention)}
                  </div>
                ))}
              </div>
            )}
          </div>
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
            아직 그려진 인생의 장이 없어요 — 지금은 그저 일상을 살고 있습니다.
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontSize: 14, fontWeight: 800 }}>{displayName}의 기억</div>
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
          {/* 조회 범위 — 겪은 일 목록 상단 (모든 탭 공통 칩, 세계 시간 기준) */}
          <RangeChips value={episodeRange} onChange={onEpisodeRangeChange} />
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
              {episodeRange !== "all"
                ? "이 기간의 기억이 없어요 — 범위를 넓히면 지난 날들이 보여요."
                : "아직 쌓인 기억이 없어요 — 함께 겪은 일이 생기면 여기에 남습니다."}
            </div>
          )}
          {episodes.length > 0 && hasMoreEpisodes && (
            // 과거 방향 커서 페이지네이션 — 오래된 기억을 목록 아래에 이어 붙인다
            <div
              onClick={loadingEpisodes ? undefined : onLoadMoreEpisodes}
              className={styles.press95}
              style={{
                alignSelf: "center",
                padding: "9px 22px",
                background: "#F2F6FC",
                color: "#5F7EC9",
                borderRadius: 9999,
                fontSize: 13,
                fontWeight: 800,
                cursor: loadingEpisodes ? "default" : "pointer",
                opacity: loadingEpisodes ? 0.6 : 1,
              }}
            >
              {loadingEpisodes ? "기억을 꺼내는 중..." : "기억 더 보기"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
