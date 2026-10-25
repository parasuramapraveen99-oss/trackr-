"use client";

import { useState, type ReactNode } from "react";
import { CHART_COLORS } from "./tokens";

export interface DonutSlice {
  label: string;
  /** Percent, 0–100. */
  value: number;
}

/**
 * Past this many groups the tail is folded into one grey "Other" slice.
 *
 * The categorical palette has nine hues. Wrapping it would paint the tenth sector the
 * same colour as the first, which reads as one slice split in two -- so the ninth slot
 * becomes "Other" instead, in a neutral that no named group uses.
 */
const NAMED_SLOTS = CHART_COLORS.length - 1;
const OTHER_COLOR = "var(--chart-mono-4)";

/**
 * The colour of group `index` out of `count`, shared with any table that lists the same
 * groups beside the donut so a row's swatch always matches its slice.
 *
 * CHART_COLORS are CSS variables redefined under `:root.dark`, so one call is correct in
 * both themes and the slices recolour the instant the theme switches.
 */
export function donutColor(index: number, count: number): string {
  if (count > CHART_COLORS.length && index >= NAMED_SLOTS) return OTHER_COLOR;
  return CHART_COLORS[index] as string;
}

/**
 * The allocation donut: one ring, one hue per group, read at a glance.
 *
 * It replaced concentric arcs (one ring per group) after owners found those hard to read
 * in one look -- comparing sweeps on rings of different radius is a judgement, whereas
 * neighbouring slices of one ring compare directly.
 *
 * SLICES ARE NOT RESCALED TO FILL THE RING. Each sweeps exactly its own percent, so when
 * a group's share is unavailable (the caller omits it -- see Dashboard `groupShare`) the
 * ring is left visibly incomplete rather than having the known groups stretched to cover
 * the gap and overstate themselves. `DonutChart` in charts.tsx normalises to its total,
 * which is right for a composition chart and wrong for this one.
 *
 * Hovering a slice or its legend row puts that group's name and share in the centre; the
 * legend carries every figure as text, so nothing is available only by hover.
 */
export function AllocationDonut({
  slices,
  centerValue,
  centerLabel,
  size = 216,
  thickness = 26,
  emptyMessage,
}: {
  slices: DonutSlice[];
  centerValue?: ReactNode;
  centerLabel?: ReactNode;
  size?: number;
  thickness?: number;
  /** Shown instead of the ring when there is nothing to allocate. */
  emptyMessage?: ReactNode;
}) {
  const [active, setActive] = useState<number | null>(null);

  const groups: { label: string; value: number; color: string }[] =
    slices.length > CHART_COLORS.length
      ? [
          ...slices.slice(0, NAMED_SLOTS).map((s, i) => ({ ...s, color: donutColor(i, slices.length) })),
          {
            label: `Other · ${slices.length - NAMED_SLOTS}`,
            value: slices.slice(NAMED_SLOTS).reduce((sum, s) => sum + s.value, 0),
            color: OTHER_COLOR,
          },
        ]
      : slices.map((s, i) => ({ ...s, color: donutColor(i, slices.length) }));

  if (groups.length === 0 && emptyMessage) {
    // A dashed ring at the real diameter keeps the panel's height and shape, so the card
    // does not resize under the cursor once there is data.
    return (
      <div style={{ display: "grid", placeItems: "center", gap: 14, padding: "6px 0 2px" }}>
        <div
          style={{ width: size * 0.62, height: size * 0.62, borderRadius: "50%", border: "1px dashed var(--border)" }}
          aria-hidden="true"
        />
        <p style={{ margin: 0, maxWidth: 260, textAlign: "center", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          {emptyMessage}
        </p>
      </div>
    );
  }

  const c = size / 2;
  const r = c - thickness / 2 - 1;
  const circ = 2 * Math.PI * r;
  // A hairline of the card's own surface between neighbours, so two adjacent warm hues
  // still read as two slices. Dropped for a single group, which would otherwise show a
  // nick at twelve o'clock in a full ring.
  const gap = groups.length > 1 ? 2 : 0;
  let cursor = 0;
  const arcs = groups.map((g) => {
    const run = (circ * Math.max(0, g.value)) / 100;
    const start = cursor;
    cursor += run;
    return { ...g, dash: `${Math.max(run - gap, 0.75)} ${circ}`, offset: -start };
  });

  const focus = active === null ? null : groups[active];

  return (
    <div onMouseLeave={() => setActive(null)}>
      <div style={{ position: "relative", width: size, height: size, margin: "6px auto 2px" }}>
        <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} style={{ display: "block" }} aria-hidden="true">
          <circle cx={c} cy={c} r={r} fill="none" stroke="var(--surface-sunken)" strokeWidth={thickness} />
          {arcs.map((a, i) => (
            <circle
              key={a.label}
              cx={c}
              cy={c}
              r={r}
              fill="none"
              stroke={a.color}
              strokeWidth={active === i ? thickness + 4 : thickness}
              strokeDasharray={a.dash}
              strokeDashoffset={a.offset}
              transform={`rotate(-90 ${c} ${c})`}
              onMouseEnter={() => setActive(i)}
              style={{
                cursor: "default",
                opacity: active === null || active === i ? 1 : 0.35,
                transition: "opacity var(--duration-fast) ease, stroke-width var(--duration-fast) ease",
              }}
            />
          ))}
        </svg>
        <div
          style={{
            position: "absolute",
            inset: thickness + 8,
            display: "grid",
            placeContent: "center",
            textAlign: "center",
            pointerEvents: "none",
          }}
        >
          <span className="tabular-nums" style={{ display: "block", font: "var(--type-metric)", color: "var(--text-primary)" }}>
            {focus ? `${focus.value.toFixed(1)}%` : centerValue}
          </span>
          <span
            style={{
              display: "block",
              marginTop: 3,
              font: "var(--type-label)",
              textTransform: "uppercase",
              letterSpacing: "var(--tracking-label)",
              color: "var(--text-tertiary)",
              overflowWrap: "anywhere",
            }}
          >
            {focus ? focus.label : centerLabel}
          </span>
        </div>
      </div>

      <div style={{ marginTop: 12, borderTop: "1px solid var(--border)" }}>
        {groups.map((g, i) => (
          <div
            key={g.label}
            onMouseEnter={() => setActive(i)}
            style={{
              display: "grid",
              gridTemplateColumns: "10px 1fr auto",
              alignItems: "center",
              gap: 10,
              padding: "9px 0",
              borderBottom: i === groups.length - 1 ? "0" : "1px solid var(--border)",
              opacity: active === null || active === i ? 1 : 0.55,
              transition: "opacity var(--duration-fast) ease",
            }}
          >
            <span style={{ height: 10, borderRadius: 2, background: g.color }} />
            <span style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>{g.label}</span>
            <span className="tabular-nums" style={{ font: "var(--type-body-sm)", fontWeight: 500, color: "var(--text-primary)" }}>
              {g.value.toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
