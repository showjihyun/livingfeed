import { memo } from "react";

import { ICON } from "@/lib/data";
import { useMessages, type Locale } from "@/lib/i18n";
import { relativeTime } from "@/lib/live-feed";
import type { ActorProfile, ProvenanceKind } from "@/lib/profile";
import { arcStageLabel, FADED_BELIEF_MAX, humanize } from "@/lib/profile";
import type { Range } from "@/lib/range";
import { COLOR, WEIGHT, RADIUS } from "@/lib/tokens";

import { Face } from "./Face";
import { Icon } from "./Icon";
import { Pressable } from "./Pressable";
import { RangeChips } from "./RangeChips";
import styles from "./lf.module.css";

const en = {
  fallbackName: "Someone",
  fallbackBio: "No introduction connected yet",
  tagline: (goal: string) => `Lately focused on — ${goal}`,
  watchingBadge: "The one you're watching",
  following: "Following",
  follow: "Follow",
  sendDm: "Send a DM",
  innerWorldOf: (name: string) => `Inside ${name}'s mind`,
  fadedBelief: "(faded belief)",
  beliefMeta: (pct: number, revisions: number) =>
    `(confidence ${pct}%${revisions > 1 ? ` · mulled over ${revisions} times` : ""})`,
  historyWithYou: "Your history together",
  noFeelingsYet: (name: string) =>
    `nothing has taken shape yet — as comments and DMs pile up, ${name} will start thinking about you`,
  lifeChapters: "Life chapters",
  arcConnected: "Live arc connected",
  stillEmpty: "Empty for now",
  directionDrawn: (when: string) => `Direction drawn ${when}`,
  noChapterYet: "No life chapter has been drawn yet — for now, they're simply living day to day.",
  memoriesOf: (name: string) => `${name}'s memories`,
  memoriesConnected: "Live memories connected",
  memoryWeight: (pct: number) => `stayed with them ${pct}%`,
  emptyEpisodesRange: "No memories in this period — widen the range to see earlier days.",
  emptyEpisodesAll: "No memories yet — what you live through together will settle here.",
  loadingMemories: "Retrieving memories...",
  loadMoreMemories: "Load more memories",
  fromWhatHappened: "from what happened",
  aHunch: "a hunch, not a memory",
};
const M: Record<Locale, typeof en> = {
  en,
  ko: {
    fallbackName: "이 사람",
    fallbackBio: "아직 소개가 연결되지 않았어요",
    tagline: (goal) => `요즘 몰두하는 것 — ${goal}`,
    watchingBadge: "당신이 지켜보는 사람",
    following: "팔로잉",
    follow: "팔로우",
    sendDm: "DM 보내기",
    innerWorldOf: (name) => `${name}의 마음속`,
    fadedBelief: "(흐려진 믿음)",
    beliefMeta: (pct, revisions) =>
      `(확신 ${pct}%${revisions > 1 ? ` · ${revisions}번 곱씹음` : ""})`,
    historyWithYou: "당신과의 역사",
    noFeelingsYet: (name) =>
      `아직 형성된 마음이 없어요 — 댓글이나 DM이 쌓이면 ${name}이(가) 당신을 곱씹기 시작합니다`,
    lifeChapters: "인생의 장",
    arcConnected: "아크 실측 연결됨",
    stillEmpty: "아직 비어 있음",
    directionDrawn: (when) => `${when} 그려진 방향`,
    noChapterYet: "아직 그려진 인생의 장이 없어요 — 지금은 그저 일상을 살고 있습니다.",
    memoriesOf: (name) => `${name}의 기억`,
    memoriesConnected: "기억 실측 연결됨",
    memoryWeight: (pct) => `마음에 남은 정도 ${pct}%`,
    emptyEpisodesRange: "이 기간의 기억이 없어요 — 범위를 넓히면 지난 날들이 보여요.",
    emptyEpisodesAll: "아직 쌓인 기억이 없어요 — 함께 겪은 일이 생기면 여기에 남습니다.",
    loadingMemories: "기억을 꺼내는 중...",
    loadMoreMemories: "기억 더 보기",
    fromWhatHappened: "겪은 일에서",
    aHunch: "기억이 아니라 짐작",
  },
};

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

/**
 * 이 문장이 겪은 일에서 온 것인지, 지금 지어낸 짐작인지 (ADR-021 §1).
 *
 * 근거 있는 것(recalled)과 지어낸 것(generated)만 표시한다 — 규칙 파생·저작물은
 * 사람이 읽고 판단할 구분이 아니라 조용히 둔다. 기계 어휘는 화면에 오르지
 * 않는다: 세계는 "generated"라고 말하지 않고 "짐작"이라고 말한다.
 */
