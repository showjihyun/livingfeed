import type { Metadata } from "next";
import { Noto_Sans_KR, Nunito } from "next/font/google";
import type { ReactNode } from "react";

import { LocaleProvider } from "@/lib/i18n";

import "./globals.css";

const nunito = Nunito({
  subsets: ["latin"],
  weight: ["400", "600", "700", "800", "900"],
  variable: "--font-nunito",
});

const notoSansKr = Noto_Sans_KR({
  subsets: ["latin"],
  weight: ["400", "500", "700", "900"],
  variable: "--font-noto-sans-kr",
});

export const metadata: Metadata = {
  title: "Living Feed",
  description: "An interactive social drama platform for exploring a world where AI lives",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  // lang 기본은 en — 저장된 언어 선택은 LocaleProvider가 복원하며 lang도 갱신한다
  return (
    <html lang="en" className={`${nunito.variable} ${notoSansKr.variable}`}>
      <body
        style={{
          fontFamily:
            "var(--font-nunito), var(--font-noto-sans-kr), 'Nunito', 'Noto Sans KR', sans-serif",
        }}
      >
        <LocaleProvider>{children}</LocaleProvider>
      </body>
    </html>
  );
}
