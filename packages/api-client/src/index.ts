/**
 * @livingfeed/api-client — Transport Abstraction Layer (ADR-010).
 *
 * 앱 코드는 프로토콜(SSE/WS/폴링)을 모른다 — 이 인터페이스만 사용한다.
 * 프로토콜 선택·재접속·백오프·커서 관리는 TAL 내부 책임이다.
 * 구현(SSE/WS)은 Living Feed MVP(로드맵 7단계)에서 추가된다.
 */

export type FeedKind =
  | "world"
  | "community"
  | "relationship"
  | "personal"
  | "private"
  | "hidden";

export interface FeedSubscription {
  close(): void;
}

export interface SessionHandle {
  send(message: unknown): void;
  close(): void;
}

export interface Transport {
  /** 피드 스트림 구독 — SSE 또는 폴링 폴백 (ADR-010) */
  subscribe(opts: {
    feeds: FeedKind[];
    cursor?: string;
    onItem: (item: unknown) => void;
  }): FeedSubscription;

  /** 상호작용 세션 — WebSocket 또는 POST+SSE 폴백 (ADR-010) */
  openSession(actorId: string): SessionHandle;
}
