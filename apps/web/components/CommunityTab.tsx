import { memo, useEffect, useState } from "react";

import type { Community, CommunityPost } from "@/lib/community";
import { ICON } from "@/lib/data";
import { relativeTime } from "@/lib/live-feed";

import { Icon } from "./Icon";

const AVATAR_COLORS = ["#CBBDE8", "#AFC8F5", "#F2B8CF", "#BFE3CF", "#E8D5A8"];

function avatarColor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

interface CommunityTabProps {
  communities: Community[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  posts: CommunityPost[];
  available: boolean;
  /** actor_id → 표시 이름 (라이브 identity, 하드코딩 금지) */
  nameOf: (actorId: string) => string;
}

export const CommunityTab = memo(CommunityTabInner);

function CommunityTabInner({
  communities,
  selectedId,
  onSelect,
  posts,
  available,
  nameOf,
}: CommunityTabProps) {
  // 목록이 오면 첫 커뮤니티를 자동 선택 (빈 화면 대신 바로 내용)
  useEffect(() => {
    if (!selectedId && communities.length > 0) onSelect(communities[0].id);
  }, [communities, selectedId, onSelect]);

  const selected = communities.find((c) => c.id === selectedId) ?? null;

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
          <div style={{ fontSize: 20, fontWeight: 800 }}>커뮤니티</div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              background: "#EAF1FB",
              color: "#4a72b8",
              borderRadius: 9999,
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            <Icon d={ICON.users} size={12} /> 내집단의 소식
          </div>
        </div>
        {selected && (
          <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
            {selected.name} · {posts.length}개
          </div>
        )}
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
        {/* 커뮤니티 선택 칩 */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {communities.map((c) => {
            const active = c.id === selectedId;
            return (
              <button
                key={c.id}
                onClick={() => onSelect(c.id)}
                style={{
                  padding: "8px 16px",
                  borderRadius: 9999,
                  border: active ? "1.5px solid #4a72b8" : "1.5px solid #E0E7F2",
                  background: active ? "#4a72b8" : "#fff",
                  color: active ? "#fff" : "#5A6478",
                  fontSize: 13,
                  fontWeight: 700,
                  cursor: "pointer",
                }}
              >
                {c.name}
              </button>
            );
          })}
        </div>

        {selected && (
          <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600, lineHeight: 1.6 }}>
            {selected.description}
          </div>
        )}

        {communities.length === 0 ? (
          <EmptyCard>
            아직 커뮤니티를 불러오지 못했어요 — 게이트웨이·피드 연결을 확인하세요.
          </EmptyCard>
        ) : posts.length === 0 ? (
          <EmptyCard>
            {available ? (
              <>이 커뮤니티는 아직 조용해요 — 소속 인물들이 움직이면 여기 소식이 쌓입니다.</>
            ) : (
              <>커뮤니티 피드를 불러오지 못했어요 — 잠시 후 다시 보여요.</>
            )}
          </EmptyCard>
        ) : (
          posts.map((post) => {
            const name = post.actorId ? nameOf(post.actorId) : "세계";
            return (
              <div
                key={post.id}
                style={{
                  border: "1.5px solid #E2E9F3",
                  background: "#fff",
                  borderRadius: 20,
                  padding: "20px 24px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 12,
                  boxShadow: "0 8px 24px rgba(74,114,184,0.08)",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <div
                    style={{
                      width: 44,
                      height: 44,
                      borderRadius: "50%",
                      background: avatarColor(post.actorId ?? "world"),
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
                    <div style={{ fontSize: 15, fontWeight: 800 }}>{name}</div>
                    <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
                      {relativeTime(post.occurredAt)}
                    </div>
                  </div>
                </div>
                {post.title && (
                  <div style={{ fontSize: 15, fontWeight: 700, lineHeight: 1.5 }}>{post.title}</div>
                )}
                <div style={{ fontSize: 14, lineHeight: 1.65, fontWeight: 500, color: "#3A4256" }}>
                  {post.body}
                </div>
              </div>
            );
          })
        )}
      </div>
    </>
  );
}

function EmptyCard({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        border: "1.5px dashed #D3DDEC",
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
      {children}
    </div>
  );
}
