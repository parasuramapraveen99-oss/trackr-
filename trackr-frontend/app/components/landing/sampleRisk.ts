import type { AnalyticsExposure, RiskWindow } from "@trackr/core";
import type { RiskData } from "@trackr/portfolio";

/**
 * Sample input for the landing window's Analytics screen: the real Risk tab
 * (`RiskBoardPreview`) drawn from one invented year.
 *
 * Every figure is DERIVED from a single series of daily returns rather than typed in, so
 * the board cannot contradict itself: the histogram's red tail ends at the 1-day-in-20
 * loss, the deepest fall is that series' own drawdown, and the swing ratio, beta and
 * Sharpe all come from the same days. The series is seeded, so server and browser render
 * the same picture. Seed 22 was chosen for a plausible year (+27%, a -17% fall that
 * recovered) consistent with the sample investor's +45% gain elsewhere in the window.
 */

/** The window's sample market value (LandingHero SAMPLE.marketValue, ₹11,89,340). */
const MARKET_VALUE = 1189340;
const RISK_FREE = 0.065;
const DAYS = 250;
const LAST_DAY = Date.UTC(2026, 8, 18); // Fri 18 Sep 2026, before the window's 20 Sep

function seeded(seed: number): () => number {
  let s = seed >>> 0;
  return () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296;
}

function tradingDays(n: number): string[] {
  const out: string[] = [];
  for (let t = LAST_DAY; out.length < n; t -= 86_400_000) {
    const day = new Date(t).getUTCDay();
    if (day !== 0 && day !== 6) out.push(new Date(t).toISOString().slice(0, 10));
  }
  return out.reverse();
}

const mean = (a: number[]) => a.reduce((s, x) => s + x, 0) / a.length;
const sd = (a: number[]) => {
  const m = mean(a);
  return Math.sqrt(a.reduce((s, x) => s + (x - m) ** 2, 0) / (a.length - 1));
};
const quantile = (a: number[], q: number) => [...a].sort((x, y) => x - y)[Math.floor(q * (a.length - 1))]!;

function build(): RiskData {
  const rand = seeded(22);
  const gauss = () => Math.sqrt(-2 * Math.log(Math.max(rand(), 1e-9))) * Math.cos(2 * Math.PI * rand());
  const bench: number[] = [];
  const book: number[] = [];
  for (let i = 0; i < DAYS; i++) {
    const b = 0.0004 + 0.008 * gauss();
    bench.push(b);
    book.push(0.0003 + 1.1 * b + 0.0075 * gauss());
  }
  const dates = tradingDays(DAYS);

  let value = 1, peak = 1, peakAt = 0;
  const drawdown: number[] = [];
  const marks = { peak: dates[0]!, trough: dates[0]!, recovered: null as string | null };
  let worst = 0;
  book.forEach((r, i) => {
    value *= 1 + r;
    if (value >= peak) {
      peak = value;
      peakAt = i;
    }
    const dd = value / peak - 1;
    drawdown.push(dd);
    if (dd < worst) {
      worst = dd;
      marks.trough = dates[i]!;
      marks.peak = dates[peakAt]!;
      marks.recovered = null;
    } else if (dd === 0 && worst < 0 && marks.recovered == null) {
      marks.recovered = dates[i]!;
    }
  });
  const cum = value - 1;
  const benchCum = bench.reduce((v, r) => v * (1 + r), 1) - 1;

  const vol = sd(book) * Math.sqrt(252);
  const benchVol = sd(bench) * Math.sqrt(252);
  const cov = mean(book.map((r, i) => (r - mean(book)) * (bench[i]! - mean(bench)))) * (DAYS / (DAYS - 1));
  const downside = Math.sqrt(mean(book.map((r) => Math.min(0, r) ** 2))) * Math.sqrt(252);
  const var95 = -quantile(book, 0.05);
  const var99 = -quantile(book, 0.01);
  const cvar95 = -mean(book.filter((r) => r <= -var95));

  const step = 0.0025, lo = -0.05;
  const edges = Array.from({ length: 41 }, (_, i) => +(lo + i * step).toFixed(4));
  const counts = new Array<number>(40).fill(0);
  for (const r of book) counts[Math.min(39, Math.max(0, Math.floor((r - lo) / step)))]! += 1;

  const window: RiskWindow = {
    observations: DAYS,
    cum_return: cum,
    cagr: cum,
    volatility: vol,
    sharpe: (mean(book) * 252 - RISK_FREE) / vol,
    sortino: (mean(book) * 252 - RISK_FREE) / downside,
    beta: cov / sd(bench) ** 2,
    var_1d_95: var95,
    var_1d_99: var99,
    cvar_1d_95: cvar95,
    max_drawdown: worst,
    bench_cum_return: benchCum,
    bench_cagr: benchCum,
    bench_volatility: benchVol,
    bench_sharpe: (mean(bench) * 252 - RISK_FREE) / benchVol,
    risk_free_rate: RISK_FREE,
    var_method: "historical",
    sortino_target: "risk_free",
    var_1d_95_value: (var95 * MARKET_VALUE).toFixed(2),
    cvar_1d_95_value: (cvar95 * MARKET_VALUE).toFixed(2),
    volatility_1d_value: (sd(book) * MARKET_VALUE).toFixed(2),
    drawdown_dates: marks,
    drawdown_series: { dates, values: drawdown },
    return_histogram: { edges, counts },
  };

  // Nine holdings across five sectors, none above 15% -- what the window says elsewhere.
  // Each column sums to 100.
  const contributions: AnalyticsExposure[] = [
    ["INE002A01018", "RELIANCE", 14.8, 17.8],
    ["INE040A01034", "HDFCBANK", 13.9, 12.1],
    ["INE009A01021", "INFY", 12.6, 13.0],
    ["INE467B01029", "TCS", 12.1, 10.9],
    ["INE090A01021", "ICICIBANK", 11.4, 12.4],
    ["INE018A01030", "LT", 10.2, 11.6],
    ["INE280A01028", "TITAN", 9.3, 10.1],
    ["INE021A01026", "ASIANPAINT", 8.4, 6.2],
    ["INE237A01028", "KOTAKBANK", 7.3, 5.9],
  ].map(([isin, symbol, weight, risk]) => ({
    isin: isin as string,
    symbol: symbol as string,
    weight_pct: weight as number,
    risk_share_pct: risk as number,
  }));

  return { risk: { "1y": window }, risk_contributions: { "1y": contributions } };
}

export const SAMPLE_RISK: RiskData = build();
