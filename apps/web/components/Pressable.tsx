"use client";

/**
 * Pressable — 클릭 가능한 요소의 공용 시맨틱 버튼.
 *
 * 클릭 div(`<div onClick>`)는 키보드 포커스·Enter/Space 활성화·스크린리더
 * 버튼 시맨틱이 없다. 이 컴포넌트는 실제 `<button>`을 쓰되 버튼 기본 크롬을
 * 지워, div처럼 자유롭게 스타일하면서도 접근성은 브라우저가 보장하게 한다.
 * onClick·disabled·aria-*·title 등은 그대로 통과된다.
 */

import type { ButtonHTMLAttributes, CSSProperties } from "react";

//: 버튼 기본 크롬 제거 — 배경·보더·패딩·폰트 상속. display는 강제하지 않아
//: 사용처의 style(flex/inline 등)이 그대로 먹는다 (레이아웃 회귀 방지).
const RESET: CSSProperties = {
  appearance: "none",
  background: "none",
  border: "none",
  margin: 0,
  padding: 0,
  font: "inherit",
  color: "inherit",
  textAlign: "inherit",
  cursor: "pointer",
};

export function Pressable({
  style,
  type,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button type={type ?? "button"} style={{ ...RESET, ...style }} {...rest} />;
}
