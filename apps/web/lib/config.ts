/**
 * 런타임 식별자 설정 — 특정 인물/플레이어를 컴포넌트 로직에 박지 않는다.
 *
 * 값은 배포 환경(env)에서 온다. 컴포넌트·데이터 계층은 리터럴 대신 이 모듈만
 * 읽는다 — 하드코딩된 식별자가 로직에 흩어지지 않게 하는 단일 지점(seam)이다.
 *
 * dev 기본값은 로컬 데모 시드(agents/personas, 관계 시드)와 맞춘 것이며,
 * 프로덕션 경로는 이 seam에서 갈린다:
 *  - PLAYER_ID    → 인증 세션에서 도출 (보안 후속 — 지금은 클라이언트 주장 값)
 *  - FOCUS_ACTOR  → 라이브 데이터/라우트에서 선택 (프로필·DM은 '연 액터'를 따라간다)
 */

/** 관찰자(플레이어) 식별자 — 프로덕션은 인증 세션에서 도출된다 */
export const PLAYER_ID = process.env.NEXT_PUBLIC_LF_PLAYER_ID ?? "p_observer_0417";

/** 관찰자(플레이어) 표시명 — 원시 id를 화면에 싣지 않기 위한 자기표시 이름 */
export const PLAYER_NAME = process.env.NEXT_PUBLIC_LF_PLAYER_NAME ?? "관찰자_0417";

/** 프로필·DM·댓글의 대상 액터 — 프로덕션은 사용자가 연 액터에서 파생된다 */
export const FOCUS_ACTOR_ID = process.env.NEXT_PUBLIC_LF_FOCUS_ACTOR ?? "a_minji_kim";

/**
 * 세션 공유 토큰 — BE(gateway·feed-api)가 LF_SESSION_TOKEN을 강제할 때 사설 경로에
 * 실린다: WS 세션, DM/사설 타임라인 조회, 푸시 구독. 미설정(dev)이면 빈 문자열 —
 * 어떤 헤더도 싣지 않아 개방 기본과 짝을 이룬다. 계정 인증이 서기 전의 공유 비밀
 * 게이트이며, 검증된 신원에서 도출하는 진짜 인증은 후속이다 (BE config 경고와 대칭).
 */
export const SESSION_TOKEN = process.env.NEXT_PUBLIC_LF_SESSION_TOKEN ?? "";

/** 사설 fetch의 Authorization 헤더 — 토큰이 없으면 빈 객체 (헤더 없음). */
export function authHeaders(): Record<string, string> {
  return SESSION_TOKEN ? { Authorization: `Bearer ${SESSION_TOKEN}` } : {};
}
