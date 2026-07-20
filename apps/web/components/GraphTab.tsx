"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ActorIdentity } from "@/lib/actors";
import { PLAYER_ID } from "@/lib/config";
import type { LiveRelEdge, RelDimensions, WorldGraphState, WorldRelEdge } from "@/lib/graph";

import styles from "./lf.module.css";

interface GraphTabProps {
  /** kuzu-projector 실측 관계 (ADR-006) — player와 닿은 엣지 전부 */
  edges: LiveRelEdge[];
  available: boolean;
  /** 세계 전체 관계망 — 액터↔액터 포함 (feed-api /graph/world) */
  world: WorldGraphState;
  /** actor_id → 표시 이름 (라이브 identity, 하드코딩 금지) */
  nameOf: (actorId: string) => string;
  identityOf: (actorId: string) => ActorIdentity | undefined;
  /** 선택된 관계 상대 (없으면 null) — '나의 관계' 뷰 전용 */
  selected: string | null;
  onSelect: (actorId: string | null) => void;
}

/* ── 궤도 캔버스 좌표계 (viewBox 760×620) ──
   중심 인물이 회전축, 나머지가 궤도 위성이다. '나의 관계'에서는 당신이 중심,
   '세계의 관계'는 링 배치(중심 없음) 또는 클릭한 인물 중심의 재중심 뷰다. */
const CX = 380;
const CY = 300;
const TILT = 0.62;

/** 에고 뷰의 플레이어 노드 키 — 액터·플레이어 id(a_…, p_…)와 충돌하지 않는 예약 키 */
const YOU_KEY = "__you__";

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

const PLAYER_COLORS: [string, string] = ["#D9E2F2", "#6D8DD6"];

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
  key: string; // placed 맵·SVG id의 안정 키 — 이름은 겹칠 수 있다('누군가')
  actorId: string | null; // null = 플레이어(당신)
  name: string;
  strength: number;
  color: string;
  edge: string;
  design: [number, number];
  isCenter?: boolean;
}

interface EdgeDef {
  id: string;
  from: string; // node key
  to: string; // node key
  kind: EdgeKind;
  width: number;
  label: string;
  /** 강도 → 투명도 (세계 뷰) — 미지정이면 1 */
  baseOpacity?: number;
}

const NODE_R_MIN = 15;
const NODE_R_MAX = 28;
const nodeRadius = (n: NodeDef) =>
  n.isCenter ? 29 : NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * n.strength;

