"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@trackr/core";
import { useFetch, usePortfolioRefresh } from "@trackr/core";
import type { BenchmarkSeriesResponse, IndexSeriesResponse, SnapshotOut } from "@trackr/core";
import { formatLakh } from "@trackr/core";
// The 1M/3M/6M/1Y/3Y/Max definitions live in lib/ranges.ts so this chart and the
// dividends card cannot end up meaning different things by the same button.
import { HISTORY_START, RANGES, startFor, type RangeKey } from "../ranges";
import {
  Button,
  Card,
  EmptyState,
  LinePlotSkeleton,
  SegmentedControl,
  VintageLinePlot,
  type PlotSeries,
} from "@trackr/ui";

/**
 * `HISTORY_START` (lib/ranges.ts) is the start date sent to the API: far enough back to
 * cover any ledger.
 *
 * The whole history is fetched ONCE and the ranges slice it here, rather than each range
 * being its own request. Two reasons, and the first is a bug this fixes:
 *
 *  - **The buttons have to be able to say "there is nothing more to show".** Server-side
 *    ranges cannot: a request for 1Y over four months of history returns the same rows a
 *    request for 3Y does, so every button past the data's own age redrew an identical
 *    chart and read as broken. Holding the full series here means the earliest snapshot
 *    date is known, so a range that would not clip anything is disabled and says why.
 *  - Switching range costs no round trip, so the plot redraws on the same frame.
 *
 * One snapshot row per day keeps this small: a decade of history is a few thousand rows.
 */

/**
 * No trading-calendar knowledge on the frontend, so a genuine data gap (a backfill run
 * that hit MissingPriceError) is told apart from routine non-trading days by elapsed
 * calendar days alone. A week covers any weekend plus an adjacent holiday without
 * falsely flagging a normal week, while a real multi-day outage still trips it.
 */
const GAP_THRESHOLD_DAYS = 7;

/** How many x-positions the plot draws. More than this and the axis labels collide. */
const TARGET_POINTS = 9;

/** One lakh, divided in Decimal by `formatLakh`. Never `Number(value) / 100_000`. */
const LAKH_DIGITS = 2;

/** A range needs at least this many rows to be worth drawing, and to be worth offering. */
const MIN_PLOTTABLE_POINTS = 2;

/** Up to this span, x-labels carry the day; beyond it, month and year. Four months is
 *  where "6 Aug, 20 Aug, 3 Sep" stops being more useful than "Aug 26, Sep 26". */
const SHORT_RANGE_DAYS = 120;

/**
 * The plot's height, fixed here and passed to BOTH the chart and its skeleton.
 *
 * One constant rather than two defaults: the card must be the same height before and
 * after the snapshots land, or the whole page below it jumps on first paint.
 */
const PLOT_HEIGHT = 300;

function monthLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", { month: "short", year: "2-digit" });
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

/** Day and month, no year: the axis label for a range short enough that every point
 *  shares one. Formatted rather than sliced off `dayLabel`, because the year is not a
 *  fixed number of trailing characters in every locale. */
function dayMonthLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

interface Sampled {
  labels: string[];
  /** ₹ lakh as EXACT decimal strings. `null` is a real gap in the snapshot series. */
  market: (string | null)[];
  benchmark: (string | null)[];
  hasGap: boolean;
}

/**
 * Snapshot rows in, plot series out, in ₹ lakh.
 *
 * Three things happen here and all of them matter for honesty:
 *
 *  - **Gaps are preserved, never bridged.** Where consecutive rows sit more than a
 *    week apart, a null is inserted so the line breaks. The engine does not persist a
 *    row for a day it could not price, and drawing through that absence would show a
 *    value the ledger cannot reproduce.
 *  - **Downsampling picks real rows.** To get from hundreds of daily snapshots to
 *    nine plotted points it selects existing rows at even indices rather than
 *    averaging them. Every drawn point is a figure that genuinely occurred on the
 *    date under it.
 *  - **The benchmark is joined by date, not by position.** `/benchmark-series` omits any
 *    snapshot date whose index level was missing, so its array is not index-aligned with
 *    the snapshots. Looking it up by `as_of` is what keeps a missing index level from
 *    silently shifting the whole benchmark line one point to the left.
 */
