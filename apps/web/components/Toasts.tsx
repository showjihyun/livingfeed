import { TOAST_ICON_D } from "@/lib/data";
import type { Toast } from "@/lib/types";

import { Icon } from "./Icon";

interface ToastsProps {
  toasts: Toast[];
  onClose: (id: number) => void;
}

export function Toasts({ toasts, onClose }: ToastsProps) {
  return (
    <div
      style={{
        position: "absolute",
        top: 20,
        right: 20,
        zIndex: 50,
        display: "flex",
        flexDirection: "column",
        gap: 10,
        width: 340,
      }}
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          style={{
            background: "#fff",
            border: "1.5px solid #E2EAF6",
            borderRadius: 18,
            padding: "14px 18px",
            display: "flex",
            gap: 12,
            alignItems: "flex-start",
            boxShadow: "0 12px 32px rgba(109,141,214,0.22)",
            animation: "lf-pop 0.3s ease-out",
          }}
        >
          <div
            style={{
              width: 34,
              height: 34,
              borderRadius: 12,
              background: t.iconBg,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <Icon d={TOAST_ICON_D[t.icon]} size={16} color={t.iconColor} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 800 }}>{t.title}</div>
            <div style={{ fontSize: 12, color: "#6B7691", lineHeight: 1.5, fontWeight: 600 }}>
              {t.body}
            </div>
          </div>
          <div
            onClick={() => onClose(t.id)}
            style={{
              color: "#A9B2C7",
              cursor: "pointer",
              fontSize: 14,
              fontWeight: 800,
              lineHeight: 1,
            }}
          >
            ×
          </div>
        </div>
      ))}
    </div>
  );
}
