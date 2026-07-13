"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ActorIdentity } from "@/lib/actors";
import type { LiveRelEdge, RelDimensions } from "@/lib/graph";

import styles from "./lf.module.css";

interface GraphTabProps {
  /** kuzu-projector 실측 관계 (ADR-006) — player와 닿은 엣지 전부 */
  edges: LiveRelEdge[];
  available: boolean;
  /** actor_id → 표시 이름 (라이브 identity, 하드코딩 금지) */
  nameOf: (actorId: string) => string;
  identityOf: (actorId: string) => ActorIdentity | undefined;
  /** 선택된 관계 상대 (없으면 null) */
  selected: string | null;
  onSelect: (actorId: string | null) => void;
}

/* ── 궤도 캔버스 좌표계 (viewBox 760×620) ──
   플레이어(당신)가 중심(회전축), 실측 관계 액터들이 궤도 위성이다.
   위성의 궤도 반경은 관계도(strength)에서, 각도는 목록 순서에서 파생된다. */
const CX = 380;
const CY = 300;
const TILT = 0.62;

const STAGE_KO: Record<string, string> = {
  stranger: "낯선 사이",
  acquaintance: "아는 사이",
  friend: "친구",
  close_friend: "가까운 사이",
  romantic: "연인",
  family: "가족 같은",
  mentor: "멘토",
  rival: "라이벌",
  enemy: "적대",
};

const NODE_PALETTE: [string, string][] = [
  ["#AFC8F5", "#7FA3E8"],
  ["#F5B8CB", "#E391AF"],
  ["#BFE3D0", "#8FCBAA"],
  ["#E8D5A8", "#D6B86A"],
  ["#C9B8F0", "#A98FE0"],
  ["#F5C8B8", "#E8977F"],
  ["#B8E0F5", "#7FB4E8"],
];

type EdgeKind = "conflict" | "trust" | "close" | "faint";

const KIND_STYLE: Record<EdgeKind, { from: string; to: string; dash?: string }> = {
  conflict: { from: "#F7A8C4", to: "#E36F9A", dash: "11 8" },
  trust: { from: "#9FDBBB", to: "#5FBF95" },
  close: { from: "#9FBCF2", to: "#6D8DD6" },
  faint: { from: "#E2EAF6", to: "#D4DFF0" },
};

function edgeKind(d: RelDimensions): EdgeKind {
  if (d.resentment >= 0.3) return "conflict";
  if (d.trust >= 0.2) return "trust";
  if (d.intimacy >= 0.15) return "close";
  return "faint";
}

interface NodeDef {
  actorId: string | null; // null = 플레이어(당신)
  name: string;
  strength: number;
  color: string;
  edge: string;
  design: [number, number];
  isPlayer?: boolean;
}

interface EdgeDef {
  actorId: string;
  from: string;
  to: string;
  kind: EdgeKind;
  width: number;
  label: string;
}

const NODE_R_MIN = 15;
const NODE_R_MAX = 28;
const nodeRadius = (n: NodeDef) =>
  n.isPlayer ? 29 : NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * n.strength;

/** 실측 엣지 → 궤도 노드. 강한 관계는 안쪽 궤도, 목록 순서로 각을 나눈다. */
function buildNodes(edges: LiveRelEdge[], nameOf: (id: string) => string): NodeDef[] {
  const player: NodeDef = {
    actorId: null,
    name: "당신",
    strength: 1,
    color: "#D9E2F2",
    edge: "#6D8DD6",
    design: [CX, CY],
    isPlayer: true,
  };
  const n = edges.length;
  const actors = edges.map((e, i) => {
    const angle = -Math.PI / 2 + (i / Math.max(1, n)) * Math.PI * 2;
    const radius = 165 + (1 - Math.min(1, e.strength)) * 125; // 강할수록 가깝게
    const [color, ring] = NODE_PALETTE[i % NODE_PALETTE.length];
    return {
      actorId: e.actorId,
      name: nameOf(e.actorId),
      strength: Math.max(0.08, e.strength),
      color,
      edge: ring,
      design: [CX + radius * Math.cos(angle), CY + radius * Math.sin(angle) * TILT] as [
        number,
        number,
      ],
    };
  });
  return [player, ...actors];
}

function buildEdges(edges: LiveRelEdge[], nameOf: (id: string) => string): EdgeDef[] {
  return edges.map((e) => {
    const kind = edgeKind(e.dimensions);
    return {
      actorId: e.actorId,
      from: "당신",
      to: nameOf(e.actorId),
      kind,
      width: 2.5 + 3.5 * Math.min(1, e.strength),
      label: STAGE_KO[e.stage] ?? e.stage,
    };
  });
}

function orbitsFor(nodes: NodeDef[]): Record<string, { radius: number; angle: number }> {
  return Object.fromEntries(
    nodes.map((n) => {
      const dx = n.design[0] - CX;
      const dy = (n.design[1] - CY) / TILT;
      return [n.name, { radius: Math.hypot(dx, dy), angle: Math.atan2(dy, dx) }];
    }),
  );
}

