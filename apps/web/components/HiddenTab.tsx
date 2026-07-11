import { ICON } from "@/lib/data";

import { Face } from "./Face";
import { Icon } from "./Icon";

export function HiddenTab() {
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
          <div style={{ fontSize: 20, fontWeight: 800 }}>Hidden Feed</div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              background: "#F1EDFB",
              color: "#7a68b3",
              borderRadius: 9999,
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            <Icon d={ICON.eye} size={12} /> 당신에게만 보여요
          </div>
        </div>
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
          신뢰가 열어준 이야기 · 2개
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
          background: "#FBFAFE",
        }}
      >
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600, lineHeight: 1.6 }}>
          세계에는 항상 보이는 것보다 많은 일이 일어나요. 높은 신뢰를 쌓은 액터가 당신에게만
          털어놓는 이야기입니다. 여기서 알게 된 것을 어떻게 쓸지는 — 당신의 선택이에요.
        </div>

        {/* 민지의 비밀 */}
        <div
          style={{
            border: "1.5px solid #E0D8F5",
            background: "#fff",
            borderRadius: 20,
            padding: "20px 24px",
            display: "flex",
            flexDirection: "column",
            gap: 12,
            boxShadow: "0 8px 24px rgba(122,104,179,0.10)",
          }}
        >
          <div
            style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <Face preset="minji44" />
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ fontSize: 15, fontWeight: 800 }}>김민지</div>
                  <div
                    style={{
                      padding: "2px 10px",
                      background: "#F1EDFB",
                      color: "#7a68b3",
                      borderRadius: 9999,
                      fontSize: 11,
                      fontWeight: 800,
                    }}
                  >
                    비공개 · 당신만
                  </div>
                </div>
                <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>
                  방금 · 신뢰의 보상
                </div>
              </div>
            </div>
            <Icon d={ICON.lockOpen} size={18} color="#7a68b3" />
          </div>
          <div style={{ fontSize: 15, lineHeight: 1.65, fontWeight: 500 }}>
            사실 아무한테도 말 안 한 게 있어요. 이직 제안을 받았어요. 지금 회사보다 훨씬 좋은
            조건인데... 철수한테 미안해서 말을 못 하겠어요. 우리 같이 시작했는데, 나만 떠나는 것
            같아서.
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "9px 14px",
              background: "#FBFAFE",
              border: "1.5px solid #E0D8F5",
              borderRadius: 12,
            }}
          >
            <Icon d={ICON.info} size={14} color="#7a68b3" />
            <div style={{ fontSize: 13, color: "#7a68b3", fontWeight: 600 }}>
              이 비밀을 아는 사람은 세계에서 당신뿐이에요. 발설하면 관계가 크게 흔들립니다.
            </div>
          </div>
        </div>

        {/* 잠긴 뒷대화방 */}
        <div
          style={{
            border: "1.5px dashed #D9D2EC",
            background: "#fff",
            borderRadius: 20,
            padding: "20px 24px",
            display: "flex",
            alignItems: "center",
            gap: 16,
            opacity: 0.85,
          }}
        >
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 14,
              background: "#F1EDFB",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <Icon d={ICON.lock} size={19} color="#7a68b3" />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3, flex: 1 }}>
            <div style={{ fontSize: 15, fontWeight: 800 }}>
              운영진 뒷대화방 — &apos;투표, 진짜 이유&apos;
            </div>
            <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
              커뮤니티 실세가 되어야 초대받는 곳이에요. 수진 또는 하린의 깊은 신뢰가 필요해요.
            </div>
          </div>
          <div
            style={{
              padding: "8px 16px",
              background: "#F1EDFB",
              color: "#7a68b3",
              borderRadius: 9999,
              fontSize: 13,
              fontWeight: 800,
              whiteSpace: "nowrap",
            }}
          >
            실세 레벨 필요
          </div>
        </div>
      </div>
    </>
  );
}