function sample(rows: SnapshotOut[], benchmarkByDate: Map<string, string>): Sampled {
  const first = rows[0];
  const last = rows[rows.length - 1];
  if (!first || !last) {
    return { labels: [], market: [], benchmark: [], hasGap: false };
  }
  const firstDate = first.as_of;
  const lastDate = last.as_of;

  const step = Math.max(1, Math.ceil(rows.length / TARGET_POINTS));
  const picked: (SnapshotOut | null)[] = [];
  let hasGap = false;

  for (let i = 0; i < rows.length; i += step) {
    const row = rows[i];
    if (!row) continue;
    const prev = picked.length > 0 ? picked[picked.length - 1] : null;
    if (prev) {
      const dayGap = (new Date(row.as_of).getTime() - new Date(prev.as_of).getTime()) / 86_400_000;
      // Compare against the sampling stride, not the raw threshold: at a wide range
      // consecutive picked rows are legitimately far apart.
      if (dayGap > GAP_THRESHOLD_DAYS * step * 1.5) {
        picked.push(null);
        hasGap = true;
      }
    }
    picked.push(row);
  }

  // Always end on the latest row, since the most recent figure is the one being asked
  // about.
  const lastPicked = picked[picked.length - 1];
  if (lastPicked && lastPicked.as_of !== lastDate) picked.push(last);

  // Short ranges span days, not months, so "Aug 26" repeated nine times labels nothing.
  const spanDays = (new Date(lastDate).getTime() - new Date(firstDate).getTime()) / 86_400_000;
  const label = spanDays > SHORT_RANGE_DAYS ? monthLabel : dayMonthLabel;

  return {
    labels: picked.map((r, i) => (r ? label(r.as_of) : `gap-${i}`)),
    // Unit conversion in Decimal, still a string afterwards. A snapshot whose amount
    // cannot be read yields null and draws as a gap, never as ₹0L.
    market: picked.map((r) => (r ? formatLakh(r.market_value, LAKH_DIGITS) : null)),
    benchmark: picked.map((r) => {
      if (!r) return null;
      const value = benchmarkByDate.get(r.as_of);
      return value == null ? null : formatLakh(value, LAKH_DIGITS);
    }),
    hasGap,
  };
}

/**
 * Portfolio value against the NIFTY 500 benchmark. TWO lines, deliberately: the question
 * this card answers is "did I beat the index", and a third invested-cost line answered a
 * different question (unrealised P&L) in the same space, which is what the KPI tiles above
 * the chart already say in numbers.
 *
 * The benchmark is not a rebased index line drawn from a single anchor. The backend
 * replicates the portfolio's OWN cash flows into NIFTY 500 TRI and marks them at each
 * snapshot date, so it starts at exactly the amount first contributed, and every later
 * contribution buys index units too. A line anchored once at the start would drift below
 * a portfolio that simply kept saving, and would read as outperformance that never
 * happened. See docs/ENGINE_SPEC.md section 6.
 *
 * If the index source is unreachable the endpoint returns no points, the series is all
 * nulls, and the plot draws nothing for it. The portfolio's own line is unaffected.
 */
