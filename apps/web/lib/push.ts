"use client";

/**
 * 서사 푸시 구독 계층 (plan/11 §D1) — sw 등록·권한·게이트웨이 왕복.
 *
 * 자리를 비운 사이 도착한 답장·안부(actor.message.sent)를 브라우저 알림으로
 * 받는 전달 채널이다. 게이트웨이 미가용·푸시 미설정·미지원 브라우저는 모두
 * 조용한 강등 — 세계 경험은 그대로고, 벨은 꺼진 채로 남는다.
 */

import { authHeaders, PLAYER_ID } from "./config";

const GATEWAY_URL = process.env.NEXT_PUBLIC_LF_GATEWAY_URL ?? "http://localhost:8000";
const WORLD_ID = process.env.NEXT_PUBLIC_LF_WORLD_ID ?? "w_main";
const SW_PATH = "/sw.js";

/** 구독 토글이 아는 네 가지 상태 — denied만 사용자가 브라우저에서 풀어야 한다 */
export type PushState = "unsupported" | "denied" | "off" | "on";

export function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** applicationServerKey 형식 변환 (base64url → 바이트) — Web Push 표준 절차 */
function serverKeyBytes(key: string): Uint8Array {
  const padded = key + "=".repeat((4 - (key.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await navigator.serviceWorker.getRegistration(SW_PATH);
  return (await reg?.pushManager.getSubscription()) ?? null;
}

/** 현재 상태 조회 — 벨의 초기 표정. 어떤 실패도 off로 강등한다. */
export async function getPushState(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  try {
    return (await currentSubscription()) ? "on" : "off";
  } catch {
    return "off";
  }
}

/** 구독 켜기 — vapid 키 조회 → 권한 → sw 구독 → 게이트웨이 저장. 실패는 조용한 강등. */
export async function enablePush(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  try {
    // 키가 없으면(게이트웨이 미가용·푸시 꺼짐) 권한 팝업조차 띄우지 않는다
    const keyRes = await fetch(`${GATEWAY_URL}/push/vapid-key`);
    if (!keyRes.ok) return "off";
    const { key } = (await keyRes.json()) as { key?: string };
    if (!key) return "off";

    if (Notification.permission !== "granted") {
      const permission = await Notification.requestPermission();
      if (permission === "denied") return "denied";
      if (permission !== "granted") return "off";
    }

    await navigator.serviceWorker.register(SW_PATH);
    const reg = await navigator.serviceWorker.ready;
    const sub =
      (await reg.pushManager.getSubscription()) ??
      (await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: serverKeyBytes(key) as BufferSource,
      }));

    const saved = await fetch(`${GATEWAY_URL}/push/subscribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        player_id: PLAYER_ID,
        world_id: WORLD_ID,
        subscription: sub.toJSON(),
      }),
    });
    if (!saved.ok) {
      await sub.unsubscribe(); // 게이트웨이가 못 받은 구독은 유령이 된다 — 되감는다
      return "off";
    }
    return "on";
  } catch {
    return getPushState();
  }
}

/** 구독 끄기 — 브라우저 해지 후 게이트웨이 저장분도 지운다 (실패해도 로컬은 꺼진다). */
export async function disablePush(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  try {
    const sub = await currentSubscription();
    if (!sub) return "off";
    const endpoint = sub.endpoint;
    await sub.unsubscribe();
    await fetch(`${GATEWAY_URL}/push/subscribe`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ player_id: PLAYER_ID, world_id: WORLD_ID, endpoint }),
    }).catch(() => undefined); // 남은 서버 구독은 410 응답 시 자동 제거된다
    return "off";
  } catch {
    return "off";
  }
}
