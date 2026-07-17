/**
 * Living Feed 서사 푸시 SW (plan/11 §D1) — 알림은 텍스트만 담백하게,
 * 클릭하면 열려 있는 세계 창을 포커스하고 없으면 연다.
 * 페이로드 계약: gateway push.py의 {title, body} JSON.
 */

self.addEventListener("push", (event) => {
  let note = null;
  try {
    note = event.data ? event.data.json() : null;
  } catch {
    // 형식 밖 페이로드는 조용히 버린다 — 빈 알림은 신뢰만 깎는다
  }
  if (!note || !note.title) return;
  event.waitUntil(
    self.registration.showNotification(note.title, { body: note.body || "" }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((wins) => {
        const win = wins.find((w) => "focus" in w);
        return win ? win.focus() : self.clients.openWindow("/");
      }),
  );
});
