import type { CSSProperties, ReactNode } from "react";

export type BadgeTone = "neutral" | "positive" | "negative" | "warning" | "info" | "outline";

const TONES: Record<BadgeTone, CSSProperties> = {
  neutral: { background: "var(--surface-sunken)", color: "var(--text-secondary)" },
  positive: { background: "var(--positive-tint)", color: "var(--positive)" },
  negative: { background: "var(--negative-tint)", color: "var(--negative)" },
  warning: { background: "var(--warning-tint)", color: "var(--warning)" },
  info: { background: "var(--info-tint)", color: "var(--info)" },
  outline: { background: "transparent", color: "var(--text-secondary)", boxShadow: "inset 0 0 0 1px var(--border)" },
};

interface BadgeProps {
  children: ReactNode;
  tone?: BadgeTone;
  /** ISINs, batch ids and anything else that must not be read as prose. */
  mono?: boolean;
  /** Native tooltip. A badge is two or three characters by design, so the sentence it
   *  stands for has nowhere else to go — and the capability badges in the broker picker
   *  are exactly the case where a reader needs that sentence on hover. */
  title?: string;
  style?: CSSProperties;
}

export function Badge({ children, tone = "neutral", mono = false, title, style }: BadgeProps) {
  return (
    <span
      title={title}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 6px",
        borderRadius: "var(--radius-xs)",
        fontFamily: mono ? "var(--font-mono)" : "var(--font-body)",
        fontSize: "var(--text-2xs)",
        fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
        lineHeight: 1.4,
        whiteSpace: "nowrap",
        ...(TONES[tone] ?? TONES.neutral),
        ...style,
      }}
    >
      {children}
    </span>
  );
}
