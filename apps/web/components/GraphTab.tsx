import { GRAPH_NODES, REL, REL_TAB_DEFS } from "@/lib/data";
import type { RelKey } from "@/lib/types";

interface GraphTabProps {
  sel: RelKey;
  onSelect: (key: RelKey) => void;
}

/** 그래프 노드 얼굴 — 크기가 %로 스케일되는 변형 (프로토타입 그대로) */
function GraphNodeFace({
  name,
  x,
  y,
  size,
  color,
  ring,
}: {
  name: string;
  x: string;
  y: string;
  size: number;
  color: string;
  ring: string;
}) {
  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width: size,
        height: size,
        borderRadius: "50%",
        background: color,
        boxShadow: ring,
      }}
    >
      <div
        style={{
          position: "absolute",
          left: "27%",
          top: "38%",
          width: "11%",
          height: "11%",
          borderRadius: "50%",
          background: "#3A4256",
        }}
      />
      <div
        style={{
          position: "absolute",
          right: "27%",
          top: "38%",
          width: "11%",
          height: "11%",
          borderRadius: "50%",
          background: "#3A4256",
        }}
      />
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: "58%",
          transform: "translateX(-50%)",
          width: "18%",
          height: "8%",
          borderBottom: "2px solid #3A4256",
          borderRadius: "0 0 8px 8px",
        }}
      />
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: "108%",
          transform: "translateX(-50%)",
          fontSize: 12,
          fontWeight: 800,
          color: "#6B7691",
          whiteSpace: "nowrap",
        }}
      >
        {name}
      </div>
    </div>
  );
}

export function GraphTab({ sel, onSelect }: GraphTabProps) {
  const rel = REL[sel];
  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "16px 24px",
          borderBottom: "1.5px solid #EEF3FB",
        }}
      >
        <div style={{ fontSize: 18, fontWeight: 900 }}>관계 그래프</div>
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
          관계를 클릭해 이야기의 이력을 보세요
        </div>
      </div>

      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        <div style={{ flex: 1, position: "relative", background: "#F8FBFF" }}>
          <svg
            width="100%"
            height="100%"
            viewBox="0 0 760 620"
            preserveAspectRatio="xMidYMid meet"
            style={{ position: "absolute", inset: 0 }}
          >
            <line
              x1={380}
              y1={300}
              x2={210}
              y2={160}
              stroke="#F5B8CB"
              strokeWidth={4}
              strokeDasharray="7 6"
              onClick={() => onSelect("mc")}
              style={{ cursor: "pointer" }}
            />
            <line
              x1={380}
              y1={300}
              x2={560}
              y2={180}
              stroke="#BFE3D0"
              strokeWidth={3}
              onClick={() => onSelect("ms")}
              style={{ cursor: "pointer" }}
            />
            <line
              x1={380}
              y1={300}
              x2={545}
              y2={440}
              stroke="#AFC8F5"
              strokeWidth={5}
              onClick={() => onSelect("me")}
              style={{ cursor: "pointer" }}
            />
            <line x1={560} y1={180} x2={640} y2={350} stroke="#FFE9A8" strokeWidth={3} />
            <line x1={210} y1={160} x2={560} y2={180} stroke="#E2EAF6" strokeWidth={2} />
            <line x1={380} y1={300} x2={200} y2={430} stroke="#E2EAF6" strokeWidth={2} />
          </svg>
          {GRAPH_NODES.map((n) => {
            const selected = n.selAlways === true || (n.selKey !== undefined && n.selKey === sel);
            return (
              <GraphNodeFace
                key={n.name}
                name={n.name}
                x={n.x}
                y={n.y}
                size={n.size}
                color={n.color}
                ring={
                  selected ? "0 0 0 3px #fff, 0 0 0 6px #6D8DD6" : "0 4px 10px rgba(109,141,214,0.18)"
                }
              />
            );
          })}
        </div>

        <div
          style={{
            width: 360,
            borderLeft: "1.5px solid #EEF3FB",
            padding: 22,
            display: "flex",
            flexDirection: "column",
            gap: 16,
            flexShrink: 0,
            overflowY: "auto",
          }}
        >
          <div style={{ display: "flex", gap: 8 }}>
            {REL_TAB_DEFS.map((rt) => {
              const active = sel === rt.key;
              return (
                <div
                  key={rt.key}
                  onClick={() => onSelect(rt.key)}
                  style={{
                    padding: "7px 14px",
                    background: active ? "#3A4256" : "#F2F6FC",
                    color: active ? "#ffffff" : "#6B7691",
                    borderRadius: 9999,
                    fontSize: 12,
                    fontWeight: 800,
                    cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  {rt.label}
                </div>
              );
            })}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <div style={{ fontSize: 17, fontWeight: 900 }}>{rel.title}</div>
            <div style={{ fontSize: 13, fontWeight: 800, color: rel.statusColor }}>
              {rel.status}
            </div>
          </div>

          <div style={{ fontSize: 13, lineHeight: 1.6, color: "#6B7691", fontWeight: 600 }}>
            {rel.desc}
          </div>

          <div style={{ display: "flex", flexDirection: "column" }}>
            <div style={{ fontSize: 12, fontWeight: 800, color: "#8C97AF", marginBottom: 10 }}>
              이 관계의 이력
            </div>
            {rel.history.map((rh) => (
              <div key={rh.title} style={{ display: "flex", gap: 12 }}>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                  <div
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      background: rh.dot,
                      marginTop: 4,
                    }}
                  />
                  <div style={{ width: 2, flex: 1, background: "#EEF3FB" }} />
                </div>
                <div style={{ paddingBottom: 13 }}>
                  <div style={{ fontSize: 13, fontWeight: 800 }}>{rh.title}</div>
                  <div style={{ fontSize: 12, color: "#8C97AF", fontWeight: 600 }}>{rh.meta}</div>
                </div>
              </div>
            ))}
          </div>

          <div
            style={{
              background: "#EDF3FD",
              borderRadius: 16,
              padding: "13px 15px",
              fontSize: 12,
              lineHeight: 1.55,
              color: "#6B7691",
              fontWeight: 600,
            }}
          >
            <span style={{ fontWeight: 800, color: "#3A4256" }}>개입할 수 있어요</span> —{" "}
            {rel.hint}
          </div>
        </div>
      </div>
    </>
  );
}
