"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { REL, REL_TAB_DEFS } from "@/lib/data";
import type { RelKey } from "@/lib/types";

import styles from "./lf.module.css";

interface GraphTabProps {
  sel: RelKey;
  onSelect: (key: RelKey) => void;
}

/* ── 그래프 데이터 (viewBox 760×620 좌표계) ──
   위상: 김민지가 중심(회전축), 나머지는 궤도 위성이다.
   드래그 회전은 위성의 극좌표 각도에 공통 θ를 더하는 유사 3D 궤도 —
   화면 y는 tilt로 눌러 타원 궤도, 깊이(z)는 크기·투명도로 표현한다. */

const CX = 380;
const CY = 300;
const TILT = 0.62;

interface NodeDef {
  name: string;
  /** 관계도(플레이어와의 관계 강도) 0..1 — 노드 크기가 여기서 파생된다 */
  strength: number;
  color: string;
  edge: string; // 그라디언트 테두리 톤
  design: [number, number]; // θ=0에서의 설계 좌표
  /** 이 노드가 참여하는 관계들 — 클릭은 첫 항목을 선택, 포함된 관계 선택 시 하이라이트 */
  selKeys?: RelKey[];
  isPlayer?: boolean;
}

// 관계도 → 반지름: 강한 관계일수록 크게 보인다 (Kuzu 관계 그래프가 붙으면 실측값)
const NODE_R_MIN = 15;
const NODE_R_MAX = 28;
const nodeRadius = (n: NodeDef) => (n.isPlayer ? 29 : NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * n.strength);

// 중심은 플레이어다 — 관계 그래프는 '내 사람들'을 보는 화면 (ADR-014 Relationship).
// 김민지는 가장 가까운 위성, 철수·수진은 민지를 통해 이어진 2-hop 인물.
const NODES: NodeDef[] = [
  { name: "당신", strength: 1, color: "#D9E2F2", edge: "#6D8DD6", design: [CX, CY], selKeys: ["me"], isPlayer: true },
  { name: "김민지", strength: 0.92, color: "#AFC8F5", edge: "#7FA3E8", design: [295, 205], selKeys: ["me", "mc", "ms"] },
  { name: "이철수", strength: 0.48, color: "#FFE9A8", edge: "#EBCB6E", design: [165, 150], selKeys: ["mc"] },
  { name: "박수진", strength: 0.55, color: "#C9B8F0", edge: "#A98FE0", design: [565, 165], selKeys: ["ms"] },
  { name: "한하린", strength: 0.24, color: "#F5B8CB", edge: "#E391AF", design: [645, 355] },
  { name: "정도윤", strength: 0.18, color: "#BFE3D0", edge: "#8FCBAA", design: [205, 445] },
];

/** 설계 좌표 → 궤도 극좌표 (θ=0에서 설계 좌표를 재현하도록 역산) */
const ORBITS = Object.fromEntries(
  NODES.map((n) => {
    const dx = n.design[0] - CX;
    const dy = (n.design[1] - CY) / TILT;
    return [n.name, { radius: Math.hypot(dx, dy), angle: Math.atan2(dy, dx) }];
  }),
);

interface Placed {
  x: number;
  y: number;
  depth: number; // 0(맨 뒤)..1(맨 앞)
  scale: number;
}

function place(name: string, theta: number, zoom: number): Placed {
  const orbit = ORBITS[name];
  if (orbit.radius === 0) return { x: CX, y: CY, depth: 0.5, scale: 1 };
  const a = orbit.angle + theta;
  const depth = (Math.sin(a) + 1) / 2; // 화면 아래(y+)가 앞
  return {
    x: CX + orbit.radius * zoom * Math.cos(a),
    y: CY + orbit.radius * zoom * Math.sin(a) * TILT,
    depth,
    scale: (0.82 + 0.3 * depth) * (0.85 + 0.15 * zoom),
  };
}

