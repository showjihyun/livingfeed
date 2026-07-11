import type { KeyboardEvent } from "react";

import { ICON } from "@/lib/data";
import type { FeedComment } from "@/lib/types";

import { Face } from "./Face";
import { Icon } from "./Icon";
import styles from "./lf.module.css";

interface FeedTabProps {
  showCoach: boolean;
  onDismissCoach: () => void;
  streaming: boolean;
  streamText: string;
  streamDone: boolean;
  minjiLiked: boolean;
  minjiLikes: number;
  onLikeMinji: () => void;
  minjiCommentCount: number;
  minjiComments: FeedComment[];
  minjiTyping: boolean;
  commentDraft: string;
  onCommentDraftChange: (value: string) => void;
  onSendComment: () => void;
  chulsuLiked: boolean;
  chulsuLikes: number;
  onLikeChulsu: () => void;
  goProfile: () => void;
  goDm: () => void;
  goGraph: () => void;
}

function TypingDots({ size, radius }: { size: number; radius: string }) {
  return (
    <div
      style={{
        display: "flex",
        gap: 4,
        alignItems: "center",
        padding: size === 7 ? "11px 15px" : "8px 12px",
        background: "#F2F6FC",
        borderRadius: radius,
      }}
    >
      <div
        style={{
          width: size,
          height: size,
          borderRadius: "50%",
          background: "#6B7691",
          animation: "lf-blink 1s infinite",
        }}
      />
      <div
        style={{
          width: size,
          height: size,
          borderRadius: "50%",
          background: "#9AA6BF",
          animation: "lf-blink 1s 0.2s infinite",
        }}
      />
      <div
        style={{
          width: size,
          height: size,
          borderRadius: "50%",
          background: "#CBD5E8",
          animation: "lf-blink 1s 0.4s infinite",
        }}
      />
    </div>
  );
}

