import type { ReactNode } from "react";

export interface HeadlineChange {
  key: string;
  label: string;
  value: ReactNode;
  negative?: boolean | null;
}

/**
 * The screen header: title, optional subtitle, and — where the screen has one —
 * a headline figure set large with its period changes beside it.
 *
 * The headline is deliberately outside the KPI row below it. It answers "what is this
 * worth right now"; the slips answer "how did it get there". Keeping them at different
 * scales stops the one number most people open the app for from competing with four
 * others for attention.
 */
export function PageHeader({
  title,
  subtitle,
  headline,
  changes = [],
  action,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  headline?: ReactNode;
  changes?: HeadlineChange[];
  action?: ReactNode;
}) {
  return (
    <header
      style={{
        background: "var(--surface)",
        borderBottom: "1px solid var(--border)",
        padding: headline ? "18px 32px 22px" : "18px 32px",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ font: "var(--type-h1)", color: "var(--text-primary)" }}>{title}</h1>
          {subtitle && (
            <p style={{ margin: "2px 0 0", font: "var(--type-body)", color: "var(--text-tertiary)" }}>{subtitle}</p>
          )}
        </div>
        {action}
      </div>

      {headline && (
        <div style={{ display: "flex", alignItems: "baseline", flexWrap: "wrap", gap: 32, marginTop: 14 }}>
          <span
            className="tabular-nums"
            style={{
              fontFamily: "var(--font-heading)",
              fontSize: 44,
              lineHeight: 1.05,
              letterSpacing: "-0.02em",
              color: "var(--text-primary)",
            }}
          >
            {headline}
          </span>
          {changes.length > 0 && (
            <div style={{ display: "flex", gap: 28 }}>
              {changes.map((c) => (
                <div key={c.key}>
                  <span
                    style={{
                      display: "block",
                      font: "var(--type-label)",
                      textTransform: "uppercase",
                      letterSpacing: "var(--tracking-label)",
                      color: "var(--text-tertiary)",
                    }}
                  >
                    {c.label}
                  </span>
                  <span
                    className="tabular-nums"
                    style={{
                      display: "block",
                      marginTop: 3,
                      font: "var(--type-body)",
                      fontWeight: 500,
                      color:
                        c.negative == null
                          ? "var(--text-secondary)"
                          : c.negative
                            ? "var(--negative)"
                            : "var(--positive)",
                    }}
                  >
                    {c.value}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </header>
  );
}
