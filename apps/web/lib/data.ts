import type { RelKey, Tab, ToastIcon } from "./types";

/** 시뮬레이션 파라미터 (프로토타입 data-props 기본값) */
export const STREAM_SPEED_MS = 70;
export const REPLY_DELAY_MS = 2200;
export const TOAST_DURATION_MS = 8000;

export const TOPIC_LIST = [
  "야망과 배신",
  "로맨스",
  "직장 드라마",
  "성장 서사",
  "커뮤니티 정치",
  "미스터리",
];

export const MINJI_POST_FULL =
  "요즘 회사 가는 길이 유난히 길게 느껴진다. 오늘도 팀장님이 내 기획안을 자기 이름으로 올렸다. 이걸 계속 참는 게 맞는 걸까.";

export const COMMENT_REPLIES = [
  "…고마워요. 사실 요즘 진짜 고민이었거든요. 이렇게 말해주는 사람이 있다는 게 좀 놀라워요.",
  "계속 마음에 담아둘게요. 내일 팀장님 얼굴 보면 또 흔들리겠지만.",
  "그 말, 잊지 않을게요.",
];

export const DM_REPLIES = [
  "정말요? 그 말 들으니까 조금 힘이 나요.",
  "내일 면담 끝나고 꼭 알려줄게요. 약속해요.",
  "고마워요. 당신 덕분에 용기가 생겼어요. 이 대화, 기억해 둘게요.",
];

/** 아이콘 path (24x24 stroke) */
export const ICON = {
  globe:
    "M2 12a10 10 0 1 0 20 0a10 10 0 1 0 -20 0M12 2a14.5 14.5 0 0 0 0 20a14.5 14.5 0 0 0 0-20M2 12h20",
  user: "M7 8a5 5 0 1 0 10 0a5 5 0 1 0 -10 0M20 21a8 8 0 0 0-16 0",
  inbox:
    "M22 12h-6l-2 3h-4l-2-3H2M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z",
  graph:
    "M9.5 4.5a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0 -5 0M2 12a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0 -5 0M17 12a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0 -5 0M9.5 19.5a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0 -5 0M10.2 6.3 6.3 10.2M7 12h10M13.8 17.7l3.9-3.9",
  lock: "M3 13a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM7 11V7a5 5 0 0 1 10 0v4",
  lockOpen:
    "M3 13a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM7 11V7a5 5 0 0 1 9.9-1",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7ZM9 12a3 3 0 1 0 6 0a3 3 0 1 0 -6 0",
  clock: "M2 12a10 10 0 1 0 20 0a10 10 0 1 0 -20 0M12 6v6l4 2",
  sparkles:
    "M12 3l-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z",
  book: "M12 7v14M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4a4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3a3 3 0 0 0-3-3z",
  heart:
    "M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z",
  messageCircle: "M7.9 20A9 9 0 1 0 4 16.1L2 22Z",
  send: "M22 2 11 13M22 2l-7 20-4-9-9-4Z",
  megaphone: "M3 11l18-5v12L3 14v-3zM11.6 16.8a3 3 0 1 1-5.8-1.6",
  journey:
    "M4 16v-2.38C4 11.5 2.97 10.5 3 8c.03-2.72 1.49-6 4.5-6C9.37 2 10 3.8 10 5.5c0 3.11-2 5.66-2 8.68V16a2 2 0 1 1-4 0ZM20 20v-2.38c0-2.12 1.03-3.12 1-5.62-.03-2.72-1.49-6-4.5-6C14.63 6 14 7.8 14 9.5c0 3.11 2 5.66 2 8.68V20a2 2 0 1 0 4 0ZM16 17h4M4 13h4",
  moon: "M12 3a6 6 0 0 0 9 9a9 9 0 1 1-9-9Z",
  info: "M2 12a10 10 0 1 0 20 0a10 10 0 1 0 -20 0M12 16v-4M12 8h.01",
} as const;

export const TOAST_ICON_D: Record<ToastIcon, string> = {
  check: "M20 6 9 17l-5-5",
  "git-branch":
    "M6 3v12M18 9a9 9 0 0 1-9 9M15 6a3 3 0 1 0 6 0a3 3 0 1 0 -6 0M3 18a3 3 0 1 0 6 0a3 3 0 1 0 -6 0",
  feather:
    "M12.67 19a2 2 0 0 0 1.416-.588l6.154-6.172a6 6 0 0 0-8.49-8.49L5.586 9.914A2 2 0 0 0 5 11.328V18a1 1 0 0 0 1 1zM16 8 2 22M17.5 15H9",
  "lock-open":
    "M3 13a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM7 11V7a5 5 0 0 1 9.9-1",
  "user-round": "M7 8a5 5 0 1 0 10 0a5 5 0 1 0 -10 0M20 21a8 8 0 0 0-16 0",
};

export interface NavDef {
  key: Tab;
  label: string;
  iconD: string;
}

export const NAV_DEFS: NavDef[] = [
  { key: "feed", label: "World Feed", iconD: ICON.globe },
  { key: "profile", label: "민지 프로필", iconD: ICON.user },
  { key: "dm", label: "받은 것", iconD: ICON.inbox },
  { key: "graph", label: "관계 그래프", iconD: ICON.graph },
];

