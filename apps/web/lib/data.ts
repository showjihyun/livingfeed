import type { Tab, ToastIcon } from "./types";

/** UI 파라미터 (특정 인물·서사 데이터 아님) */
export const TOAST_DURATION_MS = 8000;

export const TOPIC_LIST = [
  "야망과 배신",
  "로맨스",
  "직장 드라마",
  "성장 서사",
  "커뮤니티 정치",
  "미스터리",
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
  { key: "profile", label: "프로필", iconD: ICON.user },
  { key: "dm", label: "받은 것", iconD: ICON.inbox },
  { key: "graph", label: "관계 그래프", iconD: ICON.graph },
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
