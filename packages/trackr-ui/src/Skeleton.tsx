import type { CSSProperties } from "react";
import { CHART_COLORS, ledgerHead } from "./tokens";
import { PLOT_FRAME } from "./VintageLinePlot";

/**
 * The loading placeholders.
 *
 * One rule governs every component in this file: **a skeleton occupies exactly the space
 * its real content will occupy.** Not approximately — exactly, down to the row padding and
 * the rule between rows. A placeholder that is the wrong height is worse than no
 * placeholder at all, because the page settles, the reader starts reading, and then
 * everything moves.
 *
 * That is why these are not generic grey boxes parameterised by width. `LedgerGridSkeleton`
 * takes the same `columns` array the real `LedgerGrid` takes and lays out the same grid
 * tracks; `LinePlotSkeleton` draws into the same viewBox at the same height. The structure
 * — borders, headers, rules, axes — renders immediately and for real. Only the figures are
 * standing in for something.
 */
export function Skeleton({
  width,
  height = 12,
  radius = 2,
  style,
}: {
  width?: number | string;
  height?: number | string;
  radius?: number;
  style?: CSSProperties;
}) {
  return (
    <span
      aria-hidden="true"
      className="trackr-skeleton"
      style={{ display: "block", width: width ?? "100%", height, borderRadius: radius, ...style }}
    />
  );
}

/**
 * A skeleton standing in for one figure, right-aligned like the figure will be.
 *
 * Widths vary a little per row so a loading table does not read as a barcode — but they
 * are derived from the row index, not random, so the same table renders identically on the
 * server and on the client. A `Math.random()` here is a hydration mismatch.
 */
function figureWidth(seed: number): string {
  const steps = [72, 58, 66, 52, 78, 61];
  return `${steps[seed % steps.length]}%`;
}

/**
 * The KPI slip, pre-ruled, with the figure and footer standing in.
 *
 * `wide` matches `VintageXirr`'s own `flex: 1.6` rather than a plain slip's `flex: 1`.
 * Without it the four-slip row re-proportions itself the moment /performance resolves,
 * dragging the three loaded slips sideways — a shift caused entirely by the placeholder.
 */
export function VintageKpiSkeleton({ label, wide = false }: { label?: string; wide?: boolean }) {
  return (
    <div
      style={{
        position: "relative",
        padding: "18px 18px 16px",
        background: "var(--surface)",
        borderTop: "1px solid var(--ledger-rule)",
        boxShadow: "var(--shadow-slip)",
        flex: wide ? "1.6 1 160px" : "1 1 160px",
        minWidth: 0,
      }}
    >
      {/* The label is real text, not a skeleton: it is already known before the fetch
          resolves, and showing it tells the reader which figure is arriving. */}
      <p
        style={{
          margin: "0 0 11px",
          fontFamily: "var(--font-label)",
          fontSize: 9,
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: ".2em",
          color: "var(--ledger-ink-soft)",
        }}
      >
        {label}
      </p>
      {/* 32px tall to match slipNum's own line box exactly. */}
      <Skeleton width="70%" height={32} />
      <div style={{ marginTop: 10, borderBottom: "1px solid var(--ledger-rule)", opacity: 0.55 }} />
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginTop: 9 }}>
        <Skeleton width={64} height={10} />
        <Skeleton width={44} height={10} />
      </div>
    </div>
  );
}

/**
 * The ruled grid with its header and rules drawn for real and its cells standing in.
 *
 * Takes the real column definitions so the tracks resolve to the same widths the loaded
 * table will use — the header row is not a placeholder at all, it is the finished header.
 */
export function LedgerGridSkeleton({
  columns,
  rows = 5,
  gap = 20,
  total = false,
  fillHeight = false,
}: {
  columns: { key: string; label: React.ReactNode; align?: "left" | "right"; width?: string }[];
  rows?: number;
  gap?: number;
  total?: boolean;
  /** Mirrors LedgerGrid's own `fillHeight`: hold the total against the container's bottom
   *  edge. Pass it wherever the loaded grid gets it, or the total line jumps down the card
   *  the moment the data lands -- the exact shift this file exists to prevent. */
  fillHeight?: boolean;
}) {
  const template = columns
    .map((c) => c.width || (c.align === "right" ? "minmax(0px, 1fr)" : "minmax(120px, 1.6fr)"))
    .join(" ");
  const rule = (style: string, color: string): CSSProperties => ({
    gridColumn: "1 / -1",
    height: 0,
    borderTop: `1px ${style} ${color}`,
  });

  // Same track arithmetic as LedgerGrid's, over the same item order.
  const fill = fillHeight && total;
  const headAndBodyTracks = 1 + rows + (rows === 0 ? 0 : total ? rows : rows - 1);

  return (
    <div style={{ marginTop: 2, overflowX: "auto", ...(fill ? { flex: "1 1 auto", minHeight: 0 } : null) }}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: template,
          columnGap: gap,
          alignItems: "baseline",
          minWidth: "min-content",
          ...(fill
            ? {
                minHeight: "100%",
                gridTemplateRows: `repeat(${headAndBodyTracks}, min-content) 1fr min-content min-content`,
              }
            : null),
        }}
      >
        {columns.map((c) => (
          <span
            key={`h-${c.key}`}
            style={{
              ...ledgerHead,
              paddingBottom: 8,
              textAlign: c.align || "left",
              alignSelf: "end",
            }}
          >
            {c.label}
          </span>
        ))}
        {Array.from({ length: rows }, (_, i) => (
          <ItemRow key={i} columns={columns} index={i} last={i === rows - 1 && !total} rule={rule} />
        ))}

        {fill && <span aria-hidden="true" style={{ gridColumn: "1 / -1" }} />}
        {total && <span style={{ ...rule("solid", "var(--ledger-ink)"), marginTop: 1 }} />}
        {total &&
          columns.map((c, i) => (
            <span key={`t-${c.key}`} style={{ paddingTop: 12, textAlign: c.align || "left" }}>
              <Skeleton
                width={i === 0 ? 46 : figureWidth(i + 3)}
                height={14}
                style={{ marginLeft: c.align === "right" ? "auto" : 0 }}
              />
            </span>
          ))}
      </div>
    </div>
  );
}

