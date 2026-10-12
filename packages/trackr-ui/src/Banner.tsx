import type { CSSProperties, ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

type BannerTone = "info" | "warning" | "negative" | "positive";

const TONES: Record<BannerTone, { bg: string; fg: string; icon: IconName }> = {
  info: { bg: "var(--info-tint)", fg: "var(--info)", icon: "document" },
  warning: { bg: "var(--warning-tint)", fg: "var(--warning)", icon: "reconcile" },
  negative: { bg: "var(--negative-tint)", fg: "var(--negative)", icon: "close" },
  positive: { bg: "var(--positive-tint)", fg: "var(--positive)", icon: "check" },
};

interface BannerProps {
  tone?: BannerTone;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  style?: CSSProperties;
}

/**
 * Inline disclosure strip — unpriced holdings, skipped rows, engine warnings.
 *
 * Trackr+ discloses data-quality problems in place rather than hiding them: a holding
 * partitioned out of XIRR says so next to the figure it would have changed, which is
 * the whole reason "a missing price raises, never defaults to zero".
 */
export function Banner({ tone = "warning", title, children, action, style }: BannerProps) {
  const t = TONES[tone] ?? TONES.warning;
  return (
    <div
      role="status"
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        background: t.bg,
        borderRadius: "var(--radius-md)",
        padding: "10px 14px",
        ...style,
      }}
    >
      <span style={{ color: t.fg, marginTop: 1 }}>
        <Icon name={t.icon} size={16} />
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        {title && (
          <p
            style={{
              margin: 0,
              font: "var(--type-body)",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
              color: t.fg,
            }}
          >
            {title}
          </p>
        )}
        {children && (
          <div style={{ marginTop: title ? 2 : 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
            {children}
          </div>
        )}
      </div>
      {action}
    </div>
  );
}
