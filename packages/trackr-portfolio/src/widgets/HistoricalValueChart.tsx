"use client";

import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useAuth } from "@trackr/core";
import { useFetch } from "@trackr/core";
import { decimalScale, formatDate, formatMoney, type DecimalScale } from "@trackr/core";
import type { SnapshotOut } from "@trackr/core";
import { EmptyState, ErrorState, LoadingState } from "./PageState";

type RangeKey = "1M" | "6M" | "1Y" | "3Y" | "Max";
const RANGES: RangeKey[] = ["1M", "6M", "1Y", "3Y", "Max"];
const RANGE_DAYS: Record<Exclude<RangeKey, "Max">, number> = {
  "1M": 30,
  "6M": 182,
  "1Y": 365,
  "3Y": 365 * 3,
};
// No trading-calendar knowledge on the frontend, so a genuine data gap (a backfill run
// that hit MissingPriceError, or several) is told apart from routine non-trading days
// (weekends, single-day holidays) by elapsed calendar days alone: a week comfortably
// covers any weekend plus one adjacent holiday without falsely flagging a normal week,
// while a real multi-day outage still trips it.
const GAP_THRESHOLD_DAYS = 7;

function startFor(range: RangeKey, end: string): string {
  if (range === "Max") return "2000-01-01";
  const d = new Date(end);
  d.setUTCDate(d.getUTCDate() - RANGE_DAYS[range]);
  return d.toISOString().slice(0, 10);
}

/**
 * One x-position on the chart.
 *
 * TWO KINDS OF FIELD, named so they cannot be confused:
 *
 *   `*_raw`  -- the exact decimal string the API sent. Everything a person READS comes
 *               from these, through the shared formatter.
 *   `*_at`   -- a normalised 0..1 AXIS POSITION, computed in Decimal by the shared scale
 *               and handed to Recharts, which needs a number to place a pixel. It is a
 *               coordinate, not an amount: nothing reads a rupee figure back out of it.
 *
 * The old field names were `market_value`/`invested` holding `Number(row.market_value)`,
 * which read as "the value" and was one careless line away from being displayed.
 */
interface ChartPoint {
  as_of: string;
  market_value_at: number | null;
  invested_at: number | null;
  market_value_raw: string | null;
  invested_raw: string | null;
  // Only set on the bridging last-settled point and the appended live point below --
  // a second series so the "not yet settled" segment renders visually distinct
  // (dashed, its own dot) without touching the persisted market_value line at all.
  market_value_live_at: number | null;
  market_value_live_raw: string | null;
  is_live: boolean;
}

export interface LiveValuePoint {
  as_of: string;
  live_as_of: string;
  market_value: string;
}

/**
 * Snapshot rows in, chart points out. A day with no persisted row is simply absent
 * from ``rows`` (app/routers/analytical.py's list_snapshots never fills one) -- when
 * two consecutive real rows are further apart than GAP_THRESHOLD_DAYS, an explicit
 * null point is inserted between them so the line breaks instead of drawing a smooth
 * connector across days nothing was recorded for.
 */
function buildChartData(rows: SnapshotOut[], scale: DecimalScale): ChartPoint[] {
  const points: ChartPoint[] = [];
  rows.forEach((row, i) => {
    const prevRow = i > 0 ? rows[i - 1] : undefined;
    if (prevRow) {
      const prev = new Date(prevRow.as_of);
      const cur = new Date(row.as_of);
      const dayGap = (cur.getTime() - prev.getTime()) / 86_400_000;
      if (dayGap > GAP_THRESHOLD_DAYS) {
        const gapDate = new Date(prev);
        gapDate.setUTCDate(gapDate.getUTCDate() + 1);
        points.push({
          as_of: gapDate.toISOString().slice(0, 10),
          market_value_at: null,
          invested_at: null,
          market_value_raw: null,
          invested_raw: null,
          market_value_live_at: null,
          market_value_live_raw: null,
          is_live: false,
        });
      }
    }
    points.push({
      as_of: row.as_of,
      // Positions, from the shared Decimal scale. An amount the scale cannot place stays
      // null and draws as a gap, never as the axis floor.
      market_value_at: scale.position(row.market_value),
      invested_at: scale.position(row.invested),
      market_value_raw: row.market_value,
      invested_raw: row.invested,
      market_value_live_at: null,
      market_value_live_raw: null,
      is_live: false,
    });
  });
  return points;
}

