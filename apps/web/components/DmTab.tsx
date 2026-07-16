import type { KeyboardEvent } from "react";

import { ICON } from "@/lib/data";
import type { DmMessage } from "@/lib/types";

import { Face } from "./Face";
import { Icon } from "./Icon";
import styles from "./lf.module.css";

interface DmTabProps {
  worldTime: string;
  /** 대화 상대 표시 이름 — 라이브 identity에서 온다 (하드코딩 금지) */
  partnerName: string;
  messages: DmMessage[];
  typing: boolean;
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: () => void;
  /** 더 과거 대화 페이지가 남아있는가 — 커서 소진·백엔드 미가용이면 숨긴다 */
  canLoadOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
}

export function DmTab({
  worldTime,
  partnerName,
  messages,
  typing,
  draft,
  onDraftChange,
  onSend,
  canLoadOlder,
  loadingOlder,
  onLoadOlder,
}: DmTabProps) {
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") onSend();
  };

  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "15px 28px",
          borderBottom: "1.5px solid #EEF3FB",
        }}
      >
        <Face preset="dmHeader38" />
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div style={{ fontSize: 15, fontWeight: 800 }}>{partnerName}</div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Icon d={ICON.moon} size={12} color="#8C97AF" />
            <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
              답장이 느릴 수 있어요
            </div>
          </div>
        </div>
      </div>

      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "24px 28px",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        {canLoadOlder && (
          // 과거 방향 커서 페이지네이션 — 과거 메시지를 목록 위에 이어 붙인다
          <div
            onClick={loadingOlder ? undefined : onLoadOlder}
            className={styles.press95}
            style={{
              alignSelf: "center",
              fontSize: 12,
              color: "#5F7EC9",
              background: "#EDF3FD",
              padding: "5px 14px",
              borderRadius: 9999,
              fontWeight: 700,
              cursor: loadingOlder ? "default" : "pointer",
              opacity: loadingOlder ? 0.6 : 1,
            }}
          >
            {loadingOlder ? "이전 대화 불러오는 중..." : "이전 대화 더 보기"}
          </div>
        )}

        <div
          style={{
            alignSelf: "center",
            fontSize: 12,
            color: "#8C97AF",
            background: "#F2F6FC",
            padding: "5px 14px",
            borderRadius: 9999,
            fontWeight: 700,
          }}
        >
          오늘 · 세계 시간 {worldTime}
        </div>

        {messages.map((msg, i) => {
          const mine = msg.from === "me";
          const next = messages[i + 1];
          const showAvatar = !mine && (i === messages.length - 1 || next?.from === "me");
          return (
            // 히스토리 메시지는 event id로 고정 — 위로 이어 붙여도 기존 말풍선이 리마운트되지 않는다
            <div
              key={msg.eventId ?? `local-${i}`}
              style={{
                display: "flex",
                gap: 10,
                alignItems: "flex-end",
                justifyContent: mine ? "flex-end" : "flex-start",
              }}
            >
              {showAvatar && <Face preset="dm30" />}
              <div
                style={{
                  maxWidth: 440,
                  background: mine ? "#6D8DD6" : "#F2F6FC",
                  color: mine ? "#ffffff" : "#3A4256",
                  padding: "12px 17px",
                  borderRadius: mine ? "18px 18px 4px 18px" : "18px 18px 18px 4px",
                  fontSize: 14,
                  lineHeight: 1.55,
                  fontWeight: 500,
                  animation: "lf-pop 0.25s ease-out",
                }}
              >
                {msg.text}
              </div>
            </div>
          );
        })}

        {typing && (
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <div
              style={{
                display: "flex",
                gap: 4,
                alignItems: "center",
                padding: "11px 15px",
                background: "#F2F6FC",
                borderRadius: "18px 18px 18px 4px",
              }}
            >
              <div
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: "#6B7691",
                  animation: "lf-blink 1s infinite",
                }}
              />
              <div
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: "#9AA6BF",
                  animation: "lf-blink 1s 0.2s infinite",
                }}
              />
              <div
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: "#CBD5E8",
                  animation: "lf-blink 1s 0.4s infinite",
                }}
              />
            </div>
            <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
              {partnerName}가 입력 중...
            </div>
          </div>
        )}
      </div>

      <div
        style={{
          padding: "16px 28px 22px",
          borderTop: "1.5px solid #EEF3FB",
          display: "flex",
          gap: 12,
          alignItems: "center",
        }}
      >
        <input
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          onKeyDown={onKey}
          placeholder={`${partnerName}에게 답장하기...`}
          style={{
            flex: 1,
            background: "#F2F6FC",
            border: "none",
            outline: "none",
            borderRadius: 9999,
            padding: "13px 20px",
            fontSize: 14,
            color: "#3A4256",
            fontWeight: 500,
          }}
        />
        <div
          onClick={onSend}
          className={styles.press92}
          style={{
            width: 44,
            height: 44,
            background: "#6D8DD6",
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            cursor: "pointer",
            boxShadow: "0 4px 12px rgba(109,141,214,0.35)",
          }}
        >
          <Icon d={ICON.send} size={18} color="#fff" />
        </div>
      </div>
    </>
  );
}
