/**
 * @livingfeed/api-client — Transport Abstraction Layer (ADR-010).
 *
 * 앱 코드는 프로토콜(SSE/WS/폴링)을 모른다 — 이 인터페이스만 사용한다.
 * 프로토콜 선택·재접속·백오프·커서 관리는 TAL 내부 책임이다.
 *
 * 피드 구독은 createTransport(transport.ts)가 구현한다 (SSE + 폴링 폴백).
 * 상호작용 세션(WS)은 메일박스 상호작용 경로(ADR-012 후속)와 함께 구현된다.
 */

import type { EventEnvelope } from "@livingfeed/schemas";

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
    /** 마지막으로 본 post id(ULID) — 이어받기 좌표 */
    cursor?: string;
    onItem: (item: EventEnvelope) => void;
    /** 전송 오류 통지 — 재접속은 TAL이 알아서 한다 */
    onError?: (error: unknown) => void;
  }): FeedSubscription;

  /** 상호작용 세션 — WebSocket 또는 POST+SSE 폴백 (ADR-010) */
  openSession(actorId: string): SessionHandle;
}

export { createTransport } from "./transport";
export type { SubscribeOptions, TransportOptions } from "./transport";