/**
 * Appends today's live estimate (from GET .../summary, which this chart's own
 * /snapshots call never returns -- see the module docstring) as one extra point, only
 * when the persisted series doesn't already end on that date. Once tonight's bhavcopy
 * settles, list_snapshots grows a real row for today and this stops firing on its own
 * -- no client-side bookkeeping needed to "turn it off".
 */
function appendLivePoint(
  points: ChartPoint[],
  live: LiveValuePoint | null | undefined,
  scale: DecimalScale
): ChartPoint[] {
  if (!live) return points;
  const last = points[points.length - 1];
  if (last?.as_of === live.as_of) return points;

  // No persisted history yet (a brand-new portfolio, or tonight's snapshot job simply
  // hasn't run for this range) -- nothing to bridge FROM, so the live point stands
  // alone. Still strictly more informative than the blank "no snapshots" state.
  const bridged = last
    ? points.map((p, i) =>
        i === points.length - 1
          ? {
              ...p,
              market_value_live_at: p.market_value_at,
              market_value_live_raw: p.market_value_raw,
            }
          : p
      )
    : [...points];
  bridged.push({
    as_of: live.as_of,
    market_value_at: null,
    invested_at: null,
    market_value_raw: null,
    invested_raw: null,
    market_value_live_at: scale.position(live.market_value),
    market_value_live_raw: live.market_value,
    is_live: true,
  });
  return bridged;
}

function ChartTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: ChartPoint }>;
}) {
  const entry = active ? payload?.[0] : undefined;
  if (!entry) return null;
  const point = entry.payload;
  if (point.market_value_raw === null && !point.is_live) return null;
  return (
    <div className="rounded-lg border border-navy-100 bg-surface-raised px-3 py-2 text-xs shadow-glass">
      <p className="font-medium text-navy-700">{formatDate(point.as_of)}</p>
      {point.is_live ? (
        <>
          <p className="mt-1 text-navy-600">
            Live estimate:{" "}
            <span className="font-medium">{formatMoney(point.market_value_live_raw)}</span>
          </p>
          <p className="text-[11px] text-navy-400">Not yet settled by end-of-day pricing</p>
        </>
      ) : (
        <p className="mt-1 text-navy-600">
          Market value: <span className="font-medium">{formatMoney(point.market_value_raw)}</span>
        </p>
      )}
      {point.invested_raw !== null && (
        <p className="text-navy-400">Invested: {formatMoney(point.invested_raw)}</p>
      )}
    </div>
  );
}

// Only the appended live point gets a visible dot on the market_value_live series --
// the bridging real point already has its own (dot-less) mark on the settled line, so
// drawing a second one there would just be visual noise.
function LiveDot({ cx, cy, payload }: { cx?: number; cy?: number; payload?: ChartPoint }) {
  if (!payload?.is_live || cx === undefined || cy === undefined) return null;
  return (
    <circle cx={cx} cy={cy} r={3.5} className="fill-brass-500" stroke="none" />
  );
}

/**
 * The Dashboard's historical-value chart -- backed by persisted portfolio_snapshot
 * rows (docs/trackr_plus_phase2_overview.md section 6), never a live recompute per
 * point. Replaces the honest disabled placeholder this component used to be before
 * daily snapshots were persisted (Phase 2 Session 1).
 */
