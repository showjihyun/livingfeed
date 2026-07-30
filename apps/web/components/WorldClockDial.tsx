"use client";

/**
 * 세계 시계의 초침 — 시간이 흐르고 있다는 것을 눈으로 보이게 한다.
 *
 * 숫자 시계는 세계 1분마다 한 번 바뀐다(실시간 15초). 그 사이 화면은 완전히
 * 멈춰 있어서, 4배속이라고 적어 두어도 흐름이 느껴지지 않았다. 그래서 한 바퀴가
 * 세계 1분인 바늘을 둔다 — 실시간 15초에 한 바퀴라, 보고 있으면 움직인다.
 *
 * 바늘의 좌표는 worldMinuteProgress다. 즉 **숫자 시계와 같은 진실**에서 나온다:
 * 엔진이 tick을 미루면 시계가 클램프되고 바늘도 함께 선다. 바늘을 CSS 무한
 * 회전으로 돌리면 훨씬 간단하지만, 그건 세계가 멈춘 순간에도 흐르는 척하는
 * 것이라 쓰지 않는다 (world-clock.ts의 클램프 규칙과 같은 규율).
 *
 * 렌더는 ref에 직접 transform을 쓴다 — 프레임마다 React를 깨우면 사이드바가
 * 상시 리렌더된다. 움직이는 것은 바늘 하나뿐이므로 DOM만 만진다.
 */

import { useEffect, useRef } from "react";

import { COLOR } from "@/lib/tokens";
import { worldMinuteProgress } from "@/lib/world-clock";
import { demoMinuteProgress } from "@/lib/world-clock-display";

/** 동작 최소화를 켠 사용자에게는 프레임마다가 아니라 1초마다 (움직임은 남기되 잔잔하게) */
const REDUCED_MOTION_MS = 1000;

export function WorldClockDial({ size = 15 }: { size?: number }) {
  const hand = useRef<SVGGElement | null>(null);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let raf = 0;
    let timer = 0;

    const draw = () => {
      // 앵커가 서면 엔진 시각을, 그 전에는 옆에 적히는 데모 시계를 따른다 —
      // 바늘은 언제나 **화면에 적힌 그 시계**와 같은 것을 가리킨다
      const progress = worldMinuteProgress() ?? demoMinuteProgress();
      if (hand.current) {
        hand.current.style.transform = `rotate(${progress * 360}deg)`;
      }
    };

    const loop = () => {
      draw();
      raf = window.requestAnimationFrame(loop);
    };

    const start = () => {
      window.cancelAnimationFrame(raf);
      window.clearInterval(timer);
      if (reduced.matches) {
        draw();
        timer = window.setInterval(draw, REDUCED_MOTION_MS);
      } else {
        loop();
      }
    };

    start();
    reduced.addEventListener("change", start);
    return () => {
      window.cancelAnimationFrame(raf);
      window.clearInterval(timer);
      reduced.removeEventListener("change", start);
    };
  }, []);

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden  // 시간은 옆의 숫자 시계가 읽어 준다 — 바늘은 그 곁의 움직임이다
      style={{ display: "block", flexShrink: 0 }}
    >
      <circle cx="12" cy="12" r="10" fill="none" stroke={COLOR.fainter} strokeWidth="2" />
      <g ref={hand} style={{ transformOrigin: "12px 12px" }}>
        <line
          x1="12" y1="12" x2="12" y2="4.5"
          stroke={COLOR.primary} strokeWidth="2" strokeLinecap="round"
        />
      </g>
      <circle cx="12" cy="12" r="1.6" fill={COLOR.primary} />
    </svg>
  );
}
