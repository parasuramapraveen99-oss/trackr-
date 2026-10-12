import type { CSSProperties, ReactNode } from "react";
import { CHART_COLORS, CHART_MONO } from "./tokens";

export interface Slice {
  label: string;
  value: number;
  color?: string;
  /** Overrides the computed "x%" on the legend — use for absolute figures. */
  display?: ReactNode;
  /** A SECOND figure for the legend row, set in quieter type beside `display`.
   *  The percentage answers "how much of the book", the rupee figure answers "how
   *  much money" — a weights donut that can only answer the first is half a chart. */
  sub?: ReactNode;
}

export function ChartLegend({
  items,
  layout = "list",
  ruled = false,
  maxHeight,
  style,
}: {
  items: { label: string; value?: ReactNode; sub?: ReactNode; color?: string }[];
  layout?: "list" | "inline";
  /** Hairline between rows. A ten-row sector legend without them reads as a paragraph
   *  of labels rather than a table the eye can scan down. */
  ruled?: boolean;
  /** Scroll past this height instead of stretching the card. A donut is 200px tall; a
   *  legend that runs to 600 leaves the chart stranded at the top of a column of text. */
  maxHeight?: number;
  style?: CSSProperties;
}) {
  const isList = layout === "list";
  return (
    <ul
      style={{
        listStyle: "none",
        margin: 0,
        padding: 0,
        flex: "1 1 170px",
        minWidth: 0,
        display: "flex",
        flexDirection: isList ? "column" : "row",
        flexWrap: isList ? "nowrap" : "wrap",
        gap: isList && !ruled ? 8 : isList ? 0 : 12,
        maxHeight,
        overflowY: maxHeight ? "auto" : undefined,
        ...style,
      }}
    >
      {items.map((it, i) => (
        <li
          key={it.label + i}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: isList ? "space-between" : "flex-start",
            gap: 8,
            font: "var(--type-body)",
            padding: ruled ? "6px 0" : undefined,
            borderBottom: ruled && i < items.length - 1 ? "1px solid var(--border)" : undefined,
          }}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8, color: "var(--text-secondary)", minWidth: 0 }}>
            <span
              style={{
                height: 10,
                width: 10,
                borderRadius: 3,
                flexShrink: 0,
                background: it.color ?? CHART_COLORS[i % CHART_COLORS.length],
              }}
            />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{it.label}</span>
          </span>
          {(it.value != null || it.sub != null) && (
            <span style={{ display: "inline-flex", alignItems: "baseline", gap: 6, flexShrink: 0 }}>
              {it.value != null && (
                <span className="tabular-nums" style={{ color: "var(--text-primary)" }}>{it.value}</span>
              )}
              {it.sub != null && (
                <span className="tabular-nums"
                      style={{ color: "var(--text-tertiary)", font: "var(--type-body-sm)" }}>
                  {it.sub}
                </span>
              )}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * Stroked-arc donut.
 *
 * This was a conic-gradient behind a CSS radial mask, which drew the right picture and
 * nothing else. Two things an SVG ring gives that the gradient could not:
 *
 *  - **Every slice is an element, so every slice can carry a `<title>`.** On a ten-group
 *    sector donut the thin slices are exactly the ones whose label the legend truncates,
 *    and hovering the arc is how you read them. A single gradient div has one tooltip.
 *  - **The mask edge is antialiased against the page, not the slice.** At the inner
 *    radius that shows as a pale halo which the ring's own `stroke` does not have.
 *
 * Arcs are drawn as one circle per slice with a `stroke-dasharray` run and a cumulative
 * `stroke-dashoffset`, rotated so the first slice starts at twelve o'clock.
 */
export function DonutChart({
  slices,
  size = 148,
  thickness = 22,
  centerValue,
  centerLabel,
  palette = "color",
  showLegend = true,
  legendRuled = false,
  legendMaxHeight,
  style,
}: {
  slices: Slice[];
  size?: number;
  thickness?: number;
  centerValue?: ReactNode;
  centerLabel?: ReactNode;
  palette?: "color" | "mono";
  showLegend?: boolean;
  /** Hairline-separated legend rows — see ChartLegend. */
  legendRuled?: boolean;
  legendMaxHeight?: number;
  style?: CSSProperties;
}) {
  const colors = palette === "mono" ? CHART_MONO : CHART_COLORS;
  const total = slices.reduce((s, g) => s + (g.value || 0), 0) || 1;

  // Geometry in a fixed 200-unit viewBox, scaled by `size` on the way out, so callers
  // keep passing pixel sizes and thicknesses and nothing here depends on either.
  const VB = 200;
  const stroke = (thickness / size) * VB;
  const radius = (VB - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  let cursor = 0;
  const arcs = slices.map((g, i) => {
    const fraction = (g.value || 0) / total;
    const run = circumference * fraction;
    const offset = -cursor;
    cursor += run;
    return {
      key: g.label + i,
      color: g.color ?? colors[i % colors.length],
      dash: `${run.toFixed(2)} ${(circumference - run).toFixed(2)}`,
      offset: offset.toFixed(2),
      title: `${g.label} — ${(fraction * 100).toFixed(2)}%`,
    };
  });

  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 24, minWidth: 0, ...style }}>
      <div style={{ position: "relative", height: size, width: size, flexShrink: 0 }}>
        <svg viewBox={`0 0 ${VB} ${VB}`} width={size} height={size} style={{ display: "block" }}>
          {arcs.map((a) => (
            <circle
              key={a.key}
              cx={VB / 2}
              cy={VB / 2}
              r={radius}
              fill="none"
              stroke={a.color}
              strokeWidth={stroke}
              strokeDasharray={a.dash}
              strokeDashoffset={a.offset}
              transform={`rotate(-90 ${VB / 2} ${VB / 2})`}
            >
              <title>{a.title}</title>
            </circle>
          ))}
        </svg>
        {centerValue && (
          <div
            style={{
              position: "absolute",
              inset: thickness + 4,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              textAlign: "center",
              pointerEvents: "none",
            }}
          >
            <span
              className="tabular-nums"
              style={{
                fontFamily: "var(--font-heading)",
                fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
                lineHeight: 1.1,
                color: "var(--text-primary)",
                // Shrink to fit the hole: longer figures get a smaller face rather
                // than overflowing the ring.
                fontSize: Math.max(11, Math.min(18, (size - thickness * 2 - 8) / (String(centerValue).length * 0.56))),
              }}
            >
              {centerValue}
            </span>
            {centerLabel && (
              <span style={{ marginTop: 2, font: "var(--type-label)", color: "var(--text-tertiary)" }}>
                {centerLabel}
              </span>
            )}
          </div>
        )}
      </div>
      {showLegend && (
        <ChartLegend
          ruled={legendRuled}
          maxHeight={legendMaxHeight}
          items={slices.map((g, i) => ({
            label: g.label,
            value: g.display ?? `${(((g.value || 0) / total) * 100).toFixed(1)}%`,
            sub: g.sub,
            color: g.color ?? colors[i % colors.length],
          }))}
        />
      )}
    </div>
  );
}

/** Full pie (no hole) for part-to-whole counts — import batch outcomes, match results. */
export function PieChart({
  slices,
  size = 148,
  showLegend = true,
  style,
}: {
  slices: Slice[];
  size?: number;
  showLegend?: boolean;
  style?: CSSProperties;
}) {
  const total = slices.reduce((s, g) => s + (g.value || 0), 0) || 1;
  const r = size / 2;
  let angle = -Math.PI / 2;
  const paths = slices.map((g, i) => {
    const frac = (g.value || 0) / total;
    const sweep = frac * Math.PI * 2;
    const end = angle + sweep;
    const x1 = r + r * Math.cos(angle);
    const y1 = r + r * Math.sin(angle);
    const x2 = r + r * Math.cos(end);
    const y2 = r + r * Math.sin(end);
    const large = sweep > Math.PI ? 1 : 0;
    // A single slice at 100% cannot be drawn as an arc (start === end), so it falls
    // back to two half-arcs that close the full circle.
    const d =
      frac >= 0.9999
        ? `M ${r} 0 A ${r} ${r} 0 1 1 ${r - 0.01} 0 Z`
        : `M ${r} ${r} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
    angle = end;
    return { d, color: g.color ?? CHART_COLORS[i % CHART_COLORS.length] };
  });

  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 24, minWidth: 0, ...style }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ flexShrink: 0, display: "block" }}>
        {paths.map((p, i) => (
          <path key={i} d={p.d} fill={p.color} stroke="var(--surface)" strokeWidth="1.5" />
        ))}
      </svg>
      {showLegend && (
        <ChartLegend
          items={slices.map((g, i) => ({
            label: g.label,
            value: g.display ?? `${(((g.value || 0) / total) * 100).toFixed(1)}%`,
            color: g.color ?? CHART_COLORS[i % CHART_COLORS.length],
          }))}
        />
      )}
    </div>
  );
}