interface Placed {
  x: number;
  y: number;
  depth: number;
  scale: number;
}
type Orbits = Record<string, { radius: number; angle: number }>;

function place(orbits: Orbits, name: string, theta: number, zoom: number): Placed {
  const orbit = orbits[name];
  if (!orbit || orbit.radius === 0) return { x: CX, y: CY, depth: 0.5, scale: 1 };
  const a = orbit.angle + theta;
  const depth = (Math.sin(a) + 1) / 2;
  return {
    x: CX + orbit.radius * zoom * Math.cos(a),
    y: CY + orbit.radius * zoom * Math.sin(a) * TILT,
    depth,
    scale: (0.82 + 0.3 * depth) * (0.85 + 0.15 * zoom),
  };
}

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
  onSelect: () => void;
}) {
  const { d, mid } = quadPath(a, b, 0.16);
  const style = KIND_STYLE[edge.kind];
  const gradId = `lf-eg-${edge.actorId}`;
  const pathId = `lf-ep-${edge.actorId}`;
  const flowing = edge.kind === "conflict" || selected;
  const depthOpacity = 0.55 + 0.45 * ((a.depth + b.depth) / 2);

  return (
    <g className={styles.edgeGroup} onClick={onSelect} opacity={depthOpacity}>
      <defs>
        <linearGradient id={gradId} gradientUnits="userSpaceOnUse" x1={a.x} y1={a.y} x2={b.x} y2={b.y}>
          <stop offset="0%" stopColor={style.from} />
          <stop offset="100%" stopColor={style.to} />
        </linearGradient>
      </defs>
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
      {selected &&
        [0, 1.1].map((delay) => (
          <circle key={delay} r={3.2} fill="#ffffff" stroke={style.to} strokeWidth={1.2} opacity={0.95}>
            <animateMotion dur="2.2s" begin={`${delay}s`} repeatCount="indefinite">
              <mpath href={`#${pathId}`} />
            </animateMotion>
          </circle>
        ))}
      <path d={d} stroke="transparent" strokeWidth={26} fill="none" />
      {selected && (
        <g className={styles.edgeLabel}>
          <rect
            x={mid[0] - edge.label.length * 6.4 - 10}
            y={mid[1] - 13}
            width={edge.label.length * 12.8 + 20}
            height={26}
            rx={13}
            fill="#ffffff"
            stroke={style.to}
            strokeWidth={1.5}
            filter="url(#lf-soft)"
          />
          <text x={mid[0]} y={mid[1] + 4.5} textAnchor="middle" fontSize={12.5} fontWeight={800} fill={style.to}>
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
      <circle cx={cx} cy={cy} r={r + 4} fill="#ffffff" filter="url(#lf-soft)" />
      {selected && <circle cx={cx} cy={cy} r={r + 5.5} fill="none" stroke="#6D8DD6" strokeWidth={2.5} />}
      {node.isPlayer && (
        <circle cx={cx} cy={cy} r={r + 10} fill="none" stroke="#6D8DD6" strokeWidth={1.5} strokeDasharray="3 5" opacity={0.6} />
      )}
      <circle cx={cx} cy={cy} r={r} fill={`url(#${gradId})`} />
      <circle cx={cx - r * 0.42} cy={cy - r * 0.18} r={r * 0.11} fill="#3A4256" />
      <circle cx={cx + r * 0.42} cy={cy - r * 0.18} r={r * 0.11} fill="#3A4256" />
      <path
        d={`M ${cx - r * 0.28} ${cy + r * 0.28} Q ${cx} ${cy + r * 0.52} ${cx + r * 0.28} ${cy + r * 0.28}`}
        stroke="#3A4256"
        strokeWidth={2}
        fill="none"
        strokeLinecap="round"
      />
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

function GraphCanvas({
  nodes,
  edges,
  selected,
  onSelect,
}: {
  nodes: NodeDef[];
  edges: EdgeDef[];
  selected: string | null;
  onSelect: (actorId: string | null) => void;
}) {
  const [theta, setTheta] = useState(0);
  const [zoom, setZoom] = useState(1);
  const drag = useRef({ active: false, lastX: 0, moved: 0, velocity: 0, lastAt: 0 });
  const raf = useRef<number | undefined>(undefined);

  const orbits = useMemo(() => orbitsFor(nodes), [nodes]);

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
  const guarded = useCallback(
    (actorId: string | null) => () => {
      if (drag.current.moved < 6) onSelect(actorId);
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
    for (const n of nodes) map[n.name] = place(orbits, n.name, theta, zoom);
    return map;
  }, [nodes, orbits, theta, zoom]);

  const nodesByDepth = useMemo(
    () => [...nodes].sort((a, b) => placed[a.name].depth - placed[b.name].depth),
    [nodes, placed],
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
        <pattern id="lf-dots" width="26" height="26" patternUnits="userSpaceOnUse">
          <circle cx="2" cy="2" r="1.3" fill="#DDE7F5" />
        </pattern>
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

      {edges.map((edge) => (
        <EdgeGroup
          key={edge.actorId}
          edge={edge}
          a={placed[edge.from]}
          b={placed[edge.to]}
          selected={edge.actorId === selected}
          onSelect={guarded(edge.actorId)}
        />
      ))}

      {nodesByDepth.map((node) => (
        <NodeGroup
          key={node.name}
          node={node}
          at={placed[node.name]}
          selected={node.actorId !== null && node.actorId === selected}
          onSelect={node.isPlayer ? undefined : guarded(node.actorId)}
        />
      ))}
    </svg>
  );
}

const DIM_META: { key: keyof RelDimensions; label: string; color: string }[] = [
  { key: "trust", label: "신뢰", color: "#5FBF95" },
  { key: "intimacy", label: "친밀", color: "#6D8DD6" },
  { key: "respect", label: "존중", color: "#7FA3E8" },
  { key: "attraction", label: "끌림", color: "#C76F93" },
  { key: "resentment", label: "원한", color: "#E36F9A" },
];

function RelationshipPanel({
  edge,
  identity,
  name,
}: {
  edge: LiveRelEdge;
  identity: ActorIdentity | undefined;
  name: string;
}) {
  return (
    <>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ fontSize: 18, fontWeight: 900 }}>{name}</div>
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
            {STAGE_KO[edge.stage] ?? edge.stage}
          </div>
        </div>
        <div style={{ fontSize: 13, fontWeight: 800, color: "#6B7691" }}>
          관계도 {Math.round(edge.strength * 100)}%
        </div>
      </div>

      {identity?.bio && (
        <div style={{ fontSize: 13, lineHeight: 1.6, color: "#6B7691", fontWeight: 600 }}>
          {identity.bio}
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ fontSize: 12, fontWeight: 800, color: "#8C97AF" }}>관계의 결 (실측)</div>
        {DIM_META.map((dim) => {
          const value = edge.dimensions[dim.key];
          const magnitude = Math.min(1, Math.abs(value));
          return (
            <div key={dim.key} style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ width: 34, fontSize: 12, fontWeight: 700, color: "#6B7691" }}>
                {dim.label}
              </div>
              <div
                style={{
                  flex: 1,
                  height: 8,
                  borderRadius: 9999,
                  background: "#EEF3FB",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: `${Math.round(magnitude * 100)}%`,
                    height: "100%",
                    borderRadius: 9999,
                    background: value < 0 ? "#C0808F" : dim.color,
                  }}
                />
              </div>
              <div style={{ width: 40, textAlign: "right", fontSize: 12, fontWeight: 700, color: "#8C97AF" }}>
                {value >= 0 ? "" : "−"}
                {Math.round(magnitude * 100)}
              </div>
            </div>
          );
        })}
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
        <span style={{ fontWeight: 800, color: "#3A4256" }}>개입할 수 있어요</span> — DM이나 댓글로
        {` ${name}`}과의 관계가 실제로 움직입니다. 개입은 흔적을 남겨요.
      </div>
    </>
  );
}