type EdgeKind = "conflict" | "trust" | "close" | "faint";

interface EdgeDef {
  id: string;
  selKey?: RelKey;
  from: string;
  to: string;
  kind: EdgeKind;
  width: number;
  bend: number;
  label?: string;
}

const EDGES: EdgeDef[] = [
  { id: "bg1", from: "박수진", to: "한하린", kind: "faint", width: 2.5, bend: 0.18 },
  { id: "bg2", from: "이철수", to: "박수진", kind: "faint", width: 2, bend: -0.1 },
  { id: "bg3", from: "김민지", to: "정도윤", kind: "faint", width: 2, bend: 0.15 },
  { id: "mc", selKey: "mc", from: "김민지", to: "이철수", kind: "conflict", width: 4.5, bend: -0.2, label: "갈등 중" },
  { id: "ms", selKey: "ms", from: "김민지", to: "박수진", kind: "trust", width: 4, bend: 0.18, label: "신뢰 회복 중" },
  { id: "me", selKey: "me", from: "당신", to: "김민지", kind: "close", width: 5.5, bend: 0.2, label: "가까운 사이" },
];

const KIND_STYLE: Record<EdgeKind, { from: string; to: string; dash?: string }> = {
  conflict: { from: "#F7A8C4", to: "#E36F9A", dash: "11 8" },
  trust: { from: "#9FDBBB", to: "#5FBF95" },
  close: { from: "#9FBCF2", to: "#6D8DD6" },
  faint: { from: "#E2EAF6", to: "#D4DFF0" },
};

function quadPath(a: Placed, b: Placed, bend: number): { d: string; mid: [number, number] } {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const cx = (a.x + b.x) / 2 + (-dy / len) * len * bend;
  const cy = (a.y + b.y) / 2 + (dx / len) * len * bend;
  return {
    d: `M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`,
    mid: [0.25 * a.x + 0.5 * cx + 0.25 * b.x, 0.25 * a.y + 0.5 * cy + 0.25 * b.y],
  };
}

function EdgeGroup({
  edge,
  a,
  b,
  selected,
  onSelect,
}: {
  edge: EdgeDef;
  a: Placed;
  b: Placed;
  selected: boolean;
  onSelect?: () => void;
}) {
  const { d, mid } = quadPath(a, b, edge.bend);
  const style = KIND_STYLE[edge.kind];
  const gradId = `lf-eg-${edge.id}`;
  const pathId = `lf-ep-${edge.id}`;
  const flowing = edge.kind === "conflict" || selected;
  const depthOpacity = 0.55 + 0.45 * ((a.depth + b.depth) / 2);

  if (edge.kind === "faint") {
    return (
      <path
        d={d}
        stroke={style.from}
        strokeWidth={edge.width}
        fill="none"
        strokeLinecap="round"
        opacity={depthOpacity * 0.9}
      />
    );
  }

  return (
    <g className={styles.edgeGroup} onClick={onSelect} opacity={depthOpacity}>
      <defs>
        <linearGradient id={gradId} gradientUnits="userSpaceOnUse" x1={a.x} y1={a.y} x2={b.x} y2={b.y}>
          <stop offset="0%" stopColor={style.from} />
          <stop offset="100%" stopColor={style.to} />
        </linearGradient>
      </defs>

      {/* 헤일로 — 블러 글로우. 선택되면 숨쉰다 */}
      <path
        className={styles.edgeHalo}
        d={d}
        stroke={`url(#${gradId})`}
        strokeWidth={edge.width + 9}
        fill="none"
        strokeLinecap="round"
        filter="url(#lf-glow)"
        style={selected ? { opacity: 0.7, animation: "lf-breathe 2.6s ease-in-out infinite" } : undefined}
      />

      {/* 본선 */}
      <path
        id={pathId}
        d={d}
        stroke={`url(#${gradId})`}
        strokeWidth={selected ? edge.width + 1 : edge.width}
        fill="none"
        strokeLinecap="round"
        strokeDasharray={style.dash}
        style={flowing && style.dash ? { animation: "lf-dash 1.6s linear infinite" } : undefined}
      />

      {/* 선택 에지 위를 흐르는 입자 — 관계가 살아있다 */}
      {selected &&
        [0, 1.1].map((delay) => (
          <circle key={delay} r={3.2} fill="#ffffff" stroke={style.to} strokeWidth={1.2} opacity={0.95}>
            <animateMotion dur="2.2s" begin={`${delay}s`} repeatCount="indefinite">
              <mpath href={`#${pathId}`} />
            </animateMotion>
          </circle>
        ))}

      {/* 넉넉한 히트 영역 */}
      <path d={d} stroke="transparent" strokeWidth={26} fill="none" />

      {/* 관계 라벨 필 */}
      {edge.label && (
        <g className={styles.edgeLabel}>
          <rect
            x={mid[0] - edge.label.length * 6.4 - 10}
            y={mid[1] - 13}
            width={edge.label.length * 12.8 + 20}
            height={26}
            rx={13}
            fill="#ffffff"
            stroke={selected ? style.to : "#E2EAF6"}
            strokeWidth={1.5}
            filter="url(#lf-soft)"
          />
          <text
            x={mid[0]}
            y={mid[1] + 4.5}
            textAnchor="middle"
            fontSize={12.5}
            fontWeight={800}
            fill={selected ? style.to : "#6B7691"}
          >
            {edge.label}
          </text>
        </g>
      )}
    </g>
  );
}

