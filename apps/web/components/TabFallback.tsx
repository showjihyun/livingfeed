/**
 * 지연 로드 탭의 로딩 자리 — next/dynamic loading 폴백 (StudioTab·GraphTab 등
 * 무거운 탭은 초기 번들에서 빼고 열 때 코드를 받아온다). 청크가 도착하는 짧은
 * 순간에만 보이므로 담백한 중앙 placeholder 하나면 충분하다.
 */
import { COLOR, WEIGHT } from "@/lib/tokens";

export function TabFallback() {
  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: COLOR.faint,
        fontSize: 14,
        fontWeight: WEIGHT.semibold,
      }}
    >
      불러오는 중…
    </div>
  );
}
