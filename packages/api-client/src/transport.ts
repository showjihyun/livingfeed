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
  /** 세션 공유 토큰 — gateway가 LF_SESSION_TOKEN을 강제할 때 필요 */
  sessionToken?: string;
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

export type SessionStatus = "connecting" | "open" | "closed";

export interface SessionOptions {
  /** 세션 소유 플레이어 — 커맨드의 발신자이자 응답 push의 필터 키 */
  playerId: string;
  /** 액터 응답(actor.message.sent 봉투) 수신 */
  onReply?: (envelope: EventEnvelope) => void;
  /** 커맨드 적재 확인 — seq는 send가 반환한 값 */
  onAck?: (seq: number, eventId: string) => void;
  /** 서버가 거부한 커맨드 (조용한 유실 금지 — 반드시 이유가 온다) */
  onCommandError?: (seq: number | null, message: string) => void;
  onStatus?: (status: SessionStatus) => void;
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

function sessionUrl(opts: TransportOptions, playerId: string): string {
  const url = new URL("/session", opts.baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("player_id", playerId);
  url.searchParams.set("world_id", opts.worldId ?? DEFAULT_WORLD);
  if (opts.sessionToken) url.searchParams.set("token", opts.sessionToken);
  return url.toString();
}

function openWsSession(opts: TransportOptions, session: SessionOptions): SessionHandle {
  let socket: WebSocket | null = null;
  let closed = false;
  let seq = 0;
  let backoff = 500;
  const pending: string[] = []; // 접속 전/재접속 중 커맨드 버퍼

  const connect = () => {
    if (closed) return;
    session.onStatus?.("connecting");
    socket = new WebSocket(sessionUrl(opts, session.playerId));

    socket.onopen = () => {
      backoff = 500;
      session.onStatus?.("open");
      for (const raw of pending.splice(0)) socket?.send(raw);
    };
    socket.onmessage = (event) => {
      const frame = JSON.parse(event.data as string) as {
        type: string;
        seq: number | null;
        payload: Record<string, unknown>;
      };
      if (frame.type === "actor.reply") {
        session.onReply?.(frame.payload as unknown as EventEnvelope);
      } else if (frame.type === "ack") {
        session.onAck?.(frame.seq as number, frame.payload.event_id as string);
      } else if (frame.type === "error") {
        session.onCommandError?.(frame.seq, String(frame.payload.message));
      }
    };
    socket.onerror = (event) => session.onError?.(event);
    socket.onclose = () => {
      socket = null;
      if (closed) return;
      session.onStatus?.("connecting");
      setTimeout(connect, backoff); // 재접속은 TAL 책임 (ADR-010)
      backoff = Math.min(backoff * 2, opts.maxBackoffMs ?? 10_000);
    };
  };
  connect();

  const sendCommand = (type: string, payload: Record<string, unknown>): number => {
    const mySeq = ++seq;
    const raw = JSON.stringify({ type, seq: mySeq, payload });
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(raw);
    else pending.push(raw);
    return mySeq;
  };

  return {
    sendDm: (targetActorId, text) =>
      sendCommand("dm.send", { target_actor_id: targetActorId, text }),
    sendComment: (targetActorId, postId, text) =>
      sendCommand("comment.post", { target_actor_id: targetActorId, post_id: postId, text }),
    addReaction: (targetActorId, postId, kind = "like") =>
      sendCommand("reaction.add", { target_actor_id: targetActorId, post_id: postId, kind }),
    close: () => {
      closed = true;
      session.onStatus?.("closed");
      socket?.close();
    },
  };
}

export function createTransport(opts: TransportOptions): Transport {
  return {
    subscribe(sub: SubscribeOptions): FeedSubscription {
      return typeof EventSource === "undefined"
        ? subscribePolling(opts, sub)
        : subscribeSse(opts, sub);
    },

    openSession(session: SessionOptions): SessionHandle {
      if (typeof WebSocket === "undefined") {
        // POST+SSE 강등(ADR-010 §Fallback)은 후속 — 지금은 명시적 실패가 침묵보다 낫다
        throw new Error("이 환경에는 WebSocket이 없다 — 상호작용 세션 불가");
      }
      return openWsSession(opts, session);
    },
  };
}