/** 실측 엣지 → 궤도 노드. 강한 관계는 안쪽 궤도, 목록 순서로 각을 나눈다. */
function buildNodes(edges: LiveRelEdge[], nameOf: (id: string) => string): NodeDef[] {
  const player: NodeDef = {
    key: YOU_KEY,
    actorId: null,
    name: "당신",
    strength: 1,
    color: PLAYER_COLORS[0],
    edge: PLAYER_COLORS[1],
    design: [CX, CY],
    isCenter: true,
  };
  const n = edges.length;
  const actors = edges.map((e, i) => {
    const angle = -Math.PI / 2 + (i / Math.max(1, n)) * Math.PI * 2;
    const radius = 165 + (1 - Math.min(1, e.strength)) * 125; // 강할수록 가깝게
    const [color, ring] = NODE_PALETTE[i % NODE_PALETTE.length];
    return {
      key: e.actorId,
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

function buildEdges(edges: LiveRelEdge[]): EdgeDef[] {
  return edges.map((e) => {
    const kind = edgeKind(e.dimensions);
    return {
      id: e.actorId,
      from: YOU_KEY,
      to: e.actorId,
      kind,
      width: 2.5 + 3.5 * Math.min(1, e.strength),
      label: STAGE_KO[e.stage] ?? e.stage,
    };
  });
}

/* ── 세계의 관계 — 방향 간선을 시각 쌍으로 접고, 수치는 정성 어휘로 바꾼다 ── */

interface WorldPair {
  id: string; // "a--b" (정렬) — SVG id에 안전
  a: string;
  b: string;
  weight: number; // 표시 가중치 — 방향별 max (서버 정의: 유대·긴장 중 큰 쪽)
  kind: EdgeKind;
  stage: string;
  dims: RelDimensions; // 우세 방향의 차원
}

function mergeWorldPairs(edges: WorldRelEdge[]): WorldPair[] {
  const byPair = new Map<string, WorldPair>();
  for (const e of edges) {
    const [a, b] = e.fromId < e.toId ? [e.fromId, e.toId] : [e.toId, e.fromId];
    const id = `${a}--${b}`;
    const existing = byPair.get(id);
    if (existing === undefined || e.weight > existing.weight) {
      byPair.set(id, {
        id,
        a,
        b,
        weight: existing !== undefined ? Math.max(existing.weight, e.weight) : e.weight,
        kind: edgeKind(e.dimensions),
        stage: e.stage,
        dims: e.dimensions,
      });
    }
  }
  return [...byPair.values()].sort((x, y) => y.weight - x.weight);
}

/** 간선의 정성 어휘 — 수치·기계 어휘 무노출 (리시트 정화의 결) */
function worldToneLabel(pair: WorldPair): string {
  if (pair.dims.resentment >= 0.3) return "팽팽한 긴장";
  if (pair.weight >= 0.45) return "깊은 유대";
  if (pair.weight >= 0.18) return "가까워진 사이";
  if (pair.weight >= 0.06) return "쌓여가는 사이";
  return "스치는 인연";
}

/** 관계의 결을 한 문장으로 — 가장 짙은 차원 하나만 조용히 말한다 */
function textureSentence(d: RelDimensions): string | null {
  const phrases: [number, string][] = [
    [d.resentment, "앙금이 남아 있는 결이에요"],
    [d.trust, "신뢰가 쌓여가는 결이에요"],
    [d.intimacy, "곁을 내주는 결이에요"],
    [d.respect, "서로를 인정하는 결이에요"],
    [d.attraction, "끌림이 감도는 결이에요"],
  ];
  let best: string | null = null;
  let bestValue = 0.05; // 이보다 옅으면 결을 단정하지 않는다
  for (const [value, phrase] of phrases) {
    if (value > bestValue) {
      bestValue = value;
      best = phrase;
    }
  }
  if (best === null && d.trust <= -0.15) return "믿음이 흔들리는 결이에요";
  return best;
}

interface WorldScene {
  nodes: NodeDef[];
  edges: EdgeDef[];
}

/** 세계 뷰 노드·간선 — focus가 없으면 링 배치, 있으면 그 인물 중심의 궤도 배치.

    간선 강도(가중치)는 굵기·투명도로 표현한다. 규모가 작은 세계에서도 결이
    보이도록 화면 내 최댓값 기준 상대 표현이다 — 수치는 어디에도 싣지 않는다. */
function buildWorldScene(
  pairs: WorldPair[],
  focus: string | null,
  displayName: (id: string) => string,
): WorldScene {
  const incident = focus === null ? pairs : pairs.filter((p) => p.a === focus || p.b === focus);

  // 노드 목록: 링은 전체, 재중심은 중심 인물 + 직접 이웃만
  const sums = new Map<string, number>();
  for (const p of incident) {
    sums.set(p.a, (sums.get(p.a) ?? 0) + p.weight);
    sums.set(p.b, (sums.get(p.b) ?? 0) + p.weight);
  }
  if (focus !== null) sums.set(focus, sums.get(focus) ?? 0);
  const maxSum = Math.max(...sums.values(), 0.0001);

  const colorsFor = (id: string, i: number): [string, string] =>
    id === PLAYER_ID ? PLAYER_COLORS : NODE_PALETTE[i % NODE_PALETTE.length];

  let nodes: NodeDef[];
  if (focus === null) {
    // 링 배치 — 인연이 많은 인물부터 시계 방향, 존재감은 크기에만
    const ids = [...sums.keys()].sort(
      (x, y) => (sums.get(y) ?? 0) - (sums.get(x) ?? 0) || x.localeCompare(y),
    );
    const n = ids.length;
    nodes = ids.map((id, i) => {
      const angle = -Math.PI / 2 + (i / Math.max(1, n)) * Math.PI * 2;
      const [color, ring] = colorsFor(id, i);
      return {
        key: id,
        actorId: id,
        name: displayName(id),
        strength: Math.max(0.15, (sums.get(id) ?? 0) / maxSum),
        color,
        edge: ring,
        design: [CX + 235 * Math.cos(angle), CY + 235 * Math.sin(angle) * TILT] as [
          number,
          number,
        ],
      };
    });
  } else {
    // 재중심 — 에고 뷰와 같은 문법: 가까울수록 안쪽 궤도
    const neighbors = incident
      .map((p) => ({ id: p.a === focus ? p.b : p.a, weight: p.weight }))
      .sort((x, y) => y.weight - x.weight);
    const maxW = Math.max(...neighbors.map((nb) => nb.weight), 0.0001);
    const [centerColor, centerRing] = colorsFor(focus, 0);
    const center: NodeDef = {
      key: focus,
      actorId: focus,
      name: displayName(focus),
      strength: 1,
      color: centerColor,
      edge: centerRing,
      design: [CX, CY],
      isCenter: true,
    };
    const n = neighbors.length;
    nodes = [
      center,
      ...neighbors.map((nb, i) => {
        const angle = -Math.PI / 2 + (i / Math.max(1, n)) * Math.PI * 2;
        const radius = 165 + (1 - nb.weight / maxW) * 125;
        const [color, ring] = colorsFor(nb.id, i + 1);
        return {
          key: nb.id,
          actorId: nb.id,
          name: displayName(nb.id),
          strength: Math.max(0.15, nb.weight / maxW),
          color,
          edge: ring,
          design: [CX + radius * Math.cos(angle), CY + radius * Math.sin(angle) * TILT] as [
            number,
            number,
          ],
        };
      }),
    ];
  }

  // 간선: 보이는 노드 사이 전부 — 재중심에서는 이웃끼리의 간선도 옅게 남긴다
  const visible = new Set(nodes.map((node) => node.key));
  const shown = pairs.filter((p) => visible.has(p.a) && visible.has(p.b));
  const maxW = Math.max(...shown.map((p) => p.weight), 0.0001);
  const edges = shown.map((p) => {
    const rel = p.weight / maxW;
    const offFocus = focus !== null && p.a !== focus && p.b !== focus;
    return {
      id: p.id,
      from: p.a,
      to: p.b,
      kind: p.kind,
      width: 2 + 4.5 * rel,
      label: worldToneLabel(p),
      baseOpacity: (0.4 + 0.6 * rel) * (offFocus ? 0.45 : 1),
    };
  });
  return { nodes, edges };
}

function orbitsFor(nodes: NodeDef[]): Record<string, { radius: number; angle: number }> {
  return Object.fromEntries(
    nodes.map((n) => {
      const dx = n.design[0] - CX;
      const dy = (n.design[1] - CY) / TILT;
      return [n.key, { radius: Math.hypot(dx, dy), angle: Math.atan2(dy, dx) }];
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

function place(orbits: Orbits, key: string, theta: number, zoom: number): Placed {
  const orbit = orbits[key];
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
  const gradId = `lf-eg-${edge.id}`;
  const pathId = `lf-ep-${edge.id}`;
  const flowing = edge.kind === "conflict" || selected;
  const depthOpacity = (0.55 + 0.45 * ((a.depth + b.depth) / 2)) * (edge.baseOpacity ?? 1);

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
  const gradId = `lf-ng-${node.key}`;
  const depthOpacity = 0.68 + 0.32 * at.depth;
  return (
    <g className={styles.nodeGroup} onClick={onSelect} opacity={node.isCenter ? 1 : depthOpacity}>
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
      {node.isCenter && (
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
  selectedNode,
  selectedEdge,
  onNodeClick,
  onEdgeClick,
}: {
  nodes: NodeDef[];
  edges: EdgeDef[];
  selectedNode: string | null;
  selectedEdge: string | null;
  onNodeClick: (node: NodeDef) => void;
  onEdgeClick: (edge: EdgeDef) => void;
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
    (fn: () => void) => () => {
      if (drag.current.moved < 6) fn();
    },
    [],
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
    for (const n of nodes) map[n.key] = place(orbits, n.key, theta, zoom);
    return map;
  }, [nodes, orbits, theta, zoom]);

  const nodesByDepth = useMemo(
    () => [...nodes].sort((a, b) => placed[a.key].depth - placed[b.key].depth),
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
          key={edge.id}
          edge={edge}
          a={placed[edge.from]}
          b={placed[edge.to]}
          selected={edge.id === selectedEdge}
          onSelect={guarded(() => onEdgeClick(edge))}
        />
      ))}

      {nodesByDepth.map((node) => (
        <NodeGroup
          key={node.key}
          node={node}
          at={placed[node.key]}
          selected={selectedNode !== null && node.key === selectedNode}
          onSelect={guarded(() => onNodeClick(node))}
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

/* ── 세계 뷰 패널 — 수치 없이, 정성 어휘로만 말한다 ── */

const panelButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  borderRadius: 12,
  border: "1.5px solid #E2EAF6",
  background: "#ffffff",
  color: "#5F7EC9",
  fontSize: 12.5,
  fontWeight: 800,
  cursor: "pointer",
  textAlign: "left",
};

function WorldPairPanel({
  pair,
  displayName,
  onFocus,
}: {
  pair: WorldPair;
  displayName: (id: string) => string;
  onFocus: (id: string) => void;
}) {
  const tone = worldToneLabel(pair);
  const toneColor = KIND_STYLE[pair.kind].to;
  const texture = textureSentence(pair.dims);
  const touchesYou = pair.a === PLAYER_ID || pair.b === PLAYER_ID;
  return (
    <>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ fontSize: 17, fontWeight: 900 }}>
          {displayName(pair.a)} ↔ {displayName(pair.b)}
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <div
            style={{
              padding: "3px 12px",
              background: "#ffffff",
              border: `1.5px solid ${toneColor}`,
              color: toneColor,
              borderRadius: 9999,
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            {tone}
          </div>
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
            {STAGE_KO[pair.stage] ?? pair.stage}
          </div>
        </div>
      </div>

      {texture && (
        <div style={{ fontSize: 13, lineHeight: 1.6, color: "#6B7691", fontWeight: 600 }}>
          {texture}
        </div>
      )}

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
        {touchesYou
          ? "당신이 닿아 있는 인연이에요 — DM이나 댓글로 이 관계는 실제로 움직입니다."
          : "세계가 스스로 엮은 인연이에요 — 당신 없이도 이야기는 흐릅니다."}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {[pair.a, pair.b].map((id) => (
          <button key={id} style={panelButtonStyle} onClick={() => onFocus(id)}>
            {displayName(id)} 중심으로 보기
          </button>
        ))}
      </div>
    </>
  );
}

function WorldFocusPanel({
  focus,
  displayName,
  onClearFocus,
  onGoMine,
}: {
  focus: string;
  displayName: (id: string) => string;
  onClearFocus: () => void;
  onGoMine: () => void;
}) {
  const isYou = focus === PLAYER_ID;
  return (
    <>
      <div style={{ fontSize: 17, fontWeight: 900 }}>
        {displayName(focus)}의 자리에서 본 관계망
      </div>
      <div style={{ fontSize: 13, lineHeight: 1.7, color: "#8C97AF", fontWeight: 600 }}>
        {isYou
          ? "당신을 중심으로 세계를 보고 있어요. 관계의 결을 자세히 보려면 '나의 관계'가 더 깊습니다."
          : "가까운 인연일수록 안쪽 궤도에 있어요. 선을 클릭하면 그 인연의 결이 나타납니다."}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <button style={panelButtonStyle} onClick={onClearFocus}>
          세계 전체 보기
        </button>
        <button style={panelButtonStyle} onClick={onGoMine}>
          나의 관계로 돌아가기
        </button>
      </div>
    </>
  );
}

type Scope = "mine" | "world";

function ScopeChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: "7px 16px",
        borderRadius: 9999,
        border: active ? "1.5px solid #6D8DD6" : "1.5px solid #E2EAF6",
        background: active ? "#EDF3FD" : "#ffffff",
        color: active ? "#3A4256" : "#8C97AF",
        fontSize: 13,
        fontWeight: 800,
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

export const GraphTab = memo(GraphTabInner);

function GraphTabInner({
  edges,
  available,
  world,
  nameOf,
  identityOf,
  selected,
  onSelect,
}: GraphTabProps) {
  const [scope, setScope] = useState<Scope>("mine");
  // 세계 뷰의 재중심 인물과 선택된 간선 — 뷰 로컬 상태 (에고 selected와 분리)
  const [focus, setFocus] = useState<string | null>(null);
  const [worldSel, setWorldSel] = useState<string | null>(null);

  // 이름 그라운딩: 원시 id 무노출 — 나는 '당신', 모르는 인물은 nameOf가 '누군가'로 준다
  const displayName = useCallback(
    (id: string) => (id === PLAYER_ID ? "당신" : nameOf(id)),
    [nameOf],
  );

  const switchScope = useCallback((next: Scope) => {
    setScope(next);
    setFocus(null);
    setWorldSel(null);
  }, []);

  // ── 나의 관계 (현행 유지) ──
  const nodes = useMemo(() => buildNodes(edges, nameOf), [edges, nameOf]);
  const edgeDefs = useMemo(() => buildEdges(edges), [edges]);
  const selectedEdge = edges.find((e) => e.actorId === selected) ?? null;

  // ── 세계의 관계 ──
  const pairs = useMemo(() => mergeWorldPairs(world.edges), [world.edges]);
  const worldScene = useMemo(
    () => buildWorldScene(pairs, focus, displayName),
    [pairs, focus, displayName],
  );
  const selectedPair = useMemo(
    () => (worldSel !== null ? (pairs.find((p) => p.id === worldSel) ?? null) : null),
    [pairs, worldSel],
  );

  const onWorldNodeClick = useCallback((node: NodeDef) => {
    setWorldSel(null);
    setFocus((f) => (f === node.key ? null : node.key)); // 중심 재클릭은 전체로
  }, []);
  const onWorldFocus = useCallback((id: string) => {
    setWorldSel(null);
    setFocus(id);
  }, []);

  const isWorld = scope === "world";
  const canvasEmpty = isWorld ? pairs.length === 0 : edges.length === 0;
  const liveOk = isWorld ? world.available : available;

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
        <div style={{ display: "flex", gap: 8 }}>
          <ScopeChip active={!isWorld} onClick={() => switchScope("mine")}>
            나의 관계
          </ScopeChip>
          <ScopeChip active={isWorld} onClick={() => switchScope("world")}>
            세계의 관계
          </ScopeChip>
        </div>
        <div style={{ fontSize: 13, color: "#8C97AF", fontWeight: 600 }}>
          {isWorld
            ? "세계가 스스로 엮어온 관계망 — 인물을 클릭해 그 중심으로 보세요"
            : "노드를 클릭해 그 관계의 결을 보세요"}
        </div>
      </div>

      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        <div style={{ flex: 1, position: "relative", background: "#F8FBFF" }}>
          {!canvasEmpty ? (
            isWorld ? (
              <GraphCanvas
                nodes={worldScene.nodes}
                edges={worldScene.edges}
                selectedNode={focus}
                selectedEdge={worldSel}
                onNodeClick={onWorldNodeClick}
                onEdgeClick={(edge) => setWorldSel(edge.id)}
              />
            ) : (
              <GraphCanvas
                nodes={nodes}
                edges={edgeDefs}
                selectedNode={selected}
                selectedEdge={selected}
                onNodeClick={(node) => {
                  if (node.actorId !== null) onSelect(node.actorId);
                }}
                onEdgeClick={(edge) => onSelect(edge.id)}
              />
            )
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
              {isWorld ? (
                <>
                  세계가 아직 서로를 엮는 중이에요.
                  <br />
                  액터들이 부딪히고 가까워지면 관계망이 여기 떠오릅니다.
                </>
              ) : (
                <>
                  아직 이어진 관계가 없어요.
                  <br />
                  피드나 DM으로 개입하면 세계가 당신을 관계망에 새깁니다.
                </>
              )}
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
            <div style={{ fontSize: 11.5, fontWeight: 800, color: liveOk ? "#3E8A66" : "#8C97AF" }}>
              {liveOk ? "관계도 실측 연결됨" : "관계 데이터 대기 중"}
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
          {isWorld ? (
            selectedPair !== null ? (
              <WorldPairPanel pair={selectedPair} displayName={displayName} onFocus={onWorldFocus} />
            ) : focus !== null ? (
              <WorldFocusPanel
                focus={focus}
                displayName={displayName}
                onClearFocus={() => setFocus(null)}
                onGoMine={() => switchScope("mine")}
              />
            ) : (
              <div style={{ fontSize: 13, lineHeight: 1.7, color: "#8C97AF", fontWeight: 600 }}>
                {pairs.length > 0 ? (
                  <>
                    당신이 없는 곳에서도 관계는 자라요. 인물을 클릭하면 그 중심으로 다시 보고,
                    선을 클릭하면 그 인연의 결이 나타납니다.
                    {world.truncated && (
                      <>
                        <br />
                        <br />
                        지금은 가장 또렷한 인연만 추려 보여주고 있어요.
                      </>
                    )}
                  </>
                ) : (
                  "아직 관계망이 비어 있어요. 세계가 살아 움직이면 인연이 여기 그려집니다."
                )}
              </div>
            )
          ) : selectedEdge ? (
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
