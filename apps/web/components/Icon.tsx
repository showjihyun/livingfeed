import type { CSSProperties } from "react";

interface IconProps {
  d: string;
  size: number;
  color?: string;
  style?: CSSProperties;
}

/** 24x24 stroke 아이콘 (디자인 핸드오프의 인라인 SVG와 동일 규격) */
export function Icon({ d, size, color, style }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ width: size, height: size, flexShrink: 0, color, ...style }}
    >
      <path d={d} />
    </svg>
  );
}