export function GraphTab({ edges, available, nameOf, identityOf, selected, onSelect }: GraphTabProps) {
  const nodes = useMemo(() => buildNodes(edges, nameOf), [edges, nameOf]);
  const edgeDefs = useMemo(() => buildEdges(edges, nameOf), [edges, nameOf]);
  const selectedEdge = edges.find((e) => e.actorId === selected) ?? null;

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
          노드를 클릭해 그 관계의 결을 보세요
        </div>
      </div>

      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        <div style={{ flex: 1, position: "relative", background: "#F8FBFF" }}>
          {edges.length > 0 ? (
            <GraphCanvas nodes={nodes} edges={edgeDefs} selected={selected} onSelect={onSelect} />
          ) : (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: 40,
                textAlign: "center",
                color: "#8C97AF",
                fontSize: 14,
                fontWeight: 600,
                lineHeight: 1.7,
              }}
            >
              아직 이어진 관계가 없어요.
              <br />
              피드나 DM으로 개입하면 세계가 당신을 관계망에 새깁니다.
            </div>
          )}

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
            <div style={{ width: 1, height: 14, background: "#E2EAF6" }} />
            <div style={{ fontSize: 11.5, fontWeight: 800, color: available ? "#3E8A66" : "#8C97AF" }}>
              {available ? "관계도 실측 연결됨" : "관계 데이터 대기 중"}
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
          {selectedEdge ? (
            <RelationshipPanel
              edge={selectedEdge}
              identity={identityOf(selectedEdge.actorId)}
              name={nameOf(selectedEdge.actorId)}
            />
          ) : (
            <div style={{ fontSize: 13, lineHeight: 1.7, color: "#8C97AF", fontWeight: 600 }}>
              {edges.length > 0
                ? "왼쪽 그래프에서 인물을 클릭하면, 그 관계의 실측 결(신뢰·친밀·원한)과 현재 단계가 여기 나타납니다."
                : "아직 관계가 없어요. 세계에 개입하면 관계망이 자라납니다."}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
