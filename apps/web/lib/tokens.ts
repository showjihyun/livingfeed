/**
 * 디자인 토큰 — 인라인 스타일에 흩어진 리터럴(색·반경·굵기·그림자)의 단일 원천.
 *
 * 목적은 흩어진 매직값을 이름으로 모으는 것: 팔레트를 한 곳에서 바꾸면 앱 전체가
 * 따라오고, 하드코딩된 hex의 오타·표류를 없앤다. 기존 `style={{}}`에서 리터럴을
 * 이 토큰으로 치환해 나간다 (점진적 — 새 코드와 손대는 컴포넌트부터).
 *
 * 값은 현행 팔레트 그대로다 (시각 변화 없음). CSS 변수가 아니라 TS 상수인 이유는
 * 이 앱의 스타일이 전부 인라인 객체라서다 — 같은 계층에서 바로 참조하려는 것.
 */

/** 색 팔레트 — 사용 빈도순 상위값을 의미 이름으로 (grep 기준 최다 71→…) */
export const COLOR = {
  // 텍스트 (진함→흐림)
  ink: "#3A4256",
  muted: "#6B7691",
  faint: "#8C97AF",
  fainter: "#A9B2C7",
  // 브랜드 파랑
  primary: "#6D8DD6",
  primaryDeep: "#5F7EC9",
  primarySoft: "#EDF3FD",
  accent: "#AFC8F5",
  // 면·경계
  white: "#ffffff",
  surface: "#F2F6FC",
  surfaceAlt: "#FBFAFE",
  border: "#E2EAF6",
  borderSoft: "#EEF3FB",
  borderMuted: "#D8DEEA",
  // 상태: 성공/라이브(초록)
  success: "#3E8A66",
  successBright: "#5FBF95",
  successSoft: "#E3F5EC",
  // 상태: 좋아요/리시트(분홍)
  pink: "#C76F93",
  pinkSoft: "#FBECF0",
  // Hidden/서사(보라)
  violet: "#7a68b3",
} as const;

/** 모서리 반경 — pill(알약)이 압도적으로 많다 */
export const RADIUS = {
  pill: 9999,
  lg: 20,
  md: 16,
  sm: 14,
  xs: 10,
} as const;

/** 글자 굵기 — 인라인에 반복되는 400~900 */
export const WEIGHT = {
  regular: 500,
  semibold: 600,
  bold: 700,
  heavy: 800,
  black: 900,
} as const;

/** 그림자 — 카드/떠 있는 요소의 부드러운 파랑 그림자 */
export const SHADOW = {
  card: "0 4px 12px rgba(109,141,214,0.10)",
  float: "0 8px 24px rgba(109,141,214,0.16)",
} as const;