function NodeGroup({
  node,
  at,
  selected,
  onSelect,
}: {
  node: NodeDef;
  at: Placed;
  selected: boolean;
  onSelect?: () => void;
}) {
  const { x: cx, y: cy } = at;
  const r = nodeRadius(node) * at.scale;
  const gradId = `lf-ng-${node.name}`;
  const depthOpacity = 0.68 + 0.32 * at.depth;
  return (
    <g className={styles.nodeGroup} onClick={onSelect} opacity={node.isPlayer ? 1 : depthOpacity}>
      <defs>
        <radialGradient id={gradId} cx="38%" cy="30%" r="80%">
          <stop offset="0%" stopColor="#ffffff" stopOpacity={0.9} />
          <stop offset="45%" stopColor={node.color} />
          <stop offset="100%" stopColor={node.edge} />
        </radialGradient>
      </defs>

      {/* 선택 소나 펄스 */}
      {selected &&
        [0, 0.9].map((delay) => (
          <circle
            key={delay}
            cx={cx}
            cy={cy}
            r={r + 6}
            fill="none"
            stroke="#6D8DD6"
            strokeWidth={2}
            style={{
              transformBox: "fill-box",
              transformOrigin: "center",
              animation: `lf-sonar 1.8s ${delay}s ease-out infinite`,
            }}
          />
        ))}

      {/* 화이트 링 + 본체 */}
      <circle cx={cx} cy={cy} r={r + 4} fill="#ffffff" filter="url(#lf-soft)" />
      {selected && <circle cx={cx} cy={cy} r={r + 5.5} fill="none" stroke="#6D8DD6" strokeWidth={2.5} />}
      {/* 플레이어(당신) 노드는 점선 오라 — 세계 밖에서 온 존재 */}
      {node.isPlayer && (
        <circle cx={cx} cy={cy} r={r + 10} fill="none" stroke="#6D8DD6" strokeWidth={1.5} strokeDasharray="3 5" opacity={0.6} />
      )}
      <circle cx={cx} cy={cy} r={r} fill={`url(#${gradId})`} />

      {/* 얼굴 — 프로토타입 블롭 비율 그대로 (눈 11%, 입 18%) */}
      <circle cx={cx - r * 0.42} cy={cy - r * 0.18} r={r * 0.11} fill="#3A4256" />
      <circle cx={cx + r * 0.42} cy={cy - r * 0.18} r={r * 0.11} fill="#3A4256" />
      <path
        d={`M ${cx - r * 0.28} ${cy + r * 0.28} Q ${cx} ${cy + r * 0.52} ${cx + r * 0.28} ${cy + r * 0.28}`}
        stroke="#3A4256"
        strokeWidth={2}
        fill="none"
        strokeLinecap="round"
      />

      {/* 이름 라벨 — 흰 스트로크 헤일로로 배경 위에서도 또렷하게 */}
      <text
        x={cx}
        y={cy + r + 21}
        textAnchor="middle"
        fontSize={13}
        fontWeight={800}
        fill={selected ? "#3A4256" : "#6B7691"}
        stroke="#F8FBFF"
        strokeWidth={4}
        paintOrder="stroke"
      >
        {node.name}
      </text>
    </g>
  );
}