export function HistoricalValueChart({ liveValue }: { liveValue?: LiveValuePoint | null }) {
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const [range, setRange] = useState<RangeKey>("1Y");
  const start = startFor(range, asOf);

  const { data, error, loading } = useFetch<SnapshotOut[]>(
    investorId ? `/investors/${investorId}/snapshots` : null,
    { start, end: asOf, advised_only: advisedOnly || undefined },
    token
  );

  /**
   * The money axis, built ONCE from every exact string this chart will draw.
   *
   * Shared with `VintageLinePlot` (lib/format.ts), so the two charts on this product
   * cannot disagree about where a gridline goes. Every point's coordinate is derived from
   * it, so the y positions and the axis labels are the same scale by construction rather
   * than by two calculations agreeing.
   */
  const scale = useMemo(() => {
    const amounts = (data ?? []).flatMap((row) => [row.market_value, row.invested]);
    if (liveValue) amounts.push(liveValue.market_value);
    return decimalScale(amounts);
  }, [data, liveValue]);

  /**
   * Axis ticks as {position, label} pairs: the position places the gridline, the label is
   * the scale's own EXACT tick string put through the shared formatter.
   *
   * The label is never derived from the position. `tickFormatter={(v) => formatMoney(
   * String(v))}` used to take whatever number Recharts handed back and format it as money
   * -- with a normalised axis that would print `₹1`, and with the old raw axis it printed
   * a double's idea of the tick.
   */
  const axisTicks = useMemo(
    () =>
      scale.ticks
        .map((tick) => ({ at: scale.position(tick), label: formatMoney(tick) }))
        .filter((t): t is { at: number; label: string } => t.at !== null),
    [scale]
  );

  const chartData = useMemo(
    () => appendLivePoint(buildChartData(data ?? [], scale), liveValue, scale),
    [data, liveValue, scale]
  );

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h3 className="font-heading text-base text-navy-900">Value over time</h3>
          <p className="text-xs text-navy-400">Daily snapshots, INR</p>
        </div>
        <div className="flex overflow-hidden rounded-lg border border-navy-100">
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={`px-3 py-1 text-xs border-r border-navy-100 last:border-r-0 transition-colors ${
                r === range
                  ? "bg-navy-700 text-cream-50"
                  : "text-navy-500 hover:bg-cream-100"
              }`}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex h-56 items-center justify-center">
          <LoadingState label="Loading history…" />
        </div>
      ) : error ? (
        <div className="flex h-56 items-center justify-center">
          <ErrorState message={error} />
        </div>
      ) : chartData.length === 0 ? (
        <div className="flex h-56 items-center justify-center">
          <EmptyState
            title="No snapshots for this range yet"
            hint="Daily portfolio snapshots build up going forward; older history fills in as backfill reaches further back."
          />
        </div>
      ) : (
        <div className="h-56 text-navy-400">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 4, right: 8, left: 8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" className="text-navy-100" stroke="currentColor" vertical={false} />
              <XAxis
                dataKey="as_of"
                tickFormatter={(v: string) => formatDate(v)}
                tick={{ fill: "currentColor", fontSize: 11 }}
                stroke="currentColor"
                tickLine={false}
                minTickGap={32}
              />
              {/* An explicit normalised domain and explicit tick POSITIONS, each paired
                  with a precomputed exact label. Recharts is told where to draw, never
                  asked what a coordinate means. */}
              <YAxis
                domain={[0, 1]}
                ticks={axisTicks.map((t) => t.at)}
                tickFormatter={(v: number) =>
                  axisTicks.find((t) => Math.abs(t.at - v) < 1e-9)?.label ?? ""
                }
                tick={{ fill: "currentColor", fontSize: 11 }}
                stroke="currentColor"
                tickLine={false}
                axisLine={false}
                width={72}
              />
              <Tooltip content={<ChartTooltip />} />
              <Line
                type="monotone"
                dataKey="invested_at"
                className="text-navy-400"
                stroke="currentColor"
                strokeWidth={1.5}
                strokeDasharray="4 3"
                dot={false}
                connectNulls={false}
              />
              <Line
                type="monotone"
                dataKey="market_value_at"
                className="text-brass-500"
                stroke="currentColor"
                strokeWidth={2}
                dot={false}
                connectNulls={false}
              />
              <Line
                type="monotone"
                dataKey="market_value_live_at"
                className="text-brass-500"
                stroke="currentColor"
                strokeWidth={2}
                strokeDasharray="4 3"
                dot={LiveDot}
                activeDot={{ r: 4 }}
                connectNulls={false}
                isAnimationActive={false}
                legendType="none"
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
