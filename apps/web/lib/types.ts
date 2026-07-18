export type Screen = "onboarding" | "curating" | "app";

export type Tab = "feed" | "profile" | "dm" | "graph" | "hidden" | "studio";

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
  /** read.messages 행의 event id — 되읽기(REST)와 라이브(WS) 합류 시 중복 제거 키 (방금 보낸 내 댓글은 없음) */
  eventId?: string;
  /** 다른 댓글에 대한 답장 (in_reply_to ≠ post_id) — 스레드 들여쓰기 근거 */
  isReply?: boolean;
}

export interface DmMessage {
  /** 발신 주체 — 나(플레이어) 또는 상대 액터 (특정 인물에 묶지 않는다) */
  from: "me" | "actor";
  text: string;
  /** read.messages 행의 event id — 페이지를 이어 붙일 때 중복 제거 키 (실시간·방금 보낸 메시지는 없음) */
  eventId?: string;
}
