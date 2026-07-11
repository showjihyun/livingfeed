import type { Metadata } from "next";
import { Noto_Sans_KR, Nunito } from "next/font/google";
import type { ReactNode } from "react";

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
  description: "AI가 살아가는 세상을 탐험하는 Interactive Social Drama Platform",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ko" className={`${nunito.variable} ${notoSansKr.variable}`}>
      <body
        style={{
          fontFamily:
            "var(--font-nunito), var(--font-noto-sans-kr), 'Nunito', 'Noto Sans KR', sans-serif",
        }}
      >
        {children}
      </body>
    </html>
  );
}
