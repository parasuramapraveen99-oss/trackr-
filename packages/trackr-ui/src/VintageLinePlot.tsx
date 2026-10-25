import { useId, type ReactNode } from "react";
import { decimalScale } from "@trackr/core";

export interface PlotSeries {
  label: string;
  /**
   * Monetary values as EXACT DECIMAL STRINGS, one per x-position.
   *
   * `null` marks a genuinely missing snapshot -- never interpolate across one. An entry
   * that cannot be READ is treated the same way and draws a gap, because a value this
   * component cannot parse is a value it must not plot at zero; the CALLER is the one
   * that should be surfacing it as unavailable data.
   *
   * These were `number` until the Decimal migration. Passing an already-parsed number in
   * would have been indistinguishable here from an exact one, and by then the digits are
   * gone -- so the type is the enforcement.
   */
  points: (string | null)[];
  color: string;
  weight?: number;
  dash?: string;
  opacity?: number;
  marker?: "dot" | "square";
  labelColor?: string;
  /** Fill from this line down to the axis with a fading gradient of its own colour.
   *  Opt-in, and per run: a snapshot gap breaks the fill exactly as it breaks the line,
   *  so the shading never spans a value the ledger does not have. */
  area?: boolean;
}

/**
 * Ceiling on the number of x-axis labels DRAWN, whatever the series length.
 *
 * THINNING. Every data point used to get its own tick and its own label. On the monthly
 * snapshot series that reads fine — nine points, nine labels. On the analytics growth
 * series it is a year of daily closes: ~250 ticks under the axis and ~250 copies of
 * "2026-09-22" overprinting each other into a grey smear, which is the whole reason that
 * chart looked broken next to the same chart in the demo.
 *
 * Only the LABELS are thinned. Every point is still plotted, so the line keeps its shape
 * and a gap is still a gap — downsampling the data to fit the axis would change the
 * picture, which is a different and much worse fix.
 */
const MAX_X_LABELS = 9;

/**
 * The engraved value plot — portfolio value against invested cost and a rebased
 * benchmark.
 *
 * Three deliberate choices, all of them about not lying with a line:
 *
 *  1. **Gaps stay gaps.** A `null` point breaks the path into separate runs rather
 *     than drawing straight through. A missing snapshot is missing data, and joining
 *     across it invents a value the ledger cannot reproduce.
 *  2. **The band is hatched, not filled.** The area between market value and invested
 *     cost is unrealised P&L — a diagonal hatch reads as an annotation over the plot
 *     rather than a fourth series competing with the three real ones.
 *  3. **Series are labelled in place** at their last known point, so the chart needs
 *     no legend and no hover to be read on paper or in a screenshot.
 */