function ItemRow({
  columns,
  index,
  last,
  rule,
}: {
  columns: { key: string; align?: "left" | "right" }[];
  index: number;
  last: boolean;
  rule: (style: string, color: string) => CSSProperties;
}) {
  return (
    <>
      {columns.map((c, ci) => (
        // 13px/12px padding and a 16px line box are LedgerGrid's own cell metrics --
        // copied, not approximated, so rows do not resize when the figures arrive.
        <span key={`${index}-${c.key}`} style={{ padding: "13px 0 12px", minWidth: 0 }}>
          <Skeleton
            width={ci === 0 ? "62%" : figureWidth(index + ci)}
            height={16}
            style={{ marginLeft: c.align === "right" ? "auto" : 0 }}
          />
        </span>
      ))}
      {!last && <span style={rule("dotted", "var(--ledger-rule)")} />}
    </>
  );
}

/**
 * The value plot's frame, drawn for real, with a flat placeholder where the lines go.
 *
 * Same viewBox, same padding, same height as `VintageLinePlot`, so the card does not
 * change height when the series arrive — the axis simply gains its ticks and the
 * placeholder is replaced by lines.
 */
export function LinePlotSkeleton({ height = 300 }: { height?: number }) {
  const { W, PL, PR, PT, PB } = PLOT_FRAME;
  const rows = 5;
  const cols = 7;

  return (
    <svg
      viewBox={`0 0 ${W} ${height}`}
      width="100%"
      style={{ display: "block", overflow: "visible" }}
      aria-hidden="true"
    >
      {Array.from({ length: rows }, (_, i) => {
        const y = PT + (i * (height - PT - PB)) / (rows - 1);
        return (
          <g key={i}>
            <line
              x1={PL}
              y1={y}
              x2={W - PR}
              y2={y}
              stroke="var(--ledger-ink)"
              strokeWidth="0.4"
              opacity={0.11}
              strokeDasharray="1 3"
            />
            <rect x={0} y={y - 5} width="25" height="10" fill="var(--skeleton)" rx="2" />
          </g>
        );
      })}
      <line x1={PL} y1={height - PB} x2={W - PR} y2={height - PB} stroke="var(--ledger-ink)" strokeWidth="1" />
      {Array.from({ length: cols }, (_, i) => {
        const x = PL + (i * (W - PL - PR)) / (cols - 1);
        return (
          <rect key={i} x={i === 0 ? x : i === cols - 1 ? x - 32 : x - 16} y={height - PB + 10} width="32" height="9" fill="var(--skeleton)" rx="2" />
        );
      })}
      <g className="trackr-skeleton" style={{ background: "none" }}>
        <rect
          x={PL}
          y={PT + (height - PT - PB) * 0.3}
          width={W - PL - PR}
          height={(height - PT - PB) * 0.42}
          fill="var(--skeleton)"
          rx="3"
        />
      </g>
    </svg>
  );
}

/**
 * The allocation donut's frame as an unfilled track, plus the legend rows beneath.
 *
 * The ring is drawn whole and grey rather than in slices: a slice of some arbitrary
 * length would be a made-up allocation, and someone glancing at a loading screen should
 * never be able to read a number off it. Same geometry as `AllocationDonut`, so the card
 * does not resize when the figures land.
 */
export function AllocationDonutSkeleton({
  size = 216,
  thickness = 26,
  rows = 5,
}: {
  size?: number;
  thickness?: number;
  rows?: number;
}) {
  const c = size / 2;

  return (
    <div>
      <div style={{ position: "relative", width: size, height: size, margin: "6px auto 2px" }}>
        <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} aria-hidden="true">
          <circle cx={c} cy={c} r={c - thickness / 2 - 1} fill="none" stroke="var(--surface-sunken)" strokeWidth={thickness} />
        </svg>
        <div style={{ position: "absolute", inset: 0, display: "grid", placeContent: "center" }}>
          <Skeleton width={112} height={26} />
          <Skeleton width={72} height={9} style={{ margin: "6px auto 0" }} />
        </div>
      </div>
      <div style={{ marginTop: 12, borderTop: "1px solid var(--border)" }}>
        {Array.from({ length: rows }, (_, i) => (
          <div
            key={i}
            style={{
              display: "grid",
              gridTemplateColumns: "10px 1fr auto",
              alignItems: "center",
              gap: 10,
              padding: "9px 0",
              borderBottom: i === rows - 1 ? "0" : "1px solid var(--border)",
            }}
          >
            <span style={{ height: 10, borderRadius: 2, background: CHART_COLORS[i], opacity: 0.25 }} />
            <Skeleton width={`${52 + ((i * 13) % 30)}%`} height={11} />
            <Skeleton width={38} height={11} />
          </div>
        ))}
      </div>
    </div>
  );
}