const LEGEND: { label: string; kind: EdgeKind }[] = [
  { label: "갈등", kind: "conflict" },
  { label: "신뢰", kind: "trust" },
  { label: "친밀", kind: "close" },
];

/** 궤도 캔버스 — 드래그 회전(관성) + 휠 줌 + 더블클릭 리셋 */
function GraphCanvas({ sel, onSelect }: GraphTabProps) {
  const [theta, setTheta] = useState(0);
  const [zoom, setZoom] = useState(1);
  const drag = useRef({ active: false, lastX: 0, moved: 0, velocity: 0, lastAt: 0 });
  const raf = useRef<number | undefined>(undefined);

  // 관성 루프 — 드래그를 놓으면 서서히 감속
  useEffect(() => {
    const step = () => {
      const d = drag.current;
      if (!d.active && Math.abs(d.velocity) > 0.0004) {
        setTheta((t) => t + d.velocity);
        d.velocity *= 0.94;
      }
      raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return () => {
      if (raf.current !== undefined) cancelAnimationFrame(raf.current);
    };
  }, []);

  const onPointerDown = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    // setPointerCapture 금지 — 캡처는 click까지 svg로 리타게팅해 에지/노드 선택을 죽인다
    drag.current = { active: true, lastX: e.clientX, moved: 0, velocity: 0, lastAt: e.timeStamp };
  }, []);

  const onPointerMove = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d.active) return;
    const dx = e.clientX - d.lastX;
    d.lastX = e.clientX;
    d.moved += Math.abs(dx);
    const dTheta = dx * 0.006;
    d.velocity = dTheta;
    setTheta((t) => t + dTheta);
  }, []);

  const onPointerUp = useCallback(() => {
    drag.current.active = false;
  }, []);

  // 드래그였다면 click 무시 — 회전 끝에 관계가 바뀌는 오조작 방지
  const guarded = useCallback(
    (key: RelKey) => () => {
      if (drag.current.moved < 6) onSelect(key);
    },
    [onSelect],
  );

  const onWheel = useCallback((e: React.WheelEvent<SVGSVGElement>) => {
    setZoom((z) => Math.min(1.35, Math.max(0.7, z * (e.deltaY > 0 ? 0.93 : 1.075))));
  }, []);

  const onDoubleClick = useCallback(() => {
    drag.current.velocity = 0;
    setTheta(0);
    setZoom(1);
  }, []);

  const placed = useMemo(() => {
    const map: Record<string, Placed> = {};
    for (const n of NODES) map[n.name] = place(n.name, theta, zoom);
    return map;
  }, [theta, zoom]);

  // 깊이 정렬 — 뒤의 노드가 먼저 그려진다 (중심 김민지는 중간층)
  const nodesByDepth = useMemo(
    () => [...NODES].sort((a, b) => placed[a.name].depth - placed[b.name].depth),
    [placed],
  );

  return (
    <svg
      width="100%"
      height="100%"
      viewBox="0 0 760 620"
      preserveAspectRatio="xMidYMid meet"
      style={{
        position: "absolute",
        inset: 0,
        cursor: drag.current.active ? "grabbing" : "grab",
        touchAction: "none",
        userSelect: "none",
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
      onWheel={onWheel}
      onDoubleClick={onDoubleClick}
    >
      <defs>
        {/* 도트 그리드 — 공간감 */}
        <pattern id="lf-dots" width="26" height="26" patternUnits="userSpaceOnUse">
          <circle cx="2" cy="2" r="1.3" fill="#DDE7F5" />
        </pattern>
        {/* 중심 비네트 — 민지가 이야기의 중심임을 은은하게 */}
        <radialGradient id="lf-vignette" cx="50%" cy="48%" r="55%">
          <stop offset="0%" stopColor="#EDF3FD" />
          <stop offset="100%" stopColor="#EDF3FD" stopOpacity={0} />
        </radialGradient>
        <filter id="lf-glow" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="5" />
        </filter>
        <filter id="lf-soft" x="-30%" y="-30%" width="160%" height="160%">
          <feDropShadow dx="0" dy="2.5" stdDeviation="3.5" floodColor="#6D8DD6" floodOpacity="0.22" />
        </filter>
      </defs>

      <rect width="760" height="620" fill="url(#lf-dots)" />
      <rect width="760" height="620" fill="url(#lf-vignette)" />

      {/* 궤도 가이드 타원 — 아주 옅게 (안: 가장 가까운 사람, 밖: 알게 된 반경) */}
      {[175, 300].map((radius) => (
        <ellipse
          key={radius}
          cx={CX}
          cy={CY}
          rx={radius * zoom}
          ry={radius * zoom * TILT}
          fill="none"
          stroke="#DCE6F5"
          strokeWidth={1.5}
          strokeDasharray="2 7"
          opacity={0.8}
        />
      ))}

      {EDGES.map((edge) => (
        <EdgeGroup
          key={edge.id}
          edge={edge}
          a={placed[edge.from]}
          b={placed[edge.to]}
          selected={edge.selKey !== undefined && edge.selKey === sel}
          onSelect={edge.selKey ? guarded(edge.selKey) : undefined}
        />
      ))}

      {nodesByDepth.map((node) => (
        <NodeGroup
          key={node.name}
          node={node}
          at={placed[node.name]}
          selected={node.selKeys?.includes(sel) ?? false}
          onSelect={node.selKeys ? guarded(node.selKeys[0]) : undefined}
        />
      ))}
    </svg>
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
          <GraphCanvas sel={sel} onSelect={onSelect} />

          {/* 범례 + 조작 힌트 */}
          <div
            style={{
              position: "absolute",
              left: 18,
              bottom: 16,
              display: "flex",
              alignItems: "center",
              gap: 14,
              background: "rgba(255,255,255,0.85)",
              border: "1.5px solid #E2EAF6",
              borderRadius: 9999,
              padding: "7px 14px",
              backdropFilter: "blur(4px)",
              pointerEvents: "none",
            }}
          >
            {LEGEND.map((item) => (
              <div key={item.label} style={{ display: "flex", alignItems: "center", gap: 5 }}>
                <div
                  style={{
                    width: 16,
                    height: 4,
                    borderRadius: 9999,
                    background: `linear-gradient(90deg, ${KIND_STYLE[item.kind].from}, ${KIND_STYLE[item.kind].to})`,
                  }}
                />
                <div style={{ fontSize: 11.5, fontWeight: 800, color: "#6B7691" }}>{item.label}</div>
              </div>
            ))}
            <div style={{ width: 1, height: 14, background: "#E2EAF6" }} />
            <div style={{ fontSize: 11.5, fontWeight: 700, color: "#8C97AF" }}>
              드래그 회전 · 휠 확대 · 더블클릭 초기화
            </div>
          </div>
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
