# @livingfeed/web

Next.js 웹 클라이언트. 현재 화면은 **디자인 핸드오프 프로토타입**
([docs/design-handoff](../../docs/design-handoff/)의 `LivingFeed 프로토타입.dc.html`)을
1:1로 구현한 것이다.

- **라이브 피드는 실 백엔드에 배선되어 있다** (로드맵 8단계) — World Feed 상단
  "지금 세계에서" 섹션이 feed-api(`GET /feed`, 초기 목록) + TAL
  `subscribe()`(gateway SSE, 실시간)에서 흐른다. 데이터 계층: `lib/live-feed.ts`.
  백엔드 미가용이면 "오프라인" 칩만 남고 아래 데모가 화면을 채운다.
  엔드포인트 재정의: `NEXT_PUBLIC_LF_GATEWAY_URL`(기본 :8000),
  `NEXT_PUBLIC_LF_FEED_API_URL`(기본 :8001). 3000 외 포트로 dev를 띄우면
  gateway/feed-api에 `LF_CORS_ORIGINS`로 해당 오리진을 허용해야 한다.
- 그 외 상호작용(액터 답글/DM, 토스트 체인, Hidden Feed 언락)은 프로토타입과 동일한
  **클라이언트 시뮬레이션**이다 — 세계 시계(3초당 +4분), 타자기 스트리밍(70ms/자).
  상호작용 실배선은 WS 세션 단계(ADR-010/012)에서 온다. "받은 것" 탭은 다중 대화
  인박스다 — 스레드 목록은 feed-api `GET /messages/threads`, 개별 대화는
  `GET /messages` 실측(`lib/messages.ts`)이며 액터 표시 이름은 디렉터리에서 파생된다.
- 시뮬레이션 파라미터·시나리오 데이터: `lib/data.ts`
- 상태 오케스트레이터: `components/LivingFeedApp.tsx` (화면: Onboarding → Curating → app 탭 5개)
- 블롭 아바타는 `components/Face.tsx`의 프리셋으로 그린다 — 수치는 핸드오프 픽셀 값 그대로.
- 프로토타입이 box-sizing 리셋 없이 렌더링되었으므로 전역 리셋을 넣지 않는다 (globals.css 주석 참고).

```bash
pnpm --filter @livingfeed/web dev   # 포트 3000 (사용 중이면 자동 +1)
```