export function ValueChartCard() {
  const { investorId, token, asOf } = useAuth();
  const router = useRouter();
  const [range, setRange] = useState<RangeKey>("1Y");

  // Today's point is computed live on the server until the day settles, so while the
  // chart is showing today it refreshes on the same two-minute cadence as the summary.
  const isToday = asOf === new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
  const snapshots = useFetch<SnapshotOut[]>(
    investorId ? `/investors/${investorId}/snapshots` : null,
    { start: HISTORY_START, end: asOf },
    token,
    { refreshMs: isToday ? 120_000 : 0 }
  );
  const benchmark = useFetch<BenchmarkSeriesResponse>(
    investorId ? `/investors/${investorId}/benchmark-series` : null,
    { start: HISTORY_START, end: asOf },
    token
  );

  /**
   * The market itself, for an account that has no history of its own yet.
   *
   * NOT the benchmark endpoint, and the difference is the whole point. `/benchmark-series`
   * replicates THIS investor's cash flows into the index and dates each point to one of
   * their snapshots (docs/ENGINE_SPEC.md section 6), so with no snapshots it correctly
   * returns nothing -- there are no cash flows to replicate. `/market/index-series` is
   * the published index and needs no ledger at all.
   *
   * Only fetched once we know there is nothing to plot, so a funded account never pays
   * for a request it will not draw.
   */
  const noHistory = !snapshots.loading && !snapshots.error && (snapshots.data?.length ?? 0) < MIN_PLOTTABLE_POINTS;
  const marketIndex = useFetch<IndexSeriesResponse>(
    investorId && noHistory ? "/market/index-series" : null,
    { start: startFor("1Y", asOf), end: asOf },
    token
  );

  const allRows = useMemo(() => snapshots.data ?? [], [snapshots.data]);

  // The benchmark is valued at the snapshot dates, so it must be asked again whenever the
  // snapshots change. Fetched only once, it froze at whatever existed when the page
  // opened: during a history rebuild that was a few days, and the portfolio line grew to
  // years while NIFTY 500 stayed missing (2026-10-10).
  const historyKey = `${allRows.length}:${allRows[allRows.length - 1]?.as_of ?? ""}`;
  const seenHistory = useRef(historyKey);
  useEffect(() => {
    if (seenHistory.current === historyKey) return;
    seenHistory.current = historyKey;
    benchmark.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyKey]);
  usePortfolioRefresh(() => {
    snapshots.refetch();
    benchmark.refetch();
  });

  /**
   * Which ranges have anything of their own to show.
   *
   * A range whose start is at or before the earliest snapshot draws exactly what Max
   * draws, so offering it as a separate button is a promise the data cannot keep. Those
   * are disabled and say why on hover, which is the honest version of what used to
   * happen silently.
   */
  const options = useMemo(() => {
    const earliest = allRows[0]?.as_of;
    return RANGES.map((r) => {
      if (!earliest || r === "Max") return { value: r, label: r };
      const clips = startFor(r, asOf) > earliest;
      const enough =
        allRows.filter((row) => row.as_of >= startFor(r, asOf)).length >= MIN_PLOTTABLE_POINTS;
      const usable = clips && enough;
      return {
        value: r,
        label: r,
        disabled: !usable,
        title: usable
          ? undefined
          : `History starts ${dayLabel(earliest)}, so ${r} shows the same as Max.`,
      };
    });
  }, [allRows, asOf]);

  // A disabled range must still draw something rather than an empty card, and Max is the
  // only range guaranteed to have every row there is.
  const disabledRanges = useMemo(
    () => new Set(options.filter((o) => o.disabled).map((o) => o.value)),
    [options]
  );
  const effectiveRange = disabledRanges.has(range) ? "Max" : range;

  const rows = useMemo(() => {
    const from = startFor(effectiveRange, asOf);
    return allRows.filter((row) => row.as_of >= from);
  }, [allRows, effectiveRange, asOf]);

  // Keyed by date and carrying the EXACT value. `/benchmark-series` omits any snapshot
  // date whose index level was missing, so a date absent from this map stays absent from
  // the line -- it is never filled in or interpolated.
  const benchmarkByDate = useMemo(
    () => new Map((benchmark.data?.points ?? []).map((p) => [p.as_of, p.value])),
    [benchmark.data]
  );
  const { labels, market, benchmark: benchmarkPoints, hasGap } = useMemo(
    () => sample(rows, benchmarkByDate),
    [rows, benchmarkByDate]
  );

  const hasBenchmark = benchmarkPoints.some((v) => v != null);

  /** Holdings these lines leave out, because their buy history doesn't reconcile. Taken
   *  from the most recent plotted snapshot: it is the one whose figure the eye lands on,
   *  and an exclusion clears itself as soon as the gap is closed and the next rebuild
   *  replays cleanly. Same posture as the dashboard's SkippedHoldingsNotice -- state it
   *  plainly, don't alarm, and say it's temporary. */
  const excluded = rows[rows.length - 1]?.excluded_isins ?? [];

  /** The span actually drawn, named for the caption so it is read off the plotted rows
   *  rather than restated from the range button, which can disagree with them. */
  const plotted = { from: rows[0]?.as_of ?? asOf, to: rows[rows.length - 1]?.as_of ?? asOf };

  // Brass line over a fading brass fill for the portfolio; a quiet dashed rule for the
  // index. No point markers -- with daily snapshots they crowd into a bead chain -- and
  // a legend under the plot instead of labels at the line ends.
  const PORTFOLIO_COLOR = "var(--alloc-accent)";
  const BENCHMARK_COLOR = "var(--ledger-ink-faint)";
  const series: PlotSeries[] = [
    { label: "Portfolio value", points: market, color: PORTFOLIO_COLOR, weight: 2.2, area: true },
  ];
  if (hasBenchmark) {
    series.push({
      label: "NIFTY 500",
      points: benchmarkPoints,
      color: BENCHMARK_COLOR,
      weight: 1.4,
      dash: "5 4",
    });
  }

  const body = () => {
    if (snapshots.loading) return <LinePlotSkeleton height={PLOT_HEIGHT} />;
    if (snapshots.error) {
      return (
        <p style={{ margin: 0, font: "var(--type-body)", color: "var(--negative)" }}>{snapshots.error}</p>
      );
    }
    if (labels.length < MIN_PLOTTABLE_POINTS) {
      /**
       * Draw the market instead of drawing nothing.
       *
       * Half of this card's subject is the index, and the index does not depend on the
       * reader owning anything -- so the honest thing on an empty account is not a
       * notice where a chart goes, it is the half of the chart that is real. Nothing
       * here is invented: these are published NIFTY 500 TRI closes, labelled as such,
       * with no portfolio line implied and no figure standing in for one.
       *
       * The plain EmptyState is still the fallback below, for when the index source is
       * also unreachable. A chart decoration must not become a second way for the page
       * to fail.
       */
      const indexPoints = marketIndex.data?.points ?? [];
      if (indexPoints.length >= MIN_PLOTTABLE_POINTS) {
        const step = Math.max(1, Math.ceil(indexPoints.length / TARGET_POINTS));
        const sampled = indexPoints.filter((_, i) => i % step === 0 || i === indexPoints.length - 1);
        // Non-null: guarded by indexPoints.length >= MIN_PLOTTABLE_POINTS above, and
        // `sampled` always keeps the final point.
        const lastAsOf = sampled[sampled.length - 1]?.as_of ?? asOf;
        return (
          <>
            {/* Same visual language as the portfolio chart -- line over a fading fill,
                faint solid grid, legend underneath -- but in the benchmark's grey, so the
                index is never mistaken for the reader's own (brass) portfolio line. */}
            <VintageLinePlot
              height={PLOT_HEIGHT}
              labels={sampled.map((p) => p.as_of)}
              series={[
                {
                  label: "NIFTY 500 TRI",
                  points: sampled.map((p) => p.value),
                  color: "var(--ledger-ink-faint)",
                  weight: 1.8,
                  area: true,
                },
              ]}
              formatTick={(v) => v}
              inlineLabels={false}
              gridStyle="solid"
            />
            <div
              style={{
                display: "flex",
                gap: 7,
                alignItems: "center",
                margin: "10px 0 0",
                font: "var(--type-body-sm)",
                color: "var(--text-secondary)",
              }}
            >
              <svg width="18" height="4" aria-hidden="true">
                <line x1="0" y1="2" x2="18" y2="2" stroke="var(--ledger-ink-faint)" strokeWidth="1.8" />
              </svg>
              NIFTY 500 TRI
            </div>
            <div
              style={{
                margin: "10px 0 0",
                display: "flex",
                gap: 12,
                alignItems: "baseline",
                justifyContent: "space-between",
                flexWrap: "wrap",
              }}
            >
              <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)", maxWidth: "58ch" }}>
                {`NIFTY 500, up to ${dayLabel(lastAsOf)}. Your line appears after two days of data.`}
              </p>
              <Button size="sm" onClick={() => router.push("/import")}>
                Import trades
              </Button>
            </div>
          </>
        );
      }

      return (
        <EmptyState
          title="Not enough history to plot"
          hint="Your line appears after two days of data."
        />
      );
    }
    return (
      <>
        <VintageLinePlot
          height={PLOT_HEIGHT}
          labels={labels}
          series={series}
          gapNote={hasGap ? "no snapshot" : undefined}
          formatTick={(v) => `${v}L`}
          inlineLabels={false}
          gridStyle="solid"
        />
        <div
          style={{
            display: "flex",
            gap: 20,
            alignItems: "center",
            margin: "10px 0 0",
            font: "var(--type-body-sm)",
            color: "var(--text-secondary)",
          }}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
            <svg width="18" height="4" aria-hidden="true">
              <line x1="0" y1="2" x2="18" y2="2" stroke={PORTFOLIO_COLOR} strokeWidth="2.2" />
            </svg>
            Portfolio value
          </span>
          {hasBenchmark && (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
              <svg width="18" height="4" aria-hidden="true">
                <line x1="0" y1="2" x2="18" y2="2" stroke={BENCHMARK_COLOR} strokeWidth="1.4" strokeDasharray="5 4" />
              </svg>
              NIFTY 500
            </span>
          )}
        </div>
        <p style={{ margin: "8px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          {`Showing ${dayLabel(plotted.from)} to ${dayLabel(plotted.to)}, ${rows.length} daily snapshots. `}
          {hasBenchmark
            ? " The dashed line replicates your own cash flows into NIFTY 500 TRI, so below it is genuine underperformance rather than a difference in timing."
            : " The NIFTY 500 line is absent because the index source returned no levels for these dates."}
          {hasGap ? " A break in the line is a genuine snapshot gap, not an interpolated value." : ""}
          {excluded.length > 0
            ? ` Excludes ${excluded.length} holding${excluded.length > 1 ? "s" : ""} whose older buy history is incomplete — ${excluded.length > 1 ? "they" : "it"} will be included here once that gap is closed.`
            : ""}
        </p>
      </>
    );
  };

  return (
    <Card
      title="Portfolio value vs benchmark"
      subtitle="Daily snapshots in ₹ lakh. Benchmark is NIFTY 500 TRI, replicating your own cash flows."
      action={
        <SegmentedControl size="sm" options={options} value={effectiveRange} onChange={setRange} />
      }
    >
      {/*
        The body is height-locked to the plot plus its caption. Every branch below,
        skeleton, error, empty state and the plot itself, renders into the same box, so
        switching 1Y to 3Y, or a range that turns out to have no history, cannot change
        the card's height and shove the allocation and holdings panels below it up the
        page.

        `opacity` while refreshing is the only thing that moves: the previous range's plot
        stays drawn and dims slightly rather than being replaced by a skeleton.
      */}
      <div
        style={{
          minHeight: PLOT_HEIGHT + 34,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          opacity: snapshots.refreshing ? 0.55 : 1,
          transition: "opacity var(--duration-base) var(--ease-smooth)",
        }}
      >
        {body()}
      </div>
    </Card>
  );
}
