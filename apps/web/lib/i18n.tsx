"use client";

/**
 * UI 언어 seam — 표시 문자열은 컴포넌트/모듈에 co-located된 메시지 객체에서
 * locale로 고른다. 기본은 영어(en), 설정 메뉴(사이드바)에서 한국어로 전환.
 *
 * 패턴 (컴포넌트):
 *   const en = { title: "World Feed", count: (n: number) => `${n} posts` };
 *   const M: Record<Locale, typeof en> = { en, ko: { title: "...", count: (n) => `...` } };
 *   const t = useMessages(M);           // locale 변경 시 리렌더
 *
 * 패턴 (비-React 모듈 — 포맷터 등):
 *   pickMessages(M)                      // 호출 시점의 locale로 고른다
 *
 * 선택은 localStorage에 남는다. 이 seam은 UI 크롬만 다룬다 — 엔진이 생성하는
 * 라이브 콘텐츠(포스트·DM·액터 이름)는 세계의 언어를 따른다.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

export type Locale = "en" | "ko";

export const LOCALES: readonly Locale[] = ["en", "ko"];
export const DEFAULT_LOCALE: Locale = "en";
const STORAGE_KEY = "lf.locale";

/** 언어 선택지의 자기표기 — 각 언어는 자기 이름으로 보인다 (i18n 관례) */
export const LOCALE_NAMES: Record<Locale, string> = {
  en: "English",
  ko: "한국어",
};

// React 밖(순수 포맷터)에서 읽는 현재 locale — Provider가 동기화한다.
// 렌더 중 호출되는 포맷터는 컴포넌트가 locale 컨텍스트를 구독하는 한 신선하다.
let currentLocale: Locale = DEFAULT_LOCALE;

/** 비-React 코드용 현재 locale — Provider 밖에선 기본값(en) */
export function getLocale(): Locale {
  return currentLocale;
}

/** 비-React 코드용 메시지 선택 — 호출 시점의 locale */
export function pickMessages<T>(messages: Record<Locale, T>): T {
  return messages[currentLocale];
}

interface LocaleContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
}

const LocaleContext = createContext<LocaleContextValue>({
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
});

export function LocaleProvider({ children }: { children: ReactNode }) {
  // 초기 렌더는 항상 기본(en) — SSR/hydration 안전. 저장된 선택은 effect에서 복원.
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved === "en" || saved === "ko") {
        // 모듈 캐시를 상태와 같은 순간에 옮긴다 — 순수 포맷터(pickMessages)가
        // 리렌더 한 프레임 동안 이전 언어로 답하지 않게 한다
        currentLocale = saved;
        setLocaleState(saved);
      }
    } catch {
      // storage 미가용(사생활 모드 등) — 기본 언어로 진행
    }
  }, []);

  useEffect(() => {
    currentLocale = locale;
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    currentLocale = next;
    setLocaleState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // 저장 실패 — 이번 세션만 유지
    }
  }, []);

  return (
    <LocaleContext.Provider value={{ locale, setLocale }}>{children}</LocaleContext.Provider>
  );
}

export function useLocale(): LocaleContextValue {
  return useContext(LocaleContext);
}

/** co-located 메시지에서 현재 locale 분기 선택 — locale 변경 시 리렌더 */
export function useMessages<T>(messages: Record<Locale, T>): T {
  return messages[useContext(LocaleContext).locale];
}
