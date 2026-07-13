export type Screen = "onboarding" | "curating" | "app";

export type Tab = "feed" | "profile" | "dm" | "graph" | "hidden";

export type ToastIcon = "check" | "git-branch" | "feather" | "lock-open" | "user-round";

export interface Toast {
  id: number;
  icon: ToastIcon;
  iconBg: string;
  iconColor: string;
  title: string;
  body: string;
}

export interface FeedComment {
  author: string;
  text: string;
  bg: string;
  avatarBg: string;
}

export interface DmMessage {
  /** 발신 주체 — 나(플레이어) 또는 상대 액터 (특정 인물에 묶지 않는다) */
  from: "me" | "actor";
  text: string;
}
