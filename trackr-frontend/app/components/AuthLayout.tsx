import type { CSSProperties, ReactNode } from "react";

/**
 * The signed-out frame: centred card, type mark above it.
 *
 * Shared by sign-in and registration so the two cannot drift apart — they are the same
 * moment in the product seen twice, and a mismatch between them reads as a broken
 * deploy rather than a design choice.
 */
export function AuthLayout({
  title,
  subtitle,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        background: "var(--page)",
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 380,
          animation: "trackr-fade-in-up var(--duration-slow) var(--ease-smooth) both",
        }}
      >
        <div style={{ textAlign: "center", marginBottom: 28 }}>
          <span
            style={{
              display: "inline-flex",
              height: 40,
              width: 40,
              alignItems: "center",
              justifyContent: "center",
              borderRadius: "var(--radius-full)",
              background: "var(--ink-900)",
              color: "var(--text-inverse)",
              fontFamily: "var(--font-heading)",
              fontSize: 15,
            }}
          >
            T+
          </span>
          <h1 style={{ margin: "12px 0 0", font: "var(--type-h1)", color: "var(--text-primary)" }}>{title}</h1>
          {subtitle && (
            <p style={{ margin: "2px 0 0", font: "var(--type-body)", color: "var(--text-tertiary)" }}>{subtitle}</p>
          )}
        </div>
        {children}
      </div>
    </div>
  );
}

export const authFormStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 14,
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius-lg)",
  padding: 20,
  boxShadow: "var(--shadow-card)",
};

export function AuthError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p
      style={{
        margin: 0,
        borderRadius: "var(--radius-md)",
        background: "var(--negative-tint)",
        padding: "8px 12px",
        font: "var(--type-body)",
        color: "var(--negative)",
      }}
    >
      {message}
    </p>
  );
}