function ProvenanceMark({ provenance }: { provenance: ProvenanceKind }) {
  const t = useMessages(M);
  if (provenance !== "recalled" && provenance !== "generated") return null;
  const grounded = provenance === "recalled";
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: WEIGHT.bold,
        color: grounded ? COLOR.muted : "#B7C2D8",
        fontStyle: grounded ? "normal" : "italic",
      }}
    >
      {grounded ? t.fromWhatHappened : t.aHunch}
    </span>
  );
}

export const ProfileTab = memo(ProfileTabInner);

function ProfileTabInner({
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
  const t = useMessages(M);
  const aboutMe = profile?.aboutMe ?? [];
  const episodes = profile?.episodes ?? [];
  const identity = profile?.identity ?? null;
  const arc = profile?.arc ?? null;
  // 지난 장들 (연대기, 오래된 순) — 마지막 항목은 현재 장이라 카드가 이미 보여준다
  const pastChapters = (profile?.arcHistory ?? []).slice(0, -1);
  // 표시 이름·소개·목표는 라이브 identity에서 — 없으면 중립 문구 (특정 인물 하드코딩 금지)
  // 정체성 실측이 아직 없으면 중립 지칭 — 문장 속에서도 어색하지 않아야 한다
  const displayName = identity?.name ?? t.fallbackName;
  const displayBio = identity?.bio ?? t.fallbackBio;
  const topGoal = identity?.goals.slice().sort((a, b) => b.priority - a.priority)[0];
  const tagline = topGoal ? t.tagline(topGoal.description) : "";
  return (
    <div style={{ flex: 1, overflowY: "auto" }}>
      <div style={{ height: 110, background: COLOR.primarySoft }} />
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
              <div style={{ fontSize: 26, fontWeight: WEIGHT.black }}>{displayName}</div>
              <div
                style={{
                  padding: "3px 12px",
                  background: COLOR.primarySoft,
                  color: COLOR.primaryDeep,
                  borderRadius: RADIUS.pill,
                  fontSize: 12,
                  fontWeight: WEIGHT.heavy,
                }}
              >
                {t.watchingBadge}
              </div>
            </div>
            <div style={{ fontSize: 14, color: COLOR.muted, fontWeight: WEIGHT.semibold }}>{displayBio}</div>
            {tagline && (
              <div
                style={{ fontSize: 13, color: COLOR.faint, fontWeight: WEIGHT.semibold, fontStyle: "italic" }}
              >
                {tagline}
              </div>
            )}
          </div>
          <div style={{ display: "flex", gap: 10, paddingBottom: 8 }}>
            <Pressable
              onClick={onToggleFollow}
              aria-pressed={following}
              className={styles.press95}
              style={{
                padding: "10px 20px",
                background: following ? COLOR.surface : COLOR.primary,
                color: following ? COLOR.primaryDeep : COLOR.white,
                borderRadius: RADIUS.pill,
                fontSize: 14,
                fontWeight: WEIGHT.heavy,
              }}
            >
              {following ? t.following : t.follow}
            </Pressable>
            <Pressable
              onClick={goDm}
              className={styles.press95}
              style={{
                padding: "10px 20px",
                background: COLOR.primary,
                color: COLOR.white,
                borderRadius: RADIUS.pill,
                fontSize: 14,
                fontWeight: WEIGHT.heavy,
              }}
            >
              {t.sendDm}
            </Pressable>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "13px 18px",
            background: "#FFF6DE",
            borderRadius: RADIUS.md,
          }}
        >
          <Icon d={ICON.journey} size={18} color="#A87F24" />
          {aboutMe.length > 0 ? (
            // 실측 — 이 액터가 나에 대해 실제로 형성한 신념 (reflection, ADR-008)
            <div style={{ fontSize: 13, color: COLOR.muted, fontWeight: WEIGHT.semibold }}>
              <span style={{ fontWeight: WEIGHT.heavy, color: COLOR.ink }}>
                {t.innerWorldOf(displayName)}
              </span>
              {/* 한 인물이 같은 kind의 신념을 여럿 품는다 — 키는 종류가 아니라 자리다 */}
              {aboutMe.map((b, i) =>
                b.confidence <= FADED_BELIEF_MAX ? (
                  // 철회된 신념 — 잔불로 남은 흔적 (ADR-008 신념 폐기)
                  <span key={`${b.kind}-${i}`} style={{ color: "#B7C2D8", fontStyle: "italic" }}>
                    {" · "}
                    {humanize(b.statement)}{" "}
                    <span style={{ fontWeight: WEIGHT.bold }}>{t.fadedBelief}</span>
                  </span>
                ) : (
                  <span key={`${b.kind}-${i}`}>
                    {" · "}
                    {humanize(b.statement)}{" "}
                    <span style={{ color: "#A87F24", fontWeight: WEIGHT.bold }}>
                      {t.beliefMeta(Math.round(b.confidence * 100), b.revisions)}
                    </span>{" "}
                    <ProvenanceMark provenance={b.provenance} />
                  </span>
                ),
              )}
            </div>
          ) : (
            // 실측이 없으면 없다고 말한다 — 가공의 역사를 그리지 않는다
            <div style={{ fontSize: 13, color: COLOR.muted, fontWeight: WEIGHT.semibold }}>
              <span style={{ fontWeight: WEIGHT.heavy, color: COLOR.ink }}>{t.historyWithYou}</span>
              {" · "}
              {t.noFeelingsYet(displayName)}
            </div>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontSize: 14, fontWeight: WEIGHT.heavy }}>{t.lifeChapters}</div>
          <div
            style={{
              fontSize: 11,
              fontWeight: WEIGHT.heavy,
              color: arc ? COLOR.success : COLOR.faint,
            }}
          >
            {arc ? t.arcConnected : t.stillEmpty}
          </div>
        </div>

        {arc ? (
          // Director가 그린 이번 시즌의 인생 방향 (ADR-013, plan/08) — 명령이 아니라 배경
          <div
            style={{
              border: "1.5px solid #E2EAF6",
              borderRadius: RADIUS.lg,
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
                  background: COLOR.primarySoft,
                  color: COLOR.primaryDeep,
                  borderRadius: RADIUS.pill,
                  fontSize: 12,
                  fontWeight: WEIGHT.heavy,
                }}
              >
                {arcStageLabel(arc.stage)}
              </div>
              <div style={{ fontSize: 12, color: COLOR.faint, fontWeight: WEIGHT.bold }}>
                {t.directionDrawn(relativeTime(arc.plannedAt))}
              </div>
            </div>
            <div style={{ fontSize: 14, lineHeight: 1.65, fontWeight: WEIGHT.regular }}>
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
                    style={{ fontSize: 12, color: COLOR.faint, fontWeight: WEIGHT.semibold }}
                  >
                    <span style={{ color: "#7B62C9", fontWeight: WEIGHT.heavy }}>
                      {arcStageLabel(chapter.stage)}
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
              borderRadius: RADIUS.lg,
              padding: "24px 20px",
              textAlign: "center",
              color: COLOR.faint,
              fontSize: 14,
              fontWeight: WEIGHT.semibold,
            }}
          >
            {t.noChapterYet}
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontSize: 14, fontWeight: WEIGHT.heavy }}>{t.memoriesOf(displayName)}</div>
          <div
            style={{
              fontSize: 11,
              fontWeight: WEIGHT.heavy,
              color: episodes.length > 0 ? COLOR.success : COLOR.faint,
            }}
          >
            {episodes.length > 0 ? t.memoriesConnected : t.stillEmpty}
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
                  borderRadius: RADIUS.lg,
                  padding: "16px 20px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                }}
              >
                <div
                  style={{
                    fontSize: 12,
                    color: COLOR.faint,
                    fontWeight: WEIGHT.bold,
                    display: "flex",
                    flexWrap: "wrap",
                    gap: 6,
                    alignItems: "baseline",
                  }}
                >
                  <span>
                    {relativeTime(ep.occurredAt)} ·{" "}
                    {t.memoryWeight(Math.round(ep.importance * 100))}
                  </span>
                  <ProvenanceMark provenance={ep.provenance} />
                </div>
                <div style={{ fontSize: 14, lineHeight: 1.65, fontWeight: WEIGHT.regular }}>
                  {humanize(ep.summary)}
                </div>
              </div>
            ))
          ) : (
            <div
              style={{
                border: "1.5px dashed #D8E1F0",
                borderRadius: RADIUS.lg,
                padding: "24px 20px",
                textAlign: "center",
                color: COLOR.faint,
                fontSize: 14,
                fontWeight: WEIGHT.semibold,
              }}
            >
              {episodeRange !== "all" ? t.emptyEpisodesRange : t.emptyEpisodesAll}
            </div>
          )}
          {episodes.length > 0 && hasMoreEpisodes && (
            // 과거 방향 커서 페이지네이션 — 오래된 기억을 목록 아래에 이어 붙인다
            <Pressable
              onClick={onLoadMoreEpisodes}
              disabled={loadingEpisodes}
              className={styles.press95}
              style={{
                alignSelf: "center",
                padding: "9px 22px",
                background: COLOR.surface,
                color: COLOR.primaryDeep,
                borderRadius: RADIUS.pill,
                fontSize: 13,
                fontWeight: WEIGHT.heavy,
                cursor: loadingEpisodes ? "default" : "pointer",
                opacity: loadingEpisodes ? 0.6 : 1,
              }}
            >
              {loadingEpisodes ? t.loadingMemories : t.loadMoreMemories}
            </Pressable>
          )}
        </div>
      </div>
    </div>
  );
}
