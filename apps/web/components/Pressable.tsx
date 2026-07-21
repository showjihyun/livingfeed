"use client";

/**
 * Pressable — 클릭 가능한 요소의 공용 시맨틱 버튼.
 *
 * 클릭 div(`<div onClick>`)는 키보드 포커스·Enter/Space 활성화·스크린리더
 * 버튼 시맨틱이 없다. 이 컴포넌트는 실제 `<button>`을 쓰되 버튼 기본 크롬을
 * 지워, div처럼 자유롭게 스타일하면서도 접근성은 브라우저가 보장하게 한다.
 * onClick·disabled·aria-*·title·style 등은 그대로 통과된다.
 *
 * 리셋은 인라인이 아니라 CSS 클래스(.pressable)로 준다 — 인라인 리셋은 항상
 * 클래스를 이겨서, className으로 배경을 주는 버튼(.enterBtn·.navItem)과 그
 * :hover를 지워버린다(흰 글씨가 배경 없이 묻히는 회귀의 원인). 클래스로 두면
 * 뒤에 선언된 배경 클래스가 살아나고, 사용처의 인라인 style은 여전히 우선한다.
 */

import type { ButtonHTMLAttributes } from "react";

import styles from "./lf.module.css";

export function Pressable({
  type,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  const cls = className ? `${styles.pressable} ${className}` : styles.pressable;
  return <button type={type ?? "button"} className={cls} {...rest} />;
}