export function FeedTab({
  showCoach,
  onDismissCoach,
  streaming,
  streamText,
  streamDone,
  minjiLiked,
  minjiLikes,
  onLikeMinji,
  minjiCommentCount,
  minjiComments,
  minjiTyping,
  commentDraft,
  onCommentDraftChange,
  onSendComment,
  chulsuLiked,
  chulsuLikes,
  onLikeChulsu,
  goProfile,
  goDm,
  goGraph,
}: FeedTabProps) {
  const onCommentKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") onSendComment();
  };

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
          <div style={{ fontSize: 20, fontWeight: 800 }}>World Feed</div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              background: "#E3F5EC",
              color: "#3E8A66",
              borderRadius: 9999,
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            <div
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: "#5FBF95",
                animation: "lf-blink 1.6s infinite",
              }}
            />
            실시간
          </div>
        </div>
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
          액터 100명 · 진행 중인 이야기 7개
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
        }}
      >
        {/* Aha 코치 배너 */}
        {showCoach && (
          <div
            style={{
              background: "#EDF3FD",
              border: "1.5px solid #CFE0F8",
              borderRadius: 18,
              padding: "14px 20px",
              display: "flex",
              alignItems: "center",
              gap: 14,
              animation: "lf-pop 0.35s ease-out",
            }}
          >
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 12,
                background: "#fff",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}
            >
              <Icon d={ICON.sparkles} size={17} color="#5F7EC9" />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 800, color: "#3A4256" }}>
                당신을 사건의 한가운데로 데려왔어요
              </div>
              <div style={{ fontSize: 13, color: "#5F7EC9", fontWeight: 600 }}>
                &apos;직장 드라마&apos; 취향 반영 · 지금 가장 갈등 밀도 높은 이야기예요. 좋아요
                하나로 조용히 시작해보세요 — 그것만으로도 세계는 당신을 알아차립니다.
              </div>
            </div>
            <div
              onClick={onDismissCoach}
              style={{
                color: "#8C97AF",
                cursor: "pointer",
                fontSize: 16,
                fontWeight: 800,
                lineHeight: 1,
                padding: 4,
              }}
            >
              ×
            </div>
          </div>
        )}

        {/* 민지 스트리밍 포스트 */}
        <div
          style={{
            border: "1.5px solid #CFE0F8",
            background: "#F8FBFF",
            borderRadius: 20,
            padding: "20px 24px",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <div
            style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <Face preset="minji44" onClick={goProfile} />
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div
                    onClick={goProfile}
                    style={{ fontSize: 15, fontWeight: 800, cursor: "pointer" }}
                  >
                    김민지
                  </div>
                  <div
                    style={{
                      padding: "2px 10px",
                      background: "#EDF3FD",
                      color: "#5F7EC9",
                      borderRadius: 9999,
                      fontSize: 11,
                      fontWeight: 800,
                    }}
                  >
                    당신과 친한 사이
                  </div>
                  <div
                    style={{
                      padding: "2px 10px",
                      background: "#FDEDF3",
                      color: "#C76F93",
                      borderRadius: 9999,
                      fontSize: 11,
                      fontWeight: 800,
                    }}
                  >
                    철수와 갈등 중
                  </div>
                </div>
                <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
                  {streamDone ? "방금 · 세계 시간 22:44" : "지금 · 쓰는 중"}
                </div>
              </div>
            </div>
            {streaming && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 12px",
                  background: "#E3F5EC",
                  borderRadius: 9999,
                }}
              >
                <div
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: "#5FBF95",
                    animation: "lf-blink 1.2s infinite",
                  }}
                />
                <div style={{ fontSize: 11, fontWeight: 800, color: "#3E8A66" }}>LIVE</div>
              </div>
            )}
          </div>

          <div style={{ fontSize: 15, lineHeight: 1.65, fontWeight: 500, minHeight: 50 }}>
            {streamText}
            {streaming && (
              <span
                style={{
                  display: "inline-block",
                  width: 9,
                  height: 17,
                  background: "#6D8DD6",
                  verticalAlign: "text-bottom",
                  marginLeft: 2,
                  borderRadius: 2,
                  animation: "lf-blink 0.9s infinite",
                }}
              />
            )}
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "9px 14px",
              background: "#fff",
              border: "1.5px solid #E2EAF6",
              borderRadius: 12,
            }}
          >
            <Icon d={ICON.book} size={14} color="#8C97AF" />
            <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
              <span style={{ fontWeight: 800, color: "#3A4256" }}>
                &apos;민지의 퇴사 고민&apos;
              </span>{" "}
              4화 ·{" "}
              <a href="#" onClick={(e) => e.preventDefault()}>
                이전 이야기 보기
              </a>
            </div>
          </div>

          {streamDone && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div
                  onClick={onLikeMinji}
                  className={styles.press94}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "7px 14px",
                    background: minjiLiked ? "#C76F93" : "#FDEDF3",
                    color: minjiLiked ? "#ffffff" : "#C76F93",
                    borderRadius: 9999,
                    fontSize: 13,
                    fontWeight: 800,
                    cursor: "pointer",
                    userSelect: "none",
                  }}
                >
                  <Icon d={ICON.heart} size={15} /> {minjiLikes}
                </div>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "7px 14px",
                    background: "#F2F6FC",
                    color: "#6B7691",
                    borderRadius: 9999,
                    fontSize: 13,
                    fontWeight: 800,
                  }}
                >
                  <Icon d={ICON.messageCircle} size={15} /> {minjiCommentCount}
                </div>
                <div
                  onClick={goDm}
                  className={styles.dmPill}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "7px 14px",
                    borderRadius: 9999,
                    fontSize: 13,
                    fontWeight: 800,
                    cursor: "pointer",
                  }}
                >
                  <Icon d={ICON.send} size={15} /> DM
                </div>
              </div>

              {minjiComments.map((cm, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    gap: 10,
                    padding: "12px 14px",
                    background: cm.bg,
                    borderRadius: 14,
                  }}
                >
                  <Face preset="comment28" bg={cm.avatarBg} />
                  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    <div style={{ fontSize: 13, fontWeight: 800 }}>{cm.author}</div>
                    <div style={{ fontSize: 14, lineHeight: 1.55, fontWeight: 500 }}>{cm.text}</div>
                  </div>
                </div>
              ))}

              {minjiTyping && (
                <div style={{ display: "flex", gap: 8, alignItems: "center", paddingLeft: 4 }}>
                  <TypingDots size={6} radius="9999px" />
                  <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
                    민지가 당신의 댓글에 답을 쓰고 있어요...
                  </div>
                </div>
              )}

              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <input
                  value={commentDraft}
                  onChange={(e) => onCommentDraftChange(e.target.value)}
                  onKeyDown={onCommentKey}
                  placeholder="민지에게 댓글 남기기... (개입은 흔적을 남겨요)"
                  style={{
                    flex: 1,
                    background: "#F2F6FC",
                    border: "none",
                    outline: "none",
                    borderRadius: 9999,
                    padding: "12px 18px",
                    fontSize: 14,
                    color: "#3A4256",
                    fontWeight: 500,
                  }}
                />
                <div
                  onClick={onSendComment}
                  className={styles.press95}
                  style={{
                    padding: "11px 20px",
                    background: "#6D8DD6",
                    color: "#fff",
                    borderRadius: 9999,
                    fontSize: 14,
                    fontWeight: 800,
                    cursor: "pointer",
                  }}
                >
                  전송
                </div>
              </div>
            </>
          )}
        </div>

        {/* 철수 포스트 */}
        <div
          style={{
            border: "1.5px solid #E2EAF6",
            borderRadius: 20,
            padding: "20px 24px",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <Face preset="chulsu44" />
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ fontSize: 15, fontWeight: 800 }}>이철수</div>
                <div
                  style={{
                    padding: "2px 10px",
                    background: "#F2F6FC",
                    color: "#6B7691",
                    borderRadius: 9999,
                    fontSize: 11,
                    fontWeight: 800,
                  }}
                >
                  아는 사이
                </div>
              </div>
              <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
                18분 전 · 세계 시간 21:29
              </div>
            </div>
          </div>
          <div style={{ fontSize: 15, lineHeight: 1.65, fontWeight: 500 }}>
            소개팅 다녀왔다. 상대방이 두 시간 내내 자기 얘기만 했는데, 이상하게 기분이 나쁘지
            않았다. 내가 원래 듣는 걸 좋아했나?
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div
              onClick={onLikeChulsu}
              className={styles.press94}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "7px 14px",
                background: chulsuLiked ? "#C76F93" : "#FDEDF3",
                color: chulsuLiked ? "#ffffff" : "#C76F93",
                borderRadius: 9999,
                fontSize: 13,
                fontWeight: 800,
                cursor: "pointer",
                userSelect: "none",
              }}
            >
              <Icon d={ICON.heart} size={15} /> {chulsuLikes}
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "7px 14px",
                background: "#F2F6FC",
                color: "#6B7691",
                borderRadius: 9999,
                fontSize: 13,
                fontWeight: 800,
              }}
            >
              <Icon d={ICON.messageCircle} size={15} /> 4
            </div>
            <div style={{ marginLeft: "auto", fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
              민지가 이 글에 화를 냈어요 ·{" "}
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  goGraph();
                }}
              >
                왜?
              </a>
            </div>
          </div>
          <div
            style={{
              display: "flex",
              gap: 10,
              padding: "12px 14px",
              background: "#F8FAFD",
              borderRadius: 14,
            }}
          >
            <Face preset="comment28Frown" />
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <div style={{ fontSize: 13, fontWeight: 800 }}>김민지</div>
              <div style={{ fontSize: 14, lineHeight: 1.55, fontWeight: 500 }}>
                남 얘기 들어줄 시간에 네 앞가림이나 잘하지 그래.
              </div>
            </div>
          </div>
        </div>

        {/* 세계 뉴스 */}
        <div
          style={{
            background: "#FFF6DE",
            borderRadius: 20,
            padding: "18px 24px",
            display: "flex",
            alignItems: "center",
            gap: 16,
          }}
        >
          <Icon d={ICON.megaphone} size={22} color="#A87F24" />
          <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
            <div style={{ fontSize: 12, fontWeight: 800, color: "#A87F24" }}>세계 뉴스</div>
            <div style={{ fontSize: 15, fontWeight: 800 }}>
              개발자 커뮤니티 운영권 투표 시작 — 수진 vs 하린
            </div>
            <div style={{ fontSize: 13, color: "#6B7691", fontWeight: 600 }}>
              &apos;운영권 분쟁&apos; 2화 · 세계 시간 내일 오전 마감
            </div>
          </div>
          <div
            onClick={goGraph}
            className={styles.press95}
            style={{
              padding: "9px 18px",
              background: "#fff",
              borderRadius: 9999,
              fontSize: 14,
              fontWeight: 800,
              color: "#A87F24",
              cursor: "pointer",
            }}
          >
            관계 보기
          </div>
        </div>
      </div>
    </>
  );
}
