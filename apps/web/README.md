# @livingfeed/web

Next.js 웹 클라이언트. 현재 화면은 **디자인 핸드오프 프로토타입**
([docs/design-handoff](../../docs/design-handoff/)의 `LivingFeed 프로토타입.dc.html`)을
1:1로 구현한 것이다.

- 백엔드가 아직 없으므로(로드맵 5–7단계) 상호작용은 프로토타입과 동일한
  **클라이언트 시뮬레이션**이다 — 세계 시계(3초당 +4분), 타자기 스트리밍(70ms/자),
  스크립트된 민지 답글/DM, 토스트 체인, Hidden Feed 언락.
- 시뮬레이션 파라미터·시나리오 데이터: `lib/data.ts`
- 상태 오케스트레이터: `components/LivingFeedApp.tsx` (화면: Onboarding → Curating → app 탭 5개)
- 블롭 아바타는 `components/Face.tsx`의 프리셋으로 그린다 — 수치는 핸드오프 픽셀 값 그대로.
- 프로토타입이 box-sizing 리셋 없이 렌더링되었으므로 전역 리셋을 넣지 않는다 (globals.css 주석 참고).

```bash
pnpm --filter @livingfeed/web dev   # 포트 3000 (사용 중이면 자동 +1)
```
