/**
 * TAL 구현 — SSE(EventSource) 우선, 불가 환경은 롱폴링 자동 강등 (ADR-010).
 *
 * 앱 코드는 프로토콜을 모른다: 프로토콜 선택·재접속·백오프·커서 관리는
 * 전부 여기 책임이다. SSE 이벤트 id = 피드 커서(ULID)이므로 브라우저의
 * 네이티브 재접속(Last-Event-ID)이 곧 이어받기다.
 */

import type { EventEnvelope } from "@livingfeed/schemas";

import type { FeedKind, FeedSubscription, SessionHandle, Transport } from "./index";

export interface TransportOptions {
  /** gateway origin — 예: http://localhost:8000 */
  baseUrl: string;
  worldId?: string;
  /** 롱폴링 폴백의 오류 백오프 상한 (ms) */
  maxBackoffMs?: number;
}

export interface SubscribeOptions {
  feeds: FeedKind[];
  cursor?: string;
  onItem: (item: EventEnvelope) => void;
  /** 전송 오류 알림 (재접속은 TAL이 알아서 한다 — 앱은 표시만) */
  onError?: (error: unknown) => void;
}

const DEFAULT_WORLD = "w_main";
const POLL_WAIT_S = 25;

function feedUrl(
  opts: TransportOptions,
  path: "/stream/feed" | "/poll/feed",
  feeds: FeedKind[],
  cursor?: string,
): string {
  const url = new URL(path, opts.baseUrl);
  url.searchParams.set("world_id", opts.worldId ?? DEFAULT_WORLD);
  url.searchParams.set("types", feeds.join(","));
  if (cursor) url.searchParams.set("cursor", cursor);
  return url.toString();
}

function subscribeSse(opts: TransportOptions, sub: SubscribeOptions): FeedSubscription {
  const source = new EventSource(feedUrl(opts, "/stream/feed", sub.feeds, sub.cursor));
  source.addEventListener("feed.post.published", (event) => {
    sub.onItem(JSON.parse((event as MessageEvent<string>).data) as EventEnvelope);
  });
  source.onerror = (event) => {
    // EventSource가 Last-Event-ID를 들고 스스로 재접속한다 — 앱에는 통지만
    sub.onError?.(event);
  };
  return { close: () => source.close() };
}

function subscribePolling(opts: TransportOptions, sub: SubscribeOptions): FeedSubscription {
  const controller = new AbortController();
  const maxBackoff = opts.maxBackoffMs ?? 10_000;
  let cursor = sub.cursor;
  let backoff = 500;

  void (async () => {
    while (!controller.signal.aborted) {
      try {
        const url = new URL(feedUrl(opts, "/poll/feed", sub.feeds, cursor));
        url.searchParams.set("wait_s", String(POLL_WAIT_S));
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`poll ${response.status}`);
        const body = (await response.json()) as {
          items: EventEnvelope[];
          next_cursor: string | null;
        };
        for (const item of body.items) sub.onItem(item);
        cursor = body.next_cursor ?? cursor;
        backoff = 500; // 성공 — 백오프 리셋
      } catch (error) {
        if (controller.signal.aborted) return;
        sub.onError?.(error);
        await new Promise((resolve) => setTimeout(resolve, backoff));
        backoff = Math.min(backoff * 2, maxBackoff);
      }
    }
  })();

  return { close: () => controller.abort() };
}

export function createTransport(opts: TransportOptions): Transport {
  return {
    subscribe(sub: SubscribeOptions): FeedSubscription {
      return typeof EventSource === "undefined"
        ? subscribePolling(opts, sub)
        : subscribeSse(opts, sub);
    },

    openSession(_actorId: string): SessionHandle {
      // WS 상호작용 세션은 메일박스 상호작용 경로(ADR-012 후속)와 함께 구현된다
      throw new Error("openSession은 아직 구현되지 않았다 — 로드맵 다음 단계 (ADR-010 §WS)");
    },
  };
}