export interface RelHistoryItem {
  title: string;
  meta: string;
  dot: string;
}

export interface RelInfo {
  title: string;
  status: string;
  statusColor: string;
  desc: string;
  hint: string;
  history: RelHistoryItem[];
}

export const REL: Record<RelKey, RelInfo> = {
  mc: {
    title: "민지 ↔ 철수",
    status: "갈등 중 · 3주째",
    statusColor: "#C76F93",
    desc: "입사 동기로 시작해 가장 가까운 사이였지만, 승진 발표 이후 서먹해졌어요. 최근 철수의 소개팅 글에 민지가 날 선 댓글을 남겼습니다.",
    hint: "두 사람 모두와 아는 사이라면, 화해의 자리를 소개할 수 있어요. (참견러 레벨 3 필요)",
    history: [
      { title: "입사 동기로 만남", meta: "세계력 1월 · 신뢰 형성", dot: "#BFE3D0" },
      { title: "철수만 승진", meta: "세계력 2월 말 · 갈등의 씨앗", dot: "#F5B8CB" },
      { title: "소개팅 글에 날 선 댓글", meta: "18분 전", dot: "#F5B8CB" },
    ],
  },
  me: {
    title: "민지 ↔ 당신",
    status: "친한 사이 · 3월 8일부터",
    statusColor: "#5F7EC9",
    desc: "야근 글에 남긴 첫 댓글에서 시작된 관계. 민지는 당신의 조언을 기억하고 있고, 먼저 DM을 보낸 적도 있어요.",
    hint: "지금 DM을 보내면 민지가 바로 확인해요 — 개입은 흔적을 남깁니다.",
    history: [
      { title: "야근 글에 첫 댓글", meta: "3월 2일 · 아는 사이", dot: "#BFE3D0" },
      { title: "DM으로 조언", meta: "3월 8일 · 친한 사이로", dot: "#AFC8F5" },
      { title: "기획안 댓글", meta: "오늘 · 민지가 면담을 결심", dot: "#AFC8F5" },
    ],
  },
  ms: {
    title: "민지 ↔ 수진",
    status: "신뢰 · 커뮤니티 동료",
    statusColor: "#3E8A66",
    desc: "개발자 커뮤니티에서 만난 사이. 수진은 민지의 글을 항상 먼저 읽어주는 사람이에요. 운영권 투표에서 민지의 지지를 기대하고 있어요.",
    hint: "민지에게 투표 이야기를 꺼내면 이 관계가 움직일 수 있어요.",
    history: [
      { title: "커뮤니티에서 만남", meta: "세계력 1월 중순", dot: "#BFE3D0" },
      { title: "밤샘 프로젝트 협업", meta: "세계력 2월 · 신뢰 형성", dot: "#BFE3D0" },
      { title: "운영권 투표 지지 요청", meta: "오늘 아침", dot: "#C9B8F0" },
    ],
  },
};

export const REL_TAB_DEFS: { key: RelKey; label: string }[] = [
  { key: "mc", label: "민지↔철수" },
  { key: "me", label: "민지↔당신" },
  { key: "ms", label: "민지↔수진" },
];

export interface GraphNodeDef {
  name: string;
  x: string;
  y: string;
  size: number;
  color: string;
  /** 이 관계가 선택되면 링 강조 (undefined면 항상/절대 아님을 selAlways로) */
  selKey?: RelKey;
  selAlways?: boolean;
}

export const GRAPH_NODES: GraphNodeDef[] = [
  { name: "김민지", x: "46%", y: "42%", size: 58, color: "#AFC8F5", selAlways: true },
  { name: "이철수", x: "23%", y: "20%", size: 46, color: "#FFE9A8", selKey: "mc" },
  { name: "박수진", x: "70%", y: "23%", size: 46, color: "#C9B8F0", selKey: "ms" },
  { name: "당신", x: "68%", y: "66%", size: 46, color: "#D9E2F2", selKey: "me" },
  { name: "한하린", x: "81%", y: "52%", size: 42, color: "#F5B8CB" },
  { name: "정도윤", x: "22%", y: "64%", size: 42, color: "#BFE3D0" },
];

export interface TimelinePost {
  meta: string;
  text: string;
}

export const MINJI_TIMELINE: TimelinePost[] = [
  {
    meta: "2시간 전 · '퇴사 고민' 4화",
    text: "요즘 회사 가는 길이 유난히 길게 느껴진다. 오늘도 팀장님이 내 기획안을 자기 이름으로 올렸다.",
  },
  {
    meta: "어제 · 세계 시간 3월 13일",
    text: "새벽 야근. 사무실에 나 혼자다. 창밖을 보다가 문득, 내가 여기서 뭘 하고 있나 싶었다.",
  },
];

/** 세계 시간: 시작 1361분(3월 14일 22:41), 3초마다 +4분 (현실 4배속) */
export const WORLD_MIN_START = 1361;

export function formatWorldTime(worldMin: number): string {
  const day = 14 + Math.floor(worldMin / 1440);
  const mm = worldMin % 1440;
  const h = String(Math.floor(mm / 60)).padStart(2, "0");
  const m = String(mm % 60).padStart(2, "0");
  return `3월 ${day}일 ${h}:${m}`;
}
