import type { CSSProperties, ReactNode } from "react";

import { cardTitle } from "./tokens";

interface CardProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
  padding?: string | number;
  tone?: "default" | "sunken";
  style?: CSSProperties;
}

/**
 * The panel every screen is built from — charts, allocation, tables, forms.
 *
 * It wears the KPI slip's treatment exactly (see ds/tokens.ts `slipShell`): a square
 * corner, a darker ledger rule across the top, and a hairline ring plus a shallow lift
 * instead of a border. That is deliberate and it is the whole point of this component —
 * the metric slips at the top of a screen and the chart below them are the same kind of
 * object, and giving them two different silhouettes made a dashboard read as two designs
 * stacked. Structure comes from the ring and the rule, never from elevation: in a
 * near-white system a real drop shadow reads as grime.
 *
 * The header is letterspaced small caps rather than a sentence-case heading — a card
 * announces itself the way a metric does, one step above the KPI slip's own label
 * (see `cardTitle` in ds/tokens.ts). `subtitle` keeps sentence case beneath it, which
 * is where the actual prose belongs.
 */
export function Card({ title, subtitle, action, children, padding, tone = "default", style }: CardProps) {
  return (
    <section
      style={{
        background: tone === "sunken" ? "var(--surface-sunken)" : "var(--surface)",
        borderTop: "1px solid var(--ledger-rule)",
        borderRadius: 0,
        padding: padding ?? "var(--card-padding)",
        boxShadow: "var(--shadow-slip)",
        ...style,
      }}
    >
      {(title || action) && (
        <header
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: 12,
            marginBottom: 16,
          }}
        >
          <div style={{ minWidth: 0 }}>
            {title && <h3 style={cardTitle}>{title}</h3>}
            {subtitle && (
              <p style={{ margin: "6px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
                {subtitle}
              </p>
            )}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

interface EmptyStateProps {
  title: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
  dashed?: boolean;
  style?: CSSProperties;
}

export function EmptyState({ title, hint, action, dashed = true, style }: EmptyStateProps) {
  return (
    <div
      style={{
        border: (dashed ? "1px dashed " : "1px solid ") + "var(--border)",
        borderRadius: 0,
        background: "var(--surface-sunken)",
        padding: "32px 16px",
        textAlign: "center",
        ...style,
      }}
    >
      <p
        style={{
          margin: 0,
          font: "var(--type-body)",
          fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
          color: "var(--text-secondary)",
        }}
      >
        {title}
      </p>
      {hint && (
        <p style={{ margin: "4px auto 0", maxWidth: 420, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          {hint}
        </p>
      )}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  );
}
