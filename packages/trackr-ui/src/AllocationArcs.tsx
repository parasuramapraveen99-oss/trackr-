import type { ReactNode } from "react";
import { ALLOC_RAMP } from "./tokens";

export interface ArcSlice {
  label: string;
  /** Percent, 0–100. */
  value: number;
}

/**
 * Concentric arc rings instead of a donut.
 *
 * A donut makes every slice fight for the same ring, so the fifth and sixth
 * categories become unreadable slivers. Giving each category its own ring means a 3%
 * allocation is still a legible arc, and the ink-to-brass ramp does the ordering work
 * that hue would otherwise have to — weight reads as depth of colour.
 *
 * The ramp is seven steps, so slices beyond the seventh are dropped rather than
 * wrapping the palette and implying two categories are the same.
 */
export function AllocationArcs({
  slices,
  centerValue,
  centerLabel,
  size = 216,
  emptyMessage,
}: {
  slices: ArcSlice[];
  centerValue?: ReactNode;
  centerLabel?: ReactNode;
  size?: number;
  /**
   * Shown instead of the rings when there is nothing to allocate. Without it an empty
   * `slices` drew the centre figure inside a ring of nothing — a ₹— floating in blank
   * space, which reads as a chart that failed rather than a portfolio not yet entered.
   */
  emptyMessage?: ReactNode;
}) {
  const rings = slices.slice(0, ALLOC_RAMP.length);
  const n = rings.length;
  const c = size / 2;
  const hole = 66;
  // Ring weight and spacing are solved from the count so the innermost ring never
  // collides with the centred figure, whatever the number of categories.
  const w = n > 0 ? Math.min(9, (c - hole) / (1 + 1.35 * (n - 1))) : 9;
  const gap = w * 0.35;

  if (rings.length === 0 && emptyMessage) {
    // One dashed ring at the real diameter, so the panel keeps the height and the shape
    // it will have once there is data and the card does not resize under the cursor.
    return (
      <div style={{ display: "grid", placeItems: "center", gap: 14, padding: "6px 0 2px" }}>
        <div
          style={{
            width: size * 0.62,
            height: size * 0.62,
            borderRadius: "50%",
            border: "1px dashed var(--border)",
          }}
          aria-hidden="true"
        />
        <p
          style={{
            margin: 0,
            maxWidth: 260,
            textAlign: "center",
            font: "var(--type-body-sm)",
            color: "var(--text-tertiary)",
          }}
        >
          {emptyMessage}
        </p>
      </div>
    );
  }

  return (
    <div>
      <div style={{ position: "relative", width: size, height: size, margin: "6px auto 2px" }}>
        <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} style={{ display: "block" }} aria-hidden="true">
          {rings.map((s, i) => {
            const r = c - w / 2 - i * (w + gap);
            const circ = 2 * Math.PI * r;
            return (
              <g key={s.label}>
                <circle cx={c} cy={c} r={r} fill="none" stroke="var(--surface-sunken)" strokeWidth={w} />
                <circle
                  cx={c}
                  cy={c}
                  r={r}
                  fill="none"
                  stroke={ALLOC_RAMP[i]}
                  strokeWidth={w}
                  strokeLinecap="round"
                  strokeDasharray={`${(circ * s.value) / 100} ${circ}`}
                  transform={`rotate(-90 ${c} ${c})`}
                />
              </g>
            );
          })}
        </svg>
        <div style={{ position: "absolute", inset: 0, display: "grid", placeContent: "center", textAlign: "center" }}>
          <span className="tabular-nums" style={{ display: "block", font: "var(--type-metric)", color: "var(--text-primary)" }}>
            {centerValue}
          </span>
          <span
            style={{
              display: "block",
              marginTop: 3,
              font: "var(--type-label)",
              textTransform: "uppercase",
              letterSpacing: "var(--tracking-label)",
              color: "var(--text-tertiary)",
            }}
          >
            {centerLabel}
          </span>
        </div>
      </div>

      <div style={{ marginTop: 12, borderTop: "1px solid var(--border)" }}>
        {rings.map((s, i) => (
          <div
            key={s.label}
            style={{
              display: "grid",
              gridTemplateColumns: "10px 1fr auto",
              alignItems: "center",
              gap: 10,
              padding: "9px 0",
              borderBottom: i === rings.length - 1 ? "0" : "1px solid var(--border)",
            }}
          >
            <span style={{ height: 10, borderRadius: 2, background: ALLOC_RAMP[i] }} />
            <span style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>{s.label}</span>
            <span
              className="tabular-nums"
              style={{ font: "var(--type-body-sm)", fontWeight: 500, color: "var(--text-primary)" }}
            >
              {s.value.toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
