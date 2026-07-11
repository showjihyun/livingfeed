export type Screen = "onboarding" | "curating" | "app";

export type Tab = "feed" | "profile" | "dm" | "graph" | "hidden";

export type RelKey = "mc" | "me" | "ms";

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
  from: "me" | "minji";
  text: string;
}