export function VintageLinePlot({
  labels,
  series,
  height = 300,
  formatTick = (v: string) => v,
  formatLabel = (v: string) => v,
  maxXLabels = MAX_X_LABELS,
  baseline,
  gapNote,
  band: wantsBand = false,
  inlineLabels = true,
  gridStyle = "dotted",
}: {
  labels: string[];
  series: PlotSeries[];
  height?: number;
  /** Receives the tick already rounded and rendered to the axis's own precision, so a
   *  caller only ever appends a unit -- it never has to guess how many decimals a step
   *  of 0.25 needs, nor defend against 6.500000000000001. */
  formatTick?: (v: string) => ReactNode;
  /** Applied to each x-axis label that survives thinning. Separate from `formatTick`,
   *  which is the Y axis: a daily series wants "2026-09" on x and "₹1,12,400" on y, and
   *  one formatter cannot be both. */
  formatLabel?: (v: string) => string;
  /** Ceiling on drawn x-axis labels — see THINNING above. */
  maxXLabels?: number;
  /** A dashed horizontal reference line, e.g. the ₹1,00,000 a growth series starts from.
   *  Drawn under the series and labelled at the right margin. `value` is exact decimal
   *  text, like every other monetary value here. */
  baseline?: { value: string; label?: string };
  gapNote?: string;
  /** Hatch the area between the first two series. OFF by default, and opt-in rather
   *  than positional on purpose: the band asserts a specific relationship between those
   *  two series (it was written for market value against invested cost, where the gap
   *  IS unrealised P&L). Drawn automatically, it would silently reappear between
   *  whatever two series happen to be first -- shading the distance between a portfolio
   *  and its benchmark, say, which measures nothing. */
  band?: boolean;
  /** Label each series at its last point (the default). Off when the caller draws its own
   *  legend under the plot. */
  inlineLabels?: boolean;
  /** Horizontal gridlines: the engraved dotted rule, or a faint solid rule. */
  gridStyle?: "dotted" | "solid";
}) {
  // Gradient ids must be unique per plot on a page; two charts sharing one id would
  // paint each other's fills.
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const W = 860;
  const PL = 46;
  const PR = 108;
  const PT = 18;
  const PB = 34;

  const drawable = series.flatMap((s) => s.points).filter((v): v is string => v != null);
  if (drawable.length === 0 || labels.length < 2) return null;

  // The baseline joins the extent it is drawn against. Left out, a ₹1,00,000 reference
  // under a series that never dips below ₹1,10,000 would be clipped off the bottom of
  // the plot -- present in the code, invisible on the screen.
  //
  // The scale itself is the SHARED Decimal one (lib/format.ts): exact bounds, exact tick
  // labels, and a separate dimensionless position per value. It replaces a local numeric
  // `scaleFor` whose padding and rounding were arithmetically identical but computed on
  // doubles, which is why an all-zero series used to gain a phantom third gridline.
  const extent = baseline ? [...drawable, baseline.value] : drawable;
  const scale = decimalScale(extent);
  const ticks = scale.ticks;

  // Inner breathing room: the first and last points sit a little inside the axes instead
  // of on the corner, where they crowded the bottom y label and the first date.
  const XPAD = 14;
  const x = (i: number) => PL + XPAD + (i * (W - PL - PR - 2 * XPAD)) / (labels.length - 1);

  /**
   * Exact value -> a y PIXEL. Null for anything the scale cannot place.
   *
   * `scale.position` does the only conversion in the chart, in Decimal, and what it hands
   * back is dimensionless -- 0 at the axis floor, 1 at its ceiling. Multiplying that by a
   * pixel height is geometry, and the result is never read back as an amount: tick text
   * and tooltips come from the exact strings, never from inverting this.
   */
  const y = (v: string | null | undefined): number | null => {
    const at = scale.position(v);
    return at === null ? null : PT + (1 - at) * (height - PT - PB);
  };

  // Evenly spaced through the series and ALWAYS including both ends, because the first
  // and last dates are the two a reader looks for. Rounding the stride means the gaps
  // can differ by one index, which is invisible; dropping the last label is not.
  const labelStride = Math.max(1, Math.ceil((labels.length - 1) / Math.max(1, maxXLabels - 1)));
  const xTicks = (
    labels.length <= maxXLabels
      ? labels.map((_, i) => i)
      : [
          ...Array.from({ length: Math.floor((labels.length - 1) / labelStride) }, (_, k) => k * labelStride),
          labels.length - 1,
        ]
  ).map((i) => ({ i, text: labels[i] ?? "" }));

  /** Contiguous runs of PLOTTABLE indices — one path segment each.
   *
   *  Missing and unreadable are the same thing here: both break the run. A string the
   *  scale cannot place is not a point at zero, it is an absence, and joining across it
   *  would draw a value the ledger cannot reproduce. */
  const runs = (pts: (string | null)[]): number[][] => {
    const out: number[][] = [];
    let cur: number[] = [];
    pts.forEach((v, i) => {
      if (y(v) === null) {
        if (cur.length) out.push(cur);
        cur = [];
      } else cur.push(i);
    });
    if (cur.length) out.push(cur);
    return out;
  };

  // Coordinate formatting stays numeric because by this line the values ARE pixels.
  const path = (pts: (string | null)[]) =>
    runs(pts)
      .map((r) => "M" + r.map((i) => `${x(i).toFixed(1)} ${(y(pts[i]) as number).toFixed(1)}`).join("L"))
      .join(" ");

  // The band closes the primary series (market value) against the reference series
  // (invested cost), but only over spans where BOTH have data — otherwise it would
  // fill straight across a snapshot gap and invent the very area it is measuring.
  const primary = series[0];
  const reference = series[1];
  const band =
    wantsBand && primary && reference
      ? runs(primary.points)
          .filter((r) => r.every((i) => y(reference.points[i]) !== null))
          .map(
            (r) =>
              "M" +
              r.map((i) => `${x(i).toFixed(1)} ${(y(primary.points[i]) as number).toFixed(1)}`).join("L") +
              "L" +
              r
                .slice()
                .reverse()
                .map((i) => `${x(i).toFixed(1)} ${(y(reference.points[i]) as number).toFixed(1)}`)
                .join("L") +
              "Z"
          )
      : [];

  const gapIndex = primary ? primary.points.findIndex((v) => y(v) === null) : -1;

  return (
    <svg
      viewBox={`0 0 ${W} ${height}`}
      width="100%"
      style={{ display: "block", overflow: "visible" }}
      role="img"
      aria-label={series.map((s) => s.label).join(", ")}
    >
      <defs>
        {series.map((s, k) =>
          s.area ? (
            <linearGradient key={s.label} id={`${uid}-area-${k}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity={0.32} />
              <stop offset="100%" stopColor={s.color} stopOpacity={0} />
            </linearGradient>
          ) : null
        )}
        <pattern id="ledger-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--ledger-ink)" strokeWidth="0.6" opacity="0.15" />
        </pattern>
      </defs>

      {ticks.map((t) => {
        const ty = y(t);
        if (ty === null) return null;
        return (
        <g key={t}>
          <line
            x1={PL}
            y1={ty}
            x2={W - PR}
            y2={ty}
            stroke="var(--ledger-ink)"
            strokeWidth={gridStyle === "solid" ? 0.6 : 0.4}
            opacity={gridStyle === "solid" ? 0.09 : 0.11}
            strokeDasharray={gridStyle === "solid" ? undefined : "1 3"}
          />
          <text
            x={PL - 9}
            y={ty + 3.5}
            textAnchor="end"
            style={{ fontFamily: "var(--font-slip)", fontSize: 13, fill: "var(--ledger-ink)", opacity: 0.6 }}
          >
            {/* The EXACT tick string, already at the step's own precision. */}
            {formatTick(t)}
          </text>
        </g>
        );
      })}

      {band.map((dPath, i) => (
        <path key={i} d={dPath} fill="url(#ledger-hatch)" />
      ))}

      {series.map((s, k) =>
        s.area
          ? runs(s.points)
              .filter((r) => r.length > 1)
              .map((r, j) => {
                const floor = (height - PB).toFixed(1);
                const top = r
                  .map((i) => `${x(i).toFixed(1)} ${(y(s.points[i]) as number).toFixed(1)}`)
                  .join("L");
                const d = `M${x(r[0]!).toFixed(1)} ${floor}L${top}L${x(r[r.length - 1]!).toFixed(1)} ${floor}Z`;
                return <path key={`${s.label}-area-${j}`} d={d} fill={`url(#${uid}-area-${k})`} />;
              })
          : null
      )}

      {baseline && y(baseline.value) !== null && (
        <g>
          <line
            x1={PL}
            y1={y(baseline.value) as number}
            x2={W - PR}
            y2={y(baseline.value) as number}
            stroke="var(--text-tertiary)"
            strokeWidth="1"
            strokeDasharray="3 3"
          />
          {baseline.label && (
            <text
              x={W - PR + 6}
              y={(y(baseline.value) as number) + 3.5}
              style={{
                fontFamily: "var(--font-label)",
                fontSize: 9.5,
                letterSpacing: ".06em",
                fill: "var(--text-tertiary)",
              }}
            >
              {baseline.label}
            </text>
          )}
        </g>
      )}

      {/* One soft rule and no tick marks. The full-ink line with a tick under every date was
          the heaviest stroke on the chart -- near-white on the dark theme -- and out-shouted
          the series it sits under. The labels alone place the dates. */}
      <line x1={PL} y1={height - PB} x2={W - PR} y2={height - PB} stroke="var(--ledger-ink-faint)" strokeWidth="1" />
      {xTicks.map(({ i, text }) => (
        <g key={text + i}>
          <text
            // The end labels grow inward, so neither runs under the y-axis labels nor
            // off the right edge; the rest stay centred on their tick.
            x={i === 0 ? x(i) - XPAD : i === labels.length - 1 ? x(i) + XPAD : x(i)}
            y={height - PB + 18}
            textAnchor={i === 0 ? "start" : i === labels.length - 1 ? "end" : "middle"}
            style={{
              fontFamily: "var(--font-label)",
              fontSize: 9.5,
              letterSpacing: ".08em",
              textTransform: "uppercase",
              fill: "var(--ledger-ink-faint)",
            }}
          >
            {formatLabel(text)}
          </text>
        </g>
      ))}

      {/* Drawn back-to-front so the primary series sits on top of the reference lines. */}
      {series
        .slice()
        .reverse()
        .map((s) => (
          <path
            key={s.label}
            d={path(s.points)}
            fill="none"
            stroke={s.color}
            strokeWidth={s.weight ?? 1.2}
            strokeDasharray={s.dash}
            strokeLinejoin="round"
            strokeLinecap={s.dash ? "round" : "butt"}
            opacity={s.opacity ?? 1}
          />
        ))}

      {series.map((s) =>
        s.points.map((v, i) => {
          const py = y(v);
          if (py === null) return null;
          return s.marker === "square" ? (
            <rect
              key={s.label + i}
              x={x(i) - 2.6}
              y={py - 2.6}
              width="5.2"
              height="5.2"
              fill="var(--surface)"
              stroke={s.color}
              strokeWidth="1.1"
            />
          ) : s.marker === "dot" ? (
            <circle key={s.label + i} cx={x(i)} cy={py} r="3.1" fill={s.color} />
          ) : null;
        })
      )}

      {inlineLabels && series.map((s) => {
        const i = s.points.reduce((acc: number, v, idx) => (y(v) === null ? acc : idx), -1);
        if (i < 0) return null;
        return (
          <text
            key={s.label}
            x={x(i) + 10}
            y={(y(s.points[i]) as number) + 4}
            style={{
              fontFamily: "var(--font-label)",
              fontSize: 10,
              fontWeight: 600,
              letterSpacing: ".04em",
              fill: s.labelColor ?? s.color,
            }}
          >
            {s.label}
          </text>
        );
      })}

      {gapIndex > -1 && gapNote && (
        <g>
          <line
            x1={x(gapIndex)}
            y1={PT + 16}
            x2={x(gapIndex)}
            y2={height - PB - 4}
            stroke="var(--ledger-ink)"
            strokeWidth="0.5"
            strokeDasharray="2 3"
            opacity=".32"
          />
          <text
            x={x(gapIndex)}
            y={PT + 8}
            textAnchor="middle"
            style={{
              fontFamily: "var(--font-label)",
              fontSize: 9.5,
              letterSpacing: ".14em",
              textTransform: "uppercase",
              fill: "var(--ledger-ink-faint)",
            }}
          >
            {gapNote}
          </text>
        </g>
      )}
    </svg>
  );
}
